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
}

/** All requirement groups for a given major_id. Paginates automatically. */
export async function fetchMajorRequirements(major_id: string): Promise<ReqGroup[]> {
  const supabase = createClient();
  const PAGE = 1000;
  const allRows: ReqGroup[] = [];

  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from("major_requirements")
      .select("id, group_name, requirement_type, courses, courses_needed, waivable")
      .eq("major_id", major_id)
      .range(from, from + PAGE - 1);

    if (error) throw new Error(error.message);
    if (!data || data.length === 0) break;
    allRows.push(...(data as ReqGroup[]));
    if (data.length < PAGE) break;
  }

  return allRows;
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
      .select("id, title, min_units, description")
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
