import { createClient } from "@/lib/supabase/client";
import type { ReqGroup } from "@/lib/api/courses";

export interface MinorOption {
  minor_id: string;   // AnteaterAPI program id, e.g. "459"
  name: string;       // cleaned display name, e.g. "Information and Computer Science"
}

interface MinorRequirementRow {
  id: string;
  minor_id: string;
  requirement_id: string;
  requirement_type: "course" | "group" | "unit" | "marker";
  label: string;
  courses: string[] | null;
  courses_needed: number | null;
  parent_requirement_id: string | null;
  group_requirement_count: number | null;
  sort_order: number;
}

function cleanMinorName(raw: string): string {
  // "Minor in Information and Computer Science" → "Information and Computer Science"
  return raw.replace(/^Minor\s+in\s+/i, "").trim() || raw;
}

/** Fetch all minors with cleaned display names, sorted alphabetically. */
export async function fetchMinors(): Promise<MinorOption[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("minors")
    .select("id, name")
    .order("name");

  if (error) throw new Error(error.message);

  return (data ?? [])
    .map((r) => ({ minor_id: r.id as string, name: cleanMinorName(r.name as string) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * All requirement groups for a given minor, adapted into the same ReqGroup
 * shape the major/GE sidebar already consumes — so the existing classifyGroup
 * + BucketSection pooling works without modification.
 *
 * Mapping from minor_requirements rows:
 *   - top-level 'course' row  → one ReqGroup (courses + courses_needed as-is)
 *   - top-level 'group' row   → one pooled ReqGroup: courses = union of its
 *                               children's courses, courses_needed = the group's
 *                               group_requirement_count (the "pick N of these
 *                               sub-requirements" count)
 *   - 'unit' / 'marker' rows  → skipped (no enumerable courses to place)
 *   - rows with no courses    → skipped (narrative-only requirements)
 *
 * requirement_type is set to "required" so classifyGroup routes each row:
 *   pick-N pools (courses_needed < courses.length) → "Required Selections",
 *   all-required rows → lower/upper division by course number,
 *   label keywords (elective/additional/…) → "Electives".
 */
export async function fetchMinorRequirements(minor_id: string): Promise<ReqGroup[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("minor_requirements")
    .select(
      "id, minor_id, requirement_id, requirement_type, label, courses, courses_needed, parent_requirement_id, group_requirement_count, sort_order",
    )
    .eq("minor_id", minor_id)
    .order("sort_order");

  if (error) throw new Error(error.message);
  const rows = (data ?? []) as MinorRequirementRow[];

  // Index children by their parent requirement_id
  const childrenByParent = new Map<string, MinorRequirementRow[]>();
  for (const r of rows) {
    if (r.parent_requirement_id) {
      const list = childrenByParent.get(r.parent_requirement_id) ?? [];
      list.push(r);
      childrenByParent.set(r.parent_requirement_id, list);
    }
  }

  const result: ReqGroup[] = [];
  let key = 0;

  for (const row of rows) {
    if (row.parent_requirement_id) continue; // children absorbed into their group

    if (row.requirement_type === "course") {
      const courses = row.courses ?? [];
      if (courses.length === 0) continue; // narrative-only, nothing to pool
      result.push({
        id: key++,
        group_name: row.label,
        requirement_type: "required",
        courses: [...new Set(courses)],
        courses_needed: row.courses_needed ?? 1,
        waivable: false,
      });
    } else if (row.requirement_type === "group") {
      // Pool all descendant courses into a single selectable group
      const children = childrenByParent.get(row.requirement_id) ?? [];
      const pooled = [...new Set(children.flatMap((c) => c.courses ?? []))];
      if (pooled.length === 0) continue;
      result.push({
        id: key++,
        group_name: row.label,
        requirement_type: "required",
        courses: pooled,
        courses_needed: row.group_requirement_count ?? 1,
        waivable: false,
      });
    }
    // 'unit' / 'marker' rows have no courses — skipped
  }

  return result;
}
