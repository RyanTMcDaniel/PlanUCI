import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

export async function GET() {
  const supabase = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
  );

  const { data, error } = await supabase
    .from("ap_credits")
    .select("ap_course_name")
    .order("ap_course_name");

  if (error) return NextResponse.json({ error: error.message }, { status: 500 });

  const seen = new Set<string>();
  const names: string[] = (data ?? [])
    .map((r) => r.ap_course_name as string)
    .filter((n) => !seen.has(n) && seen.add(n));

  return NextResponse.json({ names });
}
