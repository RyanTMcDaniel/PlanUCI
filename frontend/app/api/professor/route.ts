import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const courseId = searchParams.get("id");
  if (!courseId) return NextResponse.json({ professor: null }, { status: 400 });

  const supabase = serviceClient();

  // Step 1: instructors who taught this course
  const { data: ci } = await supabase
    .from("course_instructors")
    .select("ucinetid")
    .eq("course_id", courseId);

  if (!ci || ci.length === 0) return NextResponse.json({ professor: null });

  const ucinetids = ci.map((r) => r.ucinetid);

  // Step 2: top-rated instructor with meaningful review count
  const { data: reviews } = await supabase
    .from("rmp_reviews")
    .select("ucinetid, overall_rating, difficulty_rating, num_ratings, sentiment_label")
    .in("ucinetid", ucinetids)
    .not("overall_rating", "is", null)
    .gte("num_ratings", 3)
    .order("overall_rating", { ascending: false })
    .limit(1);

  if (!reviews || reviews.length === 0) return NextResponse.json({ professor: null });

  const top = reviews[0];

  // Step 3: instructor name
  const { data: instructor } = await supabase
    .from("instructors")
    .select("name")
    .eq("ucinetid", top.ucinetid)
    .single();

  return NextResponse.json({
    professor: {
      name: instructor?.name ?? top.ucinetid,
      overall_rating: top.overall_rating as number,
      difficulty_rating: top.difficulty_rating as number,
      num_ratings: top.num_ratings as number,
      sentiment_label: top.sentiment_label as string | null,
    },
  });
}
