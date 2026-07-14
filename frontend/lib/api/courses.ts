import { createClient } from "@/lib/supabase/client";

export interface ReqGroup {
  id: number;
  group_name: string;
  requirement_group?: string;   // Anteater API requirement_group ID (e.g. "GE_VIII")
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
  // Authoritative per-course GE designations, e.g. ["GE III: Social & Behavioral Sciences"].
  ge_list: string[] | null;
  // Enrollment restriction text, e.g. "Anthropology majors only". Used to exclude
  // major-specific writing seminars from the generic GE autofill candidate pool.
  restriction: string | null;
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

/** Resolve the catalogue major_id and spec slug for a given Anteater API major_id.
 *  The catalogue scraper uses its own IDs (e.g. "BS-0K6C") that may differ from
 *  the Anteater API IDs (e.g. "BS-0K6A").  Resolves by finding which sibling
 *  major_id (same major_name) has catalogue rows, and deriving the spec slug
 *  from specialization_name ("Data Science" → "data-science"). */
async function resolveCatalogueIds(
  supabase: ReturnType<typeof createClient>,
  major_id: string,
): Promise<{ catMajorId: string; catSpecId: string | null }> {
  // Fast path: direct match (works for most non-spec majors)
  const { data: direct } = await supabase
    .from("catalogue_requirements")
    .select("major_id")
    .eq("major_id", major_id)
    .limit(1);
  if (direct?.length) return { catMajorId: major_id, catSpecId: null };

  // Look up major_name + specialization_name to find siblings
  const { data: meta } = await supabase
    .from("major_requirements")
    .select("major_name, specialization_name")
    .eq("major_id", major_id)
    .limit(1);
  if (!meta?.length) return { catMajorId: major_id, catSpecId: null };

  const majorName = (meta[0].major_name ?? "").trim();
  const specName  = (meta[0].specialization_name ?? "").trim();
  const catSpecId = specName ? specName.toLowerCase().replace(/\s+/g, "-") : null;

  // Find all sibling major_ids sharing the same program name
  const { data: siblings } = await supabase
    .from("major_requirements")
    .select("major_id")
    .eq("major_name", majorName);
  const siblingIds = [...new Set((siblings ?? []).map((r) => r.major_id))];

  // Single probe: find which sibling has catalogue rows
  const { data: probe } = await supabase
    .from("catalogue_requirements")
    .select("major_id")
    .in("major_id", siblingIds)
    .limit(1);
  const catMajorId = probe?.[0]?.major_id ?? major_id;

  return { catMajorId, catSpecId };
}

/** Fetch core catalogue rows (specialization_id IS NULL) for a major.
 *  These supplement or override the API rows with scraped-from-catalogue data. */
async function fetchCatalogueRows(
  supabase: ReturnType<typeof createClient>,
  major_id: string,
  specialization_id?: string | null,
): Promise<ReqGroup[]> {
  const { catMajorId, catSpecId } = await resolveCatalogueIds(supabase, major_id);
  const resolvedSpecId = specialization_id ?? catSpecId;

  const { data: coreData } = await supabase
    .from("catalogue_requirements")
    .select("group_name, requirement_type, courses, courses_needed")
    .eq("major_id", catMajorId)
    .is("specialization_id", null);

  const rows: ReqGroup[] = (coreData ?? []).map((r) => ({
    id: 0,
    group_name: r.group_name,
    requirement_type: r.requirement_type as ReqGroup["requirement_type"],
    courses: r.courses ?? [],
    courses_needed: r.courses_needed ?? 1,
    waivable: false,
  }));

  if (resolvedSpecId) {
    const { data: specData } = await supabase
      .from("catalogue_requirements")
      .select("group_name, requirement_type, courses, courses_needed")
      .eq("major_id", catMajorId)
      .eq("specialization_id", resolvedSpecId);

    for (const r of specData ?? []) {
      rows.push({
        id: 0,
        group_name: r.group_name,
        requirement_type: r.requirement_type as ReqGroup["requirement_type"],
        courses: r.courses ?? [],
        courses_needed: r.courses_needed ?? 1,
        waivable: false,
      });
    }
  }

  return rows;
}

/** Merge two arrays of ReqGroup by group_name.  Later array takes precedence. */
function mergeReqGroups(base: ReqGroup[], override: ReqGroup[]): ReqGroup[] {
  const map = new Map<string, ReqGroup>();
  for (const row of base) map.set(row.group_name, { ...row, courses: [...row.courses] });
  for (const row of override) {
    // Catalogue row wins: replace any existing API row with the same group_name
    map.set(row.group_name, { ...row, courses: [...row.courses] });
  }
  return [...map.values()];
}

/** All requirement groups for a given major. Merges:
 *  1. API rows from major_requirements (parent + specialization)
 *  2. Catalogue rows from catalogue_requirements (catalogue takes precedence per group_name)
 */
export async function fetchMajorRequirements(
  major_id: string,
  parent_id?: string,
  specialization_id?: string | null,
): Promise<ReqGroup[]> {
  const supabase = createClient();

  const [specRows, parentRows, catalogueRows] = await Promise.all([
    fetchReqRowsForId(supabase, major_id),
    parent_id && parent_id !== major_id ? fetchReqRowsForId(supabase, parent_id) : Promise.resolve([]),
    fetchCatalogueRows(supabase, major_id, specialization_id).catch(() => [] as ReqGroup[]),
  ]);

  // Start with API rows (spec + parent merge)
  const apiGrouped = new Map<string, ReqGroup>();
  for (const row of [...specRows, ...parentRows]) {
    const existing = apiGrouped.get(row.group_name);
    if (existing) {
      existing.courses = [...new Set([...existing.courses, ...row.courses])];
      existing.courses_needed = Math.max(existing.courses_needed, row.courses_needed);
    } else {
      apiGrouped.set(row.group_name, { ...row, courses: [...row.courses] });
    }
  }

  // Apply catalogue override
  const merged = mergeReqGroups([...apiGrouped.values()], catalogueRows);

  // For groups with no courses, attempt to parse IDs from the group name
  for (const req of merged) {
    if (req.courses.length === 0) {
      req.courses = parseCoursesFromGroupName(req.group_name);
    }
  }

  return merged;
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
      .select("id, title, min_units, description, course_level, terms, avg_gpa, ge_list, restriction")
      .in("id", ids.slice(i, i + BATCH));

    if (error) throw new Error(error.message);
    if (data) results.push(...(data as CourseDetail[]));
  }

  return results;
}

// AND/OR/NOT prerequisite tree (same shape the backend optimizer evaluates).
// Leaves: { prereqType: "course", courseId, coreq? } | { prereqType: "exam", ... }.
export type PrereqTree = Record<string, unknown>;

/** Pull stored prerequisite_tree JSON for the given courses straight from the
 *  `courses` table. Returns a map keyed by the raw course id (only courses that
 *  actually have a tree are included). Used for frontend GE/Minor placement so
 *  selected courses land in a prereq-valid quarter without the major optimizer. */
export async function fetchPrereqTrees(ids: string[]): Promise<Record<string, PrereqTree>> {
  if (ids.length === 0) return {};
  const supabase = createClient();
  const BATCH = 100;
  const out: Record<string, PrereqTree> = {};

  for (let i = 0; i < ids.length; i += BATCH) {
    const { data, error } = await supabase
      .from("courses")
      .select("id, prerequisite_tree")
      .in("id", ids.slice(i, i + BATCH));

    if (error) throw new Error(error.message);
    for (const r of data ?? []) {
      const tree = (r as { id: string; prerequisite_tree: PrereqTree | null }).prerequisite_tree;
      if (tree) out[(r as { id: string }).id] = tree;
    }
  }

  return out;
}

/** Pull the raw `corequisites` text for the given courses straight from the
 *  `courses` table. Returns a map keyed by raw course id (only courses with a
 *  non-empty corequisites string are included). The corequisites field is the
 *  authoritative store for true bidirectional coreqs (lecture↔lab); the
 *  prerequisite_tree stores coreq edges one-directionally (and not at all for
 *  lab rows), so the field is the only reliable source of mutual pairs. */
export async function fetchCorequisites(ids: string[]): Promise<Record<string, string>> {
  if (ids.length === 0) return {};
  const supabase = createClient();
  const BATCH = 100;
  const out: Record<string, string> = {};

  for (let i = 0; i < ids.length; i += BATCH) {
    const { data, error } = await supabase
      .from("courses")
      .select("id, corequisites")
      .in("id", ids.slice(i, i + BATCH));

    if (error) throw new Error(error.message);
    for (const r of data ?? []) {
      const text = (r as { id: string; corequisites: string | null }).corequisites;
      if (text && text.trim()) out[(r as { id: string }).id] = text;
    }
  }

  return out;
}

/** Distinct AP exam names sorted alphabetically (for the AP scores input UI).
 *  Routed through /api/ap-exams to use the service key (ap_credits has RLS). */
export async function fetchApExamNames(): Promise<string[]> {
  const res = await fetch("/api/ap-exams");
  if (!res.ok) throw new Error(`Failed to fetch AP exams: ${res.status}`);
  const json = await res.json();
  return json.names as string[];
}

export interface ApCreditResult {
  /** UCI course IDs satisfied by AP credit (used to mark individual courses covered). */
  courses: Set<string>;
  /** requirement_group IDs for GE categories directly satisfied by AP exam score. */
  geGroups: Set<string>;
}

/**
 * Given a map of { "AP Calculus AB": 4, ... }, resolve to:
 *   - courses: UCI course IDs satisfied by AP equivalencies
 *   - geGroups: requirement_group IDs for GEs directly satisfied by the exam
 * Routed through /api/ap-credits to use the service key (ap_credits has RLS).
 */
export async function resolveApCredits(
  apScores: Record<string, number>,
): Promise<ApCreditResult> {
  const empty = { courses: new Set<string>(), geGroups: new Set<string>() };
  if (Object.keys(apScores).length === 0) return empty;
  const res = await fetch("/api/ap-credits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ap_scores: apScores }),
  });
  if (!res.ok) return empty;
  const json = await res.json();
  return {
    courses:  new Set<string>(json.courses  ?? []),
    geGroups: new Set<string>(json.ge_groups ?? []),
  };
}

/** All 11 university-wide GE requirement groups. */
export async function fetchGERequirements(): Promise<ReqGroup[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("major_requirements")
    .select("id, requirement_group, group_name, requirement_type, courses, courses_needed, waivable")
    .eq("major_id", "ALL_MAJORS");

  if (error) throw new Error(error.message);
  return (data ?? []) as ReqGroup[];
}
