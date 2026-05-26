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
