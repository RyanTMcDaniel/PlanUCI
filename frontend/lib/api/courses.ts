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

export async function fetchMajorRequirements(major_id: string): Promise<ReqGroup[]> {
  const res = await fetch(`/api/requirements/${encodeURIComponent(major_id)}`);
  if (!res.ok) throw new Error(`Failed to load requirements (${res.status})`);
  const { requirements, error } = await res.json();
  if (error) throw new Error(error);
  return requirements as ReqGroup[];
}

export async function fetchCourseDetails(ids: string[]): Promise<CourseDetail[]> {
  if (ids.length === 0) return [];
  const res = await fetch("/api/courses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!res.ok) throw new Error(`Failed to load course details (${res.status})`);
  const { courses, error } = await res.json();
  if (error) throw new Error(error);
  return courses as CourseDetail[];
}

export async function fetchGERequirements(): Promise<ReqGroup[]> {
  const res = await fetch("/api/ge");
  if (!res.ok) throw new Error(`Failed to load GE requirements (${res.status})`);
  const { requirements, error } = await res.json();
  if (error) throw new Error(error);
  return requirements as ReqGroup[];
}
