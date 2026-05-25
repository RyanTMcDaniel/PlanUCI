import { createClient } from "@/lib/supabase/client";

export interface MajorOption {
  major_id: string;
  display_name: string;
}

function cleanDisplayName(raw: string | null, fallback: string): string {
  if (!raw) return fallback;
  return (
    raw.replace(/^(Major|Minor|Program|Bachelor of (Science|Arts)|B\.[SA]\.?)\s+in\s+/i, "").trim() ||
    fallback
  );
}

/** Fetch all distinct majors with cleaned display names. Paginates automatically. */
export async function fetchMajors(): Promise<MajorOption[]> {
  const supabase = createClient();
  const PAGE = 1000;
  const allRows: { major_id: string; major_name: string | null }[] = [];

  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from("major_requirements")
      .select("major_id, major_name")
      .neq("major_id", "ALL_MAJORS")
      .range(from, from + PAGE - 1);

    if (error) throw new Error(error.message);
    if (!data || data.length === 0) break;
    allRows.push(...data);
    if (data.length < PAGE) break;
  }

  const seen = new Map<string, string>();
  for (const row of allRows) {
    if (!seen.has(row.major_id)) {
      seen.set(row.major_id, cleanDisplayName(row.major_name, row.major_id));
    }
  }

  return Array.from(seen.entries())
    .map(([major_id, display_name]) => ({ major_id, display_name }))
    .sort((a, b) => a.display_name.localeCompare(b.display_name));
}
