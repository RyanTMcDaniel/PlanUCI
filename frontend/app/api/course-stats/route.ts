import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

// ── Handler ────────────────────────────────────────────────────────────────

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const courseId = searchParams.get("id");
  if (!courseId) {
    return NextResponse.json(
      { professor: null, difficulty_score: null, avg_gpa: null, prof_gpa: null },
      { status: 400 },
    );
  }

  const supabase = serviceClient();

  // Run difficulty, avg GPA, and instructor queries in parallel
  const [cfResult, gradeResult, ciResult] = await Promise.all([
    supabase
      .from("course_features")
      .select("difficulty_score")
      .eq("course_id", courseId)
      .maybeSingle(),
    supabase
      .from("grade_distributions")
      .select("average_gpa")
      .eq("course_id", courseId)
      .not("average_gpa", "is", null),
    supabase
      .from("course_instructors")
      .select("ucinetid")
      .eq("course_id", courseId),
  ]);

  const diffScore = (cfResult.data?.difficulty_score as number | null) ?? null;
  const grades = gradeResult.data ?? [];
  const avg_gpa =
    grades.length > 0
      ? grades.reduce((sum, r) => sum + (r.average_gpa as number), 0) / grades.length
      : null;

  const ci = ciResult.data ?? [];
  if (ci.length === 0) {
    return NextResponse.json({ professor: null, difficulty_score: diffScore, avg_gpa, prof_gpa: null });
  }

  const ucinetids = ci.map((r) => r.ucinetid);

  // Top-rated instructor by RMP overall rating
  const { data: reviews } = await supabase
    .from("rmp_reviews")
    .select("ucinetid, overall_rating, difficulty_rating, num_ratings, sentiment_label")
    .in("ucinetid", ucinetids)
    .not("overall_rating", "is", null)
    .gte("num_ratings", 3)
    .order("overall_rating", { ascending: false })
    .limit(1);

  if (!reviews || reviews.length === 0) {
    return NextResponse.json({ professor: null, difficulty_score: diffScore, avg_gpa, prof_gpa: null });
  }

  const top = reviews[0];
  const [instrResult, profGradeResult] = await Promise.all([
    supabase.from("instructors").select("name").eq("ucinetid", top.ucinetid).single(),
    supabase
      .from("grade_distributions")
      .select("average_gpa")
      .eq("course_id", courseId)
      .eq("ucinetid", top.ucinetid)
      .not("average_gpa", "is", null),
  ]);

  const profGrades = profGradeResult.data ?? [];
  const prof_gpa =
    profGrades.length > 0
      ? profGrades.reduce((sum, r) => sum + (r.average_gpa as number), 0) / profGrades.length
      : null;

  return NextResponse.json({
    professor: {
      name: instrResult.data?.name ?? top.ucinetid,
      overall_rating: top.overall_rating as number,
      difficulty_rating: top.difficulty_rating as number,
      num_ratings: top.num_ratings as number,
      sentiment_label: top.sentiment_label as string | null,
    },
    difficulty_score: diffScore,
    avg_gpa,
    prof_gpa,
  });
}
