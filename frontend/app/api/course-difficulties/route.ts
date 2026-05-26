import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

export async function POST(req: Request) {
  const body = await req.json();
  const ids: string[] = Array.isArray(body?.ids) ? body.ids : [];
  if (ids.length === 0) return NextResponse.json({ scores: {} });

  const BATCH = 200;
  const supabase = serviceClient();
  const scores: Record<string, number> = {};

  for (let i = 0; i < ids.length; i += BATCH) {
    const { data, error } = await supabase
      .from("course_features")
      .select("course_id, difficulty_score")
      .in("course_id", ids.slice(i, i + BATCH));

    if (error || !data) continue;
    for (const row of data) {
      if (row.course_id && row.difficulty_score != null) {
        scores[row.course_id as string] = row.difficulty_score as number;
      }
    }
  }

  return NextResponse.json({ scores });
}
