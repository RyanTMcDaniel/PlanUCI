-- Run this in the Supabase SQL Editor (dashboard > SQL Editor > New query)

-- 1. course_features: course-level difficulty scores
CREATE TABLE IF NOT EXISTS public.course_features (
  course_id TEXT PRIMARY KEY,
  difficulty_score FLOAT NOT NULL
);

ALTER TABLE public.course_features ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read" ON public.course_features FOR SELECT TO anon USING (true);

-- 2. prof_course_features: per-instructor difficulty scores
CREATE TABLE IF NOT EXISTS public.prof_course_features (
  course_id       TEXT    NOT NULL,
  instructor_id   TEXT    NOT NULL,
  nlp_score       FLOAT,
  gpa_score       FLOAT,
  rmp_score       FLOAT,
  difficulty_score FLOAT  NOT NULL,
  sections_taught  INT,
  PRIMARY KEY (course_id, instructor_id)
);

ALTER TABLE public.prof_course_features ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read" ON public.prof_course_features FOR SELECT TO anon USING (true);

-- 1b / 2c. Per-score confidence, keyed on WHICH signals backed the score.
-- ~a third of the catalogue is scored by the NLP classifier alone (held-out macro
-- F1 0.576). The missingness calibration in build_features.py makes those scores
-- unbiased, but unbiased is not precise — a text-only guess must not look as
-- authoritative as a score corroborated by grade history and professor ratings.
-- high = nlp+gpa+rmp, medium = two signals, low = nlp only.
ALTER TABLE public.course_features      ADD COLUMN IF NOT EXISTS confidence      TEXT;
ALTER TABLE public.prof_course_features ADD COLUMN IF NOT EXISTS confidence      TEXT;
ALTER TABLE public.prof_course_features ADD COLUMN IF NOT EXISTS signals_present TEXT;

-- 2b. rmp_reviews provenance columns.
-- The original scrape stored ratings but NOT which RateMyProfessor record they came
-- from, which is how a single RMP professor came to be matched onto 80 different
-- ucinetids without anyone noticing. Persisting rmp_id + the RMP-side name and
-- department makes the match auditable and lets the UNIQUE constraint below enforce
-- what the pipeline previously only assumed: one RMP record → at most one instructor.
ALTER TABLE public.rmp_reviews ADD COLUMN IF NOT EXISTS rmp_id          BIGINT;
ALTER TABLE public.rmp_reviews ADD COLUMN IF NOT EXISTS rmp_first_name  TEXT;
ALTER TABLE public.rmp_reviews ADD COLUMN IF NOT EXISTS rmp_last_name   TEXT;
ALTER TABLE public.rmp_reviews ADD COLUMN IF NOT EXISTS rmp_department  TEXT;
ALTER TABLE public.rmp_reviews ADD COLUMN IF NOT EXISTS match_method    TEXT;

-- The invariant the old pipeline violated 2,276 times. Enforced in the database so
-- a future scraper bug fails loudly instead of silently poisoning every difficulty
-- score in the product.
CREATE UNIQUE INDEX IF NOT EXISTS rmp_reviews_rmp_id_unique
  ON public.rmp_reviews (rmp_id) WHERE rmp_id IS NOT NULL;

-- 3. user_profiles: per-user planner preferences
CREATE TABLE IF NOT EXISTS public.user_profiles (
  id                        UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  major_code                TEXT,
  graduation_target_year    INT,
  graduation_target_quarter TEXT,
  preferred_max_units       INT,
  updated_at                TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
-- Users can only read and write their own row
CREATE POLICY "owner access" ON public.user_profiles
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

-- 4. saved_degree_plans: saved planner state per user
CREATE TABLE IF NOT EXISTS public.saved_degree_plans (
  id          BIGSERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL DEFAULT 'My Plan',
  plan_data   JSONB,
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.saved_degree_plans ENABLE ROW LEVEL SECURITY;
-- Users can only read and write their own plans
CREATE POLICY "owner access" ON public.saved_degree_plans
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- 5. app_stats: app-wide counters (read via the SUPABASE_SERVICE_KEY only)
CREATE TABLE IF NOT EXISTS public.app_stats (
  key        TEXT PRIMARY KEY,
  value      BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO public.app_stats (key, value) VALUES ('schedules_saved', 0)
ON CONFLICT (key) DO NOTHING;

-- RLS on with NO policies → anon/authenticated have zero access; only the
-- service role (which bypasses RLS) can read/write. Both the optimizer backend
-- and /api/stats use SUPABASE_SERVICE_KEY, so they keep working.
ALTER TABLE public.app_stats ENABLE ROW LEVEL SECURITY;

-- Atomic counter bump, called fire-and-forget from the optimizer backend.
CREATE OR REPLACE FUNCTION public.increment_stat(stat_key TEXT)
RETURNS VOID AS $$
BEGIN
  UPDATE public.app_stats SET value = value + 1, updated_at = NOW()
  WHERE key = stat_key;
END;
$$ LANGUAGE plpgsql;

-- auth.users isn't exposed over PostgREST, so expose the count via a
-- SECURITY DEFINER function the service role can call with rpc().
CREATE OR REPLACE FUNCTION public.get_total_users()
RETURNS BIGINT AS $$
  SELECT COUNT(*) FROM auth.users;
$$ LANGUAGE sql SECURITY DEFINER;

-- Lock both RPCs to the service role: functions default to EXECUTE for PUBLIC,
-- which would let anon bump the counter or read the user count over the API.
REVOKE EXECUTE ON FUNCTION public.increment_stat(TEXT)  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_total_users()     FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.increment_stat(TEXT)  TO service_role;
GRANT  EXECUTE ON FUNCTION public.get_total_users()     TO service_role;
