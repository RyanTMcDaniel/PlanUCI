import { createClient } from "@/lib/supabase/client";

// ── Serialized plan shape stored in plan_data jsonb ──────────────────────────

export interface PlanData {
  plannedCourses:      Record<string, string[]>;
  selectedMajorId:     string;
  selectedDisplayName: string;
  selectedMinorId?:    string;            // optional — absent on plans saved before minors
  numYears?:           number;            // structural year count (default 4); absent on older plans
  gradQuarter?:        string;            // derived (Spring of last year); kept for display/optimizer

  maxUnits:            number;
  lockedCourses:       string[];          // Set<string> serialized as array
  apScores:            Record<string, number>;
  languageReqSatisfied?: boolean;         // GE VI satisfied outside a course; absent on older plans
  summerYears:         number[];          // Set<number> serialized as array
  savedAt:             string;            // ISO timestamp
}

const PLAN_NAME = "My Plan";

// ── Save ──────────────────────────────────────────────────────────────────────

export async function savePlan(data: Omit<PlanData, "savedAt">): Promise<void> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return;

  const payload: PlanData = { ...data, savedAt: new Date().toISOString() };

  // Look up existing row for this user
  const { data: existing } = await supabase
    .from("saved_degree_plans")
    .select("id")
    .eq("user_id", user.id)
    .eq("name", PLAN_NAME)
    .limit(1)
    .maybeSingle();

  if (existing?.id) {
    await supabase
      .from("saved_degree_plans")
      .update({ plan_data: payload, updated_at: payload.savedAt })
      .eq("id", existing.id);
  } else {
    await supabase.from("saved_degree_plans").insert({
      user_id:    user.id,
      name:       PLAN_NAME,
      plan_data:  payload,
      created_at: payload.savedAt,
      updated_at: payload.savedAt,
    });
  }
}

// ── Load ──────────────────────────────────────────────────────────────────────

export async function loadPlan(): Promise<PlanData | null> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return null;

  const { data } = await supabase
    .from("saved_degree_plans")
    .select("plan_data")
    .eq("user_id", user.id)
    .eq("name", PLAN_NAME)
    .limit(1)
    .maybeSingle();

  return (data?.plan_data as PlanData) ?? null;
}

// ── Delete ────────────────────────────────────────────────────────────────────

export async function deletePlan(): Promise<void> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return;
  // Only clear the autosave draft — named saved versions are preserved.
  await supabase
    .from("saved_degree_plans")
    .delete()
    .eq("user_id", user.id)
    .eq("name", PLAN_NAME);
}

// ── List (for /plans page) ────────────────────────────────────────────────────

export interface SavedPlanMeta {
  id:         number;
  name:       string;
  plan_data:  PlanData;
  updated_at: string | null;
}

export async function listPlans(): Promise<SavedPlanMeta[]> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return [];

  const { data } = await supabase
    .from("saved_degree_plans")
    .select("id, name, plan_data, updated_at")
    .eq("user_id", user.id)
    .order("updated_at", { ascending: false });

  return (data ?? []) as SavedPlanMeta[];
}

// ── Named saved versions (cap 5, excludes the "My Plan" autosave draft) ──────

export const MAX_SAVED_PLANS = 5;

export type SaveNamedResult =
  | { ok: true; plan: SavedPlanMeta }
  | { ok: false; reason: "signed_out" | "cap_reached" | "error" };

/** Explicit user-saved versions, newest first (excludes the autosave draft). */
export async function listNamedPlans(): Promise<SavedPlanMeta[]> {
  const all = await listPlans();
  return all.filter((p) => p.name !== PLAN_NAME);
}

/**
 * Save the current schedule under `name`.  An existing version with the same
 * name is updated in place (free); a new name inserts a new version, capped at
 * MAX_SAVED_PLANS named versions per user.
 */
export async function saveNamedPlan(
  name: string,
  data: Omit<PlanData, "savedAt">,
): Promise<SaveNamedResult> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { ok: false, reason: "signed_out" };

  const cleanName = name.trim().slice(0, 80) || "Untitled plan";
  if (cleanName === PLAN_NAME) return { ok: false, reason: "error" };

  const payload: PlanData = { ...data, savedAt: new Date().toISOString() };

  // Same-name version exists → update it (does not count against the cap).
  const { data: existing } = await supabase
    .from("saved_degree_plans")
    .select("id")
    .eq("user_id", user.id)
    .eq("name", cleanName)
    .limit(1)
    .maybeSingle();

  if (existing?.id) {
    const { data: row, error } = await supabase
      .from("saved_degree_plans")
      .update({ plan_data: payload, updated_at: payload.savedAt })
      .eq("id", existing.id)
      .select("id, name, plan_data, updated_at")
      .single();
    if (error || !row) return { ok: false, reason: "error" };
    return { ok: true, plan: row as SavedPlanMeta };
  }

  // New version — enforce the cap on named versions only.
  const named = await listNamedPlans();
  if (named.length >= MAX_SAVED_PLANS) return { ok: false, reason: "cap_reached" };

  const { data: row, error } = await supabase
    .from("saved_degree_plans")
    .insert({
      user_id:    user.id,
      name:       cleanName,
      plan_data:  payload,
      created_at: payload.savedAt,
      updated_at: payload.savedAt,
    })
    .select("id, name, plan_data, updated_at")
    .single();
  if (error || !row) return { ok: false, reason: "error" };
  return { ok: true, plan: row as SavedPlanMeta };
}

/** Delete one saved version by id (scoped to the signed-in user). */
export async function deletePlanById(id: number): Promise<void> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return;
  await supabase
    .from("saved_degree_plans")
    .delete()
    .eq("id", id)
    .eq("user_id", user.id);
}

/** Load one saved version's plan_data by id (scoped to the signed-in user). */
export async function loadPlanById(id: number): Promise<PlanData | null> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return null;
  const { data } = await supabase
    .from("saved_degree_plans")
    .select("plan_data")
    .eq("id", id)
    .eq("user_id", user.id)
    .maybeSingle();
  return (data?.plan_data as PlanData) ?? null;
}

// ── User profile sync ─────────────────────────────────────────────────────────

export async function syncUserProfile(opts: {
  majorCode:             string;
  gradQuarter:           string;
  preferredMaxUnits:     number;
}): Promise<void> {
  const supabase = createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return;

  const [targetYear, targetQuarter] = opts.gradQuarter.split("_");
  await supabase.from("user_profiles").upsert(
    {
      id:                       user.id,
      major_code:               opts.majorCode || null,
      graduation_target_year:   targetYear ? parseInt(targetYear) : null,
      graduation_target_quarter: targetQuarter ?? null,
      preferred_max_units:      opts.preferredMaxUnits,
      updated_at:               new Date().toISOString(),
    },
    { onConflict: "id" },
  );
}
