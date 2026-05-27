import { createClient } from "@/lib/supabase/client";

export interface ReqGroup {
  id: number;
  group_name: string;
  requirement_type: "required" | "elective" | "GE";
  courses: string[];
  courses_needed: number;
  waivable: boolean;
}

export interface CourseDetail {
  id: string;
  title: string | null;
  min_units: number | null;
  description: string | null;
  course_level: string | null;
  terms: string[] | null;
  avg_gpa: number | null;
}

// Extract course IDs embedded in group_name strings like "POLSCI 192A" or
// "POLSCI 192B Or 107 Or 195". Used as fallback when courses array is empty.
function parseCoursesFromGroupName(groupName: string): string[] {
  const result: string[] = [];
  // Match DEPT followed by one or more nums separated by "Or" / ","
  const re = /\b([A-Z][A-Z&/]{1,})\s+(\d[A-Z0-9]*(?:\s*(?:[Oo]r|,)\s*\d[A-Z0-9]*)*)\b/g;
  let m;
  while ((m = re.exec(groupName)) !== null) {
    const dept = m[1];
    for (const num of m[2].split(/\s*(?:[Oo]r|,)\s*/)) {
      if (num.trim()) result.push(dept + num.trim());
    }
  }
  return result;
}

async function fetchReqRowsForId(supabase: ReturnType<typeof createClient>, major_id: string): Promise<ReqGroup[]> {
  const PAGE = 1000;
  const rows: ReqGroup[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from("major_requirements")
      .select("id, group_name, requirement_type, courses, courses_needed, waivable")
      .eq("major_id", major_id)
      .range(from, from + PAGE - 1);
    if (error) throw new Error(error.message);
    if (!data || data.length === 0) break;
    rows.push(...(data as ReqGroup[]));
    if (data.length < PAGE) break;
  }
  return rows;
}

/** All requirement groups for a given major. Optionally merges parent program rows
 *  (for specialization programs where the parent fetch captures spec-specific groups). */
export async function fetchMajorRequirements(major_id: string, parent_id?: string): Promise<ReqGroup[]> {
  const supabase = createClient();

  const [specRows, parentRows] = await Promise.all([
    fetchReqRowsForId(supabase, major_id),
    parent_id && parent_id !== major_id ? fetchReqRowsForId(supabase, parent_id) : Promise.resolve([]),
  ]);

  const rawRows = [...specRows, ...parentRows];

  // Merge rows that share a group_name, deduplicating courses
  const grouped = new Map<string, ReqGroup>();
  for (const row of rawRows) {
    const existing = grouped.get(row.group_name);
    if (existing) {
      existing.courses = [...new Set([...existing.courses, ...row.courses])];
      existing.courses_needed = Math.max(existing.courses_needed, row.courses_needed);
    } else {
      grouped.set(row.group_name, { ...row, courses: [...row.courses] });
    }
  }

  // For groups with no courses, attempt to parse IDs from the group name
  const result = [...grouped.values()];
  for (const req of result) {
    if (req.courses.length === 0) {
      req.courses = parseCoursesFromGroupName(req.group_name);
    }
  }

  return result;
}

/** Course details for a list of IDs. Batches in 100s to stay under PostgREST URL limits. */
export async function fetchCourseDetails(ids: string[]): Promise<CourseDetail[]> {
  if (ids.length === 0) return [];
  const supabase = createClient();
  const BATCH = 100;
  const results: CourseDetail[] = [];

  for (let i = 0; i < ids.length; i += BATCH) {
    const { data, error } = await supabase
      .from("courses")
      .select("id, title, min_units, description, course_level, terms, avg_gpa")
      .in("id", ids.slice(i, i + BATCH));

    if (error) throw new Error(error.message);
    if (data) results.push(...(data as CourseDetail[]));
  }

  return results;
}

/** All 11 university-wide GE requirement groups. */
export async function fetchGERequirements(): Promise<ReqGroup[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("major_requirements")
    .select("id, group_name, requirement_type, courses, courses_needed, waivable")
    .eq("major_id", "ALL_MAJORS");

  if (error) throw new Error(error.message);
  return (data ?? []) as ReqGroup[];
}
