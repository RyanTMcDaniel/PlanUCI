import { createClient } from "@supabase/supabase-js";
import type { NextRequest } from "next/server";

function adminClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { auth: { persistSession: false } }
  );
}

function cleanDisplayName(raw: string | null, fallback: string): string {
  if (!raw) return fallback;
  return raw.replace(/^(Major|Minor|Program|Bachelor of (Science|Arts)|B\.[SA]\.?)\s+in\s+/i, "").trim() || fallback;
}

export async function GET(_req: NextRequest) {
  const supabase = adminClient();

  // Paginate: Supabase caps single queries at 1000 rows; major_requirements has 2685+ rows.
  const PAGE = 1000;
  const allRows: { major_id: string; major_name: string | null }[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from("major_requirements")
      .select("major_id, major_name")
      .neq("major_id", "ALL_MAJORS")
      .range(from, from + PAGE - 1);

    if (error) return Response.json({ error: error.message }, { status: 500 });
    if (!data || data.length === 0) break;
    allRows.push(...data);
    if (data.length < PAGE) break;
  }

  // Deduplicate by major_id, clean display name
  const seen = new Map<string, string>();
  for (const row of allRows) {
    if (!seen.has(row.major_id)) {
      seen.set(row.major_id, cleanDisplayName(row.major_name, row.major_id));
    }
  }

  const majors = Array.from(seen.entries())
    .map(([major_id, display_name]) => ({ major_id, display_name }))
    .sort((a, b) => a.display_name.localeCompare(b.display_name));

  return Response.json({ majors });
}
