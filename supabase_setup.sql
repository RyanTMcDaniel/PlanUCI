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

-- No RLS policy → only the service role can read/write (anon has no access).

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
