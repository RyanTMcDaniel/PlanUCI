import { createClient } from "@/lib/supabase/client";

// ── Serialized plan shape stored in plan_data jsonb ──────────────────────────

export interface PlanData {
  plannedCourses:      Record<string, string[]>;
  selectedMajorId:     string;
  selectedDisplayName: string;
  gradQuarter:         string;
  maxUnits:            number;
  lockedCourses:       string[];          // Set<string> serialized as array
  apScores:            Record<string, number>;
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
