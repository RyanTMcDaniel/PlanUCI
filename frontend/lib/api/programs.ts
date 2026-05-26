// Module-level cache — fetched once per browser session
let cached: Map<string, string> | null = null;

/** Fetch program display names from AnteaterAPI. Returns empty map on failure (rate limit, etc.) */
export async function fetchProgramNames(): Promise<Map<string, string>> {
  if (cached) return cached;
  try {
    const res = await fetch("https://anteaterapi.com/v2/rest/programs");
    if (!res.ok) throw new Error(`${res.status}`);
    const json = await res.json();
    const map = new Map<string, string>();
    for (const p of (json.data ?? []) as { id?: string; name?: string }[]) {
      if (p.id && p.name) map.set(p.id, p.name);
    }
    cached = map;
    return map;
  } catch {
    cached = new Map();
    return cached;
  }
}
