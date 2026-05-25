import { createClient } from "@supabase/supabase-js";
import type { NextRequest } from "next/server";

function adminClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { auth: { persistSession: false } }
  );
}

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ major_id: string }> }
) {
  const { major_id } = await params;
  const supabase = adminClient();

  const { data, error } = await supabase
    .from("major_requirements")
    .select("id, group_name, requirement_type, courses, courses_needed, waivable")
    .eq("major_id", major_id);

  if (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }

  return Response.json({ requirements: data ?? [] });
}
