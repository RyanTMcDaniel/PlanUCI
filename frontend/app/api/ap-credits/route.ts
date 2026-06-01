import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

// Maps "GE-VIII" → the requirement_group used in major_requirements
const GE_LABEL_TO_GROUP: Record<string, string> = {
  "GE-II":  "GE_II",
  "GE-III": "GE_III",
  "GE-IV":  "GE_IV",
  "GE-Va":  "GE_Va",
  "GE-Vb":  "GE_Vb",
  "GE-VI":  "GE_VI",
  "GE-VII": "GE_VII",
  "GE-VIII":"GE_VIII",
  "Entry Level Writing": "GE_I_WRITING_LD",
};

function parseGEGroups(requirementSatisfied: string | null): string[] {
  if (!requirementSatisfied) return [];
  return requirementSatisfied
    .split(/,\s*/)
    .map((s) => GE_LABEL_TO_GROUP[s.trim()] ?? null)
    .filter(Boolean) as string[];
}

/** POST { ap_scores: { "AP Calculus AB": 4, ... } }
 *  Returns:
 *    courses: string[]  — UCI course IDs where ap_score <= user score
 *    ge_groups: string[] — requirement_group IDs for GEs directly satisfied by AP */
export async function POST(req: Request) {
  const { ap_scores } = await req.json() as { ap_scores: Record<string, number> };
  const examNames = Object.keys(ap_scores ?? {});
  if (examNames.length === 0) return NextResponse.json({ courses: [], ge_groups: [] });

  const supabase = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
  );

  const { data, error } = await supabase
    .from("ap_credits")
    .select("ap_course_name, ap_score, course_equivalencies, requirement_satisfied")
    .in("ap_course_name", examNames);

  if (error) return NextResponse.json({ error: error.message }, { status: 500 });

  const courses: string[] = [];
  const seenCourses = new Set<string>();
  const geGroups = new Set<string>();

  for (const row of data ?? []) {
    const userScore = ap_scores[row.ap_course_name];
    if (userScore === undefined || row.ap_score > userScore) continue;

    for (const cid of row.course_equivalencies ?? []) {
      if (!seenCourses.has(cid)) { seenCourses.add(cid); courses.push(cid); }
    }

    for (const grp of parseGEGroups(row.requirement_satisfied)) {
      geGroups.add(grp);
    }
  }

  return NextResponse.json({ courses, ge_groups: [...geGroups] });
}
