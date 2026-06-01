import { createClient } from "@/lib/supabase/client";

export interface MajorOption {
  major_id: string;
  display_name: string;           // cleaned major_name (used for grouping)
  specialization_name: string | null; // null = parent/standalone, set = specialization row
}

function cleanDisplayName(raw: string | null, fallback: string): string {
  if (!raw) return fallback;
  return (
    raw.replace(/^(Major|Minor|Program|Bachelor of (Science|Arts)|B\.[SA]\.?)\s+in\s+/i, "").trim() ||
    fallback
  );
}

/** Fetch all majors and specializations with cleaned display names. */
export async function fetchMajors(): Promise<MajorOption[]> {
  const supabase = createClient();
  const PAGE = 1000;
  const allRows: { major_id: string; major_name: string | null; specialization_name: string | null }[] = [];

  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from("major_requirements")
      .select("major_id, major_name, specialization_name")
      .neq("major_id", "ALL_MAJORS")
      .range(from, from + PAGE - 1);

    if (error) throw new Error(error.message);
    if (!data || data.length === 0) break;
    allRows.push(...data);
    if (data.length < PAGE) break;
  }

  // Deduplicate by major_id, keeping the first occurrence
  const seen = new Map<string, MajorOption>();
  for (const row of allRows) {
    if (seen.has(row.major_id)) continue;
    seen.set(row.major_id, {
      major_id: row.major_id,
      display_name: cleanDisplayName(row.major_name, row.major_id),
      specialization_name: row.specialization_name ?? null,
    });
  }

  return Array.from(seen.values()).sort((a, b) => {
    const cmp = a.display_name.localeCompare(b.display_name);
    if (cmp !== 0) return cmp;
    // specs after their parent, sorted by spec name
    if (!a.specialization_name && b.specialization_name) return -1;
    if (a.specialization_name && !b.specialization_name) return 1;
    return (a.specialization_name ?? "").localeCompare(b.specialization_name ?? "");
  });
}
