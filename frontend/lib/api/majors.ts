export interface MajorOption {
  major_id: string;
  display_name: string;
}

export async function fetchMajors(): Promise<MajorOption[]> {
  const res = await fetch("/api/majors");
  if (!res.ok) throw new Error(`Failed to fetch majors (${res.status})`);
  const { majors, error } = await res.json();
  if (error) throw new Error(error);
  return majors as MajorOption[];
}
