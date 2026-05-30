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
