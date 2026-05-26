import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

// POST { ids: string[] } → { gpas: Record<string, number> }
// Queries each course individually to avoid Supabase's 1000-row default cap,
// which silently truncates multi-course IN() queries for high-volume departments.
export async function POST(req: Request) {
  const body = await req.json();
  const ids: string[] = Array.isArray(body?.ids) ? body.ids : [];
  if (ids.length === 0) return NextResponse.json({ gpas: {} });

  const supabase = serviceClient();
  const gpas: Record<string, number> = {};

  // Query concurrently in groups of 10 to stay fast while avoiding row-cap issues
  const CONCURRENCY = 10;
  for (let i = 0; i < ids.length; i += CONCURRENCY) {
    const slice = ids.slice(i, i + CONCURRENCY);
    await Promise.all(
      slice.map(async (courseId) => {
        const { data, error } = await supabase
          .from("grade_distributions")
          .select("average_gpa")
          .eq("course_id", courseId)
          .not("average_gpa", "is", null);

        if (error || !data || data.length === 0) return;
        const vals = data.map((r) => r.average_gpa as number);
        gpas[courseId] = vals.reduce((a, b) => a + b, 0) / vals.length;
      }),
    );
  }

  return NextResponse.json({ gpas });
}
