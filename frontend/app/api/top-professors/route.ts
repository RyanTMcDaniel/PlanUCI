import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

export interface TopProfessor {
  name: string;
  ucinetid: string;
  avg_difficulty: number | null;
  avg_quality: number | null;
  review_count: number;
  overall_avg_gpa: number | null;
  quarters_taught: string[];
  teaching_frequency: number;
}

function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

const QUARTER_ORDER: Record<string, number> = { Fall: 1, Winter: 2, Spring: 3, Summer: 4 };

function sortQuarters(quarters: Set<string>): string[] {
  return [...quarters].sort((a, b) => {
    const [qa, ya] = a.split(" ");
    const [qb, yb] = b.split(" ");
    const yearDiff = parseInt(ya) - parseInt(yb);
    return yearDiff !== 0 ? yearDiff : (QUARTER_ORDER[qa] ?? 5) - (QUARTER_ORDER[qb] ?? 5);
  });
}

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const courseId = searchParams.get("id");
  if (!courseId) return NextResponse.json({ professors: [] }, { status: 400 });

  const supabase = serviceClient();

  // Fetch recent sections (year >= 2023) and all known instructors in parallel
  const [recentGradesRes, courseInstrsRes] = await Promise.all([
    supabase
      .from("grade_distributions")
      .select("instructor_raw, year, quarter")
      .eq("course_id", courseId)
      .gte("year", "2023")
      .not("instructor_raw", "is", null),
    supabase
      .from("course_instructors")
      .select("ucinetid")
      .eq("course_id", courseId),
  ]);

  const recentGrades = recentGradesRes.data ?? [];
  const courseInstrs = courseInstrsRes.data ?? [];

  if (recentGrades.length === 0 || courseInstrs.length === 0) {
    return NextResponse.json({ professors: [] });
  }

  const ucinetids = courseInstrs.map((r) => r.ucinetid as string);

  // Get instructor metadata (name + shortened_names for raw-name matching)
  const { data: instructors } = await supabase
    .from("instructors")
    .select("ucinetid, name, shortened_names")
    .in("ucinetid", ucinetids);

  if (!instructors || instructors.length === 0) {
    return NextResponse.json({ professors: [] });
  }

  type InstrRow = { ucinetid: string; name: string; shortened_names: string[] };
  const recentRaws = new Set(recentGrades.map((r) => r.instructor_raw as string));

  // Keep only instructors who appear in a recent section
  const recentInstrs = (instructors as InstrRow[]).filter((instr) =>
    instr.shortened_names?.some((sn) => recentRaws.has(sn)),
  );

  if (recentInstrs.length === 0) return NextResponse.json({ professors: [] });

  // Count sections and collect quarters per instructor
  type InstrStats = InstrRow & { sectionCount: number; quarters: Set<string> };
  const instrMap = new Map<string, InstrStats>(
    recentInstrs.map((instr) => [instr.ucinetid, { ...instr, sectionCount: 0, quarters: new Set() }]),
  );

  for (const grade of recentGrades) {
    const raw = grade.instructor_raw as string;
    for (const instr of recentInstrs) {
      if (instr.shortened_names?.includes(raw)) {
        const entry = instrMap.get(instr.ucinetid)!;
        entry.sectionCount++;
        entry.quarters.add(`${grade.quarter} ${grade.year}`);
        break;
      }
    }
  }

  const totalSections = recentGrades.length;
  const qualifyingIds = [...instrMap.keys()];

  // Fetch RMP data and overall GPA for each qualifier in parallel
  const [rmpRes, ...gpaResults] = await Promise.all([
    supabase
      .from("rmp_reviews")
      .select("ucinetid, overall_rating, difficulty_rating, num_ratings")
      .in("ucinetid", qualifyingIds),
    ...recentInstrs.map((instr) =>
      supabase
        .from("grade_distributions")
        .select("average_gpa")
        .in("instructor_raw", instr.shortened_names)
        .not("average_gpa", "is", null)
        .limit(2000)
        .then(({ data }) => {
          const vals = (data ?? []).map((r) => r.average_gpa as number).filter(Number.isFinite);
          return {
            ucinetid: instr.ucinetid,
            overall_avg_gpa: vals.length > 0 ? vals.reduce((s, v) => s + v, 0) / vals.length : null,
          };
        }),
    ),
  ]);

  const rmpMap = new Map((rmpRes.data ?? []).map((r) => [r.ucinetid as string, r]));
  const gpaMap = new Map((gpaResults as { ucinetid: string; overall_avg_gpa: number | null }[]).map(
    (r) => [r.ucinetid, r.overall_avg_gpa],
  ));

  const professors: TopProfessor[] = [...instrMap.values()]
    .map((instr) => {
      const rmp = rmpMap.get(instr.ucinetid);
      return {
        name: instr.name,
        ucinetid: instr.ucinetid,
        avg_difficulty: (rmp?.difficulty_rating as number | null) ?? null,
        avg_quality: (rmp?.overall_rating as number | null) ?? null,
        review_count: (rmp?.num_ratings as number | null) ?? 0,
        overall_avg_gpa: gpaMap.get(instr.ucinetid) ?? null,
        quarters_taught: sortQuarters(instr.quarters),
        teaching_frequency: totalSections > 0 ? instr.sectionCount / totalSections : 0,
      };
    })
    .sort((a, b) => b.teaching_frequency - a.teaching_frequency)
    .slice(0, 3);

  return NextResponse.json({ professors });
}
