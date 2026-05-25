import { createClient } from "@supabase/supabase-js";
import type { NextRequest } from "next/server";

function adminClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { auth: { persistSession: false } }
  );
}

export async function POST(req: NextRequest) {
  let ids: string[];
  try {
    const body = await req.json();
    ids = body.ids;
    if (!Array.isArray(ids)) throw new Error("ids must be an array");
  } catch {
    return Response.json({ error: "Invalid request body" }, { status: 400 });
  }

  if (ids.length === 0) {
    return Response.json({ courses: [] });
  }

  const supabase = adminClient();
  // 100-ID batches keep PostgREST URL well under limits (~2KB per request)
  const BATCH = 100;
  const results: unknown[] = [];

  for (let i = 0; i < ids.length; i += BATCH) {
    const { data, error } = await supabase
      .from("courses")
      .select("id, title, min_units, description")
      .in("id", ids.slice(i, i + BATCH));

    if (error) {
      return Response.json({ error: error.message }, { status: 500 });
    }
    if (data) results.push(...data);
  }

  return Response.json({ courses: results });
}
