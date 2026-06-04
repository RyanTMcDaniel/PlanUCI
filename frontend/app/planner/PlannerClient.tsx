"use client";

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import {
  DndContext,
  DragEndEvent,
  DragOverlay,
  DragStartEvent,
  PointerSensor,
  useSensor,
  useSensors,
  useDroppable,
  useDraggable,
} from "@dnd-kit/core";
import { CSS } from "@dnd-kit/utilities";
import { type MajorOption, fetchMajors } from "@/lib/api/majors";
import { type MinorOption, fetchMinors, fetchMinorRequirements } from "@/lib/api/minors";
import {
  type ReqGroup,
  type CourseDetail,
  type ApCreditResult,
  fetchMajorRequirements,
  fetchCourseDetails,
  fetchPrereqTrees,
  fetchGERequirements,
  fetchApExamNames,
  resolveApCredits,
} from "@/lib/api/courses";
import { createClient } from "@/lib/supabase/client";
import {
  savePlan, loadPlan, syncUserProfile, deletePlan,
  saveNamedPlan, listNamedPlans, deletePlanById, loadPlanById,
  MAX_SAVED_PLANS, type SavedPlanMeta, type PlanData,
} from "@/lib/api/plans";
import { exportScheduleToPDF, type PdfYear } from "@/lib/pdf/exportSchedule";
// ── Types ─────────────────────────────────────────────────────────────────────

type PlannedCourses = Record<string, string[]>;

interface DragData {
  type: "sidebar" | "placed";
  courseId: string;
  quarterKey?: string;
}

// Coverage pills shown on autofilled cards that cross-cover requirements.
// Major/Minor courses placed with no cross-cover carry no tag (clean card).
type CoverageTag =
  | { kind: "major" }
  | { kind: "ge"; code: string }
  | { kind: "minor" };

// Clean UCI GE codes keyed by the DB's requirement_group, so pills read "Ia"
// instead of "I_WRITING_LD" / "V_THIRD".
const GE_CODE_LABELS: Record<string, string> = {
  GE_I_WRITING_LD: "Ia",
  GE_I_WRITING_UD: "Ib",
  GE_II: "II",
  GE_III: "III",
  GE_IV: "IV",
  GE_Va: "Va",
  GE_Vb: "Vb",
  GE_V_THIRD: "V",
  GE_VI: "VI",
  GE_VII: "VII",
  GE_VIII: "VIII",
};

// ge_list token (e.g. "III", "Va") → requirement_group. The authoritative GE
// taxonomy: a course satisfies a category iff its ge_list says so.
const GE_TOKEN_TO_GROUP: Record<string, string> = {
  Ia: "GE_I_WRITING_LD",
  Ib: "GE_I_WRITING_UD",
  II: "GE_II",
  III: "GE_III",
  IV: "GE_IV",
  Va: "GE_Va",
  Vb: "GE_Vb",
  VI: "GE_VI",
  VII: "GE_VII",
  VIII: "GE_VIII",
};
// Canonical category order for tie-breaks.
const GE_ORDER: Record<string, number> = {
  GE_I_WRITING_LD: 0, GE_I_WRITING_UD: 1, GE_II: 2, GE_III: 3, GE_IV: 4,
  GE_Va: 5, GE_Vb: 6, GE_V_THIRD: 7, GE_VI: 8, GE_VII: 9, GE_VIII: 10,
};
// clean code ("III", "V", "Ia") → requirement_group (inverse of GE_CODE_LABELS).
const GE_LABEL_TO_GROUP: Record<string, string> = Object.fromEntries(
  Object.entries(GE_CODE_LABELS).map(([g, label]) => [label, g]),
);

// Authoritative GE coverage for a course, derived ONLY from its ge_list (never
// array membership). Returns requirement_group codes that also appear in the
// passed geRequirements (so already-filled / not-needed categories are skipped
// when the caller passes the unfilled subset). GE_V_THIRD is satisfied by any
// "GE Va:" / "GE Vb:" course since it has no token of its own.
function getCourseGECategories(course: CourseDetail | undefined, geRequirements: ReqGroup[]): string[] {
  if (!course?.ge_list?.length) return [];
  const present = new Set(geRequirements.map((r) => r.requirement_group ?? "").filter(Boolean));
  const tokens = new Set<string>();
  for (const s of course.ge_list) {
    const m = /^GE\s+([A-Za-z]+)\s*:/.exec(s);
    if (m) tokens.add(m[1]);
  }
  const out = new Set<string>();
  for (const tok of tokens) {
    const g = GE_TOKEN_TO_GROUP[tok];
    if (g && present.has(g)) out.add(g);
  }
  if ((tokens.has("Va") || tokens.has("Vb")) && present.has("GE_V_THIRD")) out.add("GE_V_THIRD");
  return [...out].sort((a, b) => (GE_ORDER[a] ?? 99) - (GE_ORDER[b] ?? 99));
}

// Prerequisite-tree node/leaf shape: { AND|OR|NOT: item[] }, where a leaf has a
// prereqType ("course" | "exam") and (for courses) a courseId / coreq flag.
type PrereqItem = {
  prereqType?: string;
  courseId?: string;
  coreq?: boolean;
} & Record<string, unknown>;

// ── Prereq closure ─────────────────────────────────────────────────────────
// Course-leaf ids that must be ADDED so a prereq tree is satisfiable given what
// is already available. AND → every child; OR → nothing if any option already
// available, else pull the first course option; NOT/exam → ignored.
function itemSatisfied(i: PrereqItem, have: Set<string>): boolean {
  if (i?.prereqType === "course") return have.has(normId(String(i.courseId ?? "")));
  if (i?.prereqType === "exam") return true;
  return requiredMissingCourses(i, have).length === 0;
}
function missingFromItem(i: PrereqItem, have: Set<string>): string[] {
  if (i?.prereqType === "course") {
    const cid = String(i.courseId ?? "");
    return cid && !have.has(normId(cid)) ? [cid] : [];
  }
  if (i?.prereqType === "exam") return [];
  return requiredMissingCourses(i, have);
}
function requiredMissingCourses(node: unknown, have: Set<string>): string[] {
  if (!node || typeof node !== "object") return [];
  const n = node as Record<string, unknown>;
  if (Array.isArray(n.AND)) return (n.AND as PrereqItem[]).flatMap((i) => missingFromItem(i, have));
  if (Array.isArray(n.OR)) {
    const items = n.OR as PrereqItem[];
    if (items.some((i) => itemSatisfied(i, have))) return [];
    const firstCourse = items.find((i) => i?.prereqType === "course" && i.courseId);
    return firstCourse?.courseId ? [String(firstCourse.courseId)] : [];
  }
  return []; // NOT / unknown → nothing to add
}

// All course-leaf ids referenced anywhere in a prereq tree's AND/OR branches
// (NOT = anti-requisite, skipped). Used to derive topological seed ordering.
function collectPrereqCourseLeaves(node: unknown): string[] {
  if (!node || typeof node !== "object") return [];
  const n = node as Record<string, unknown>;
  const out: string[] = [];
  for (const key of ["AND", "OR"] as const) {
    const arr = n[key];
    if (!Array.isArray(arr)) continue;
    for (const item of arr as PrereqItem[]) {
      if (item?.prereqType === "course" && item.courseId) out.push(String(item.courseId));
      else if (item?.prereqType !== "exam" && item && typeof item === "object") {
        out.push(...collectPrereqCourseLeaves(item));
      }
    }
  }
  return out;
}

interface CourseStats {
  professor: {
    name: string;
    overall_rating: number;
    difficulty_rating: number;
    num_ratings: number;
    sentiment_label: string | null;
  } | null;
  difficulty_score: number | null;
  prof_gpa: number | null;
}

interface TopProfessor {
  name: string;
  ucinetid: string;
  avg_difficulty: number | null;
  avg_quality: number | null;
  review_count: number;
  overall_avg_gpa: number | null;
  quarters_taught: string[];
  teaching_frequency: number;
}

// Module-level cache — survives tooltip unmount/remount within the session
const _topProfsCache = new Map<string, TopProfessor[]>();

function fullQuarter(q: string): string {
  // quarters_taught arrive as "Fall 2021"; show the full term name on cards.
  const [season, year] = q.split(" ");
  return year ? `${season} ${year}` : season;
}

// Normalize a course ID for comparison — mirrors the backend `_norm` so placed
// courses match requirement-pool IDs regardless of source format.  Without this,
// a pool listing "I&CSCI161" or "CSE46" never matches a placed "ICS161"/"ICS46"
// (and vice versa), so elective counters read 0 even when courses are placed.
const _COURSE_ALIASES: Record<string, string> = {
  CSE31: "ICS31", CSE43: "ICS43", CSE45C: "ICS45C", CSE46: "ICS46",
};
function normId(id: string): string {
  const s = id.replace(/\s+/g, "").toUpperCase().replace("I&CSCI", "ICS");
  return _COURSE_ALIASES[s] ?? s;
}

// ── Constants ─────────────────────────────────────────────────────────────────

// Calendar anchor for the qkey scheme — keeps plannedCourses keys (e.g.
// "2026_fall") stable.  The planner is now purely structural: years are
// labelled "Year 1..N" and the calendar year only feeds the quarter key.
const START_YEAR = 2026;
const DEFAULT_YEARS = 4;
const BASE_QUARTERS = [
  { key: "fall",   label: "Fall"   },
  { key: "winter", label: "Winter" },
  { key: "spring", label: "Spring" },
];
const TIP_YEARS = [2021, 2022, 2023, 2024, 2025];

function qkey(year: number, q: string) {
  const fallYear = START_YEAR + year - 1;
  // Winter, Spring, Summer belong to the calendar year after the Fall quarter
  const calYear = q === "fall" ? fallYear : fallYear + 1;
  return `${calYear}_${q}`;
}

// Inverse of qkey for display: "2027_winter" → "Year 2 · Winter".
function formatQuarterKey(qk: string): string {
  const [yStr, season = ""] = qk.split("_");
  const calYear = parseInt(yStr, 10);
  const yearIdx = season === "fall" ? calYear - START_YEAR + 1 : calYear - START_YEAR;
  const seasonLabel = season ? season.charAt(0).toUpperCase() + season.slice(1) : qk;
  return `Year ${yearIdx} · ${seasonLabel}`;
}

const UNIT_PRESETS: { label: string; value: number; warning: boolean }[] = [
  { label: "Standard 16 Units", value: 16, warning: false },
  { label: "Heavy 20 Units",    value: 20, warning: true  },
];

function diffColor(level: string | null | undefined): string {
  if (!level) return "#3a3a3a";
  if (level.includes("Lower")) return "#22c55e";
  if (level.includes("Upper")) return "#eab308";
  if (level.includes("Graduate")) return "#ef4444";
  return "#3a3a3a";
}

function diffScoreColor(score: number): string {
  if (score < 4) return "#22c55e";
  if (score < 6) return "#eab308";
  if (score < 8) return "#f97316";
  return "#ef4444";
}

// ── Bucket classification ──────────────────────────────────────────────────────

type BucketKey = "lower" | "upper" | "choice" | "elective";

const BUCKET_LABELS: Record<BucketKey, string> = {
  lower:    "LOWER DIVISION REQUIRED",
  upper:    "UPPER DIVISION REQUIRED",
  choice:   "REQUIRED SELECTIONS",
  elective: "ELECTIVES",
};

// Groups with more courses than this get their own collapsible row inside a bucket
const INLINE_THRESHOLD = 8;

function extractCourseNumber(courseId: string): number {
  const m = courseId.match(/\d+/);
  return m ? parseInt(m[0], 10) : 0;
}

function classifyGroup(req: ReqGroup): BucketKey {
  const name = req.group_name.toLowerCase();

  // Rule 1: elective type is absolute — never overridden
  if (req.requirement_type === "elective") return "elective";

  // Rule 2: name keywords that signal a choice/pool regardless of type field
  if (
    name.includes("elective")   ||
    name.includes("select")     ||
    name.includes("choose")     ||
    name.includes("outside")    ||
    name.includes("additional") ||
    name.includes("pick")
  ) {
    return "elective";
  }

  // Rule 3: any pick-N group → "Required Selections" bucket (clearly a choice, not individually required)
  if (req.courses_needed < req.courses.length) {
    return "choice";
  }

  // Rule 4: lower vs upper by highest course number; sequences stay in their natural bucket
  if (req.courses.length === 0) return "upper";
  const nums = req.courses.map(extractCourseNumber).filter((n) => n > 0);
  if (nums.length === 0) return "upper";
  return Math.max(...nums) < 100 ? "lower" : "upper";
}

interface CourseWithDifficulty {
  difficulty_score: number | null;
  units: number | null;
}

interface QuarterDifficultyResult {
  combined: number;
  difficultyComponent: number;
  unitComponent: number;
  totalUnits: number;
}

// Backend requirements_state (POST /optimizer/requirements_state) — the authoritative
// coverage source. choice_groups.placed/remaining EXCLUDE the mandatory backbone
// (required + injected prereqs), so required courses appearing in elective pools no
// longer wrongly mark those pools satisfied.
interface BackendChoiceGroup {
  group_id:  string;
  label:     string;
  choose_n:  number;
  placed:    number;
  remaining: number;
  options:   { course_id: string }[];
}
interface RequirementsState {
  required_placed: string[];
  choice_groups:   BackendChoiceGroup[];
  all_satisfied:   boolean;
}
// Unified per-group coverage returned by getCoverage().
interface Coverage {
  placed:    number;
  needed:    number;
  remaining: number;
  done:      boolean;
}
type GetCoverage = (req: ReqGroup) => Coverage;

function calculateQuarterDifficulty(courses: CourseWithDifficulty[]): QuarterDifficultyResult | null {
  if (courses.length === 0) return null;

  const sorted = [...courses]
    .filter((c) => c.difficulty_score != null)
    .sort((a, b) => (b.difficulty_score ?? 5) - (a.difficulty_score ?? 5));

  const weights = [1.0, 0.85, 0.72, 0.61, 0.52, 0.44];
  let weightedSum = 0;
  let totalWeight = 0;
  sorted.forEach((course, i) => {
    const w = weights[i] ?? 0.4;
    weightedSum += (course.difficulty_score ?? 5) * w;
    totalWeight += w;
  });

  const difficultyComponent = totalWeight > 0 ? weightedSum / totalWeight : 5.0;
  const countPenalty = Math.max(0, (sorted.length - 3) * 0.3);
  const adjustedDifficulty = Math.min(10, difficultyComponent + countPenalty);

  const totalUnits = courses.reduce((sum, c) => sum + (c.units ?? 4), 0);
  const unitComponent = Math.min(10, (totalUnits / 20) * 10);

  const combined = Math.min(10, adjustedDifficulty * 0.6 + unitComponent * 0.4);

  return { combined, difficultyComponent: adjustedDifficulty, unitComponent, totalUnits };
}

// ── Lock conflict helpers ──────────────────────────────────────────────────────

function fmtCourse(id: string): string {
  // "MATH2D" → "MATH 2D",  "PHYSICS7C" → "PHYSICS 7C",  "I&CSCI31" → "I&CSCI 31"
  return id.replace(/^([A-Za-z&]+)(\d.*)$/, "$1 $2");
}

function parseLockConflict(conflict: string): string {
  if (conflict.includes(" missing prereq: ")) {
    const [cid, req] = conflict.split(" missing prereq: ");
    return `${fmtCourse(cid.trim())} requires ${fmtCourse(req.trim())} to be scheduled first`;
  }
  const courseId = conflict.split(" locked to")[0]?.trim() ?? "";
  const afterColon = conflict.split(": ").slice(1).join(": ");
  const blocker = afterColon
    .replace(" must be placed in an earlier quarter", "")
    .split(",")[0]
    .split(" (")[0]
    .trim();
  if (blocker && !blocker.startsWith("a ")) {
    return `${fmtCourse(courseId)} requires ${fmtCourse(blocker)} to be scheduled first`;
  }
  return `${fmtCourse(courseId)} has a prerequisite ordering conflict`;
}

// ── Icons ─────────────────────────────────────────────────────────────────────

function LockIcon({ locked }: { locked: boolean }) {
  return (
    <svg viewBox="0 0 14 14" fill="none"
      className={`w-4 h-4 transition-colors ${locked ? "text-amber-400" : "text-[#555] group-hover/card:text-[#e8e8e8]"}`}>
      <rect x="3" y="6" width="8" height="6" rx="1.2" stroke="currentColor" strokeWidth="1.3"/>
      <path d="M5 6V4.5a2 2 0 014 0V6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 12 12" fill="none"
      className={`w-3 h-3 text-[#444] transition-transform shrink-0 ${open ? "rotate-180" : ""}`}>
      <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  );
}

// ── Error banner ───────────────────────────────────────────────────────────────

function ErrorBanner({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 px-2 py-1.5 rounded border border-red-900/40 bg-red-950/20 mx-1 my-1">
      <span className="text-[10px] text-red-400 truncate">{message}</span>
      <button onClick={onRetry} className="text-[9px] text-red-300 underline shrink-0 hover:text-red-200">
        Retry
      </button>
    </div>
  );
}

// ── Major combobox ────────────────────────────────────────────────────────────

function MajorCombobox({
  options, selectedDisplayName, onSelect, loading,
}: {
  options: MajorOption[];
  selectedDisplayName: string;
  onSelect: (displayName: string) => void;
  loading: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) setQuery(selectedDisplayName);
  }, [selectedDisplayName, open]);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery(selectedDisplayName);
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [selectedDisplayName]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options
      .filter((o) => !q || o.display_name.toLowerCase().includes(q))
      .slice(0, 40);
  }, [options, query]);

  function handleSelect(opt: MajorOption) {
    onSelect(opt.display_name);
    setQuery(opt.display_name);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      setQuery(selectedDisplayName);
      inputRef.current?.blur();
    }
    if (e.key === "Enter" && filtered.length > 0) handleSelect(filtered[0]);
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={loading ? "Loading majors…" : "Search for your major..."}
          disabled={loading && options.length === 0}
          className="w-full bg-[#111] border border-[#2a2a2a] rounded px-2.5 py-1.5 text-[11px] text-[#f0f0f0] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/60 disabled:opacity-50 pr-6"
        />
        <svg className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-[#444] pointer-events-none" viewBox="0 0 12 12" fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
        </svg>
      </div>
      {open && filtered.length > 0 && (
        <div className="absolute z-50 top-full left-0 right-0 mt-px bg-[#1a1a1a] border border-[#2a2a2a] rounded max-h-52 overflow-y-auto shadow-xl">
          {filtered.map((opt) => (
            <button
              key={opt.major_id}
              onMouseDown={() => handleSelect(opt)}
              className="w-full text-left px-2.5 py-[7px] text-[11px] text-[#bbb] hover:bg-[#252525] hover:text-[#f0f0f0] transition-colors"
            >
              {opt.display_name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Minor combobox ────────────────────────────────────────────────────────────
// Mirrors MajorCombobox but minors are flat (no specializations).

function MinorCombobox({
  options, selectedMinorId, onSelect, loading,
}: {
  options: MinorOption[];
  selectedMinorId: string;
  onSelect: (minorId: string) => void;
  loading: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selectedName = useMemo(
    () => options.find((o) => o.minor_id === selectedMinorId)?.name ?? "",
    [options, selectedMinorId],
  );

  useEffect(() => {
    if (!open) setQuery(selectedName);
  }, [selectedName, open]);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery(selectedName);
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [selectedName]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options
      .filter((o) => !q || o.name.toLowerCase().includes(q))
      .slice(0, 40);
  }, [options, query]);

  function handleSelect(opt: MinorOption) {
    onSelect(opt.minor_id);
    setQuery(opt.name);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      setQuery(selectedName);
      inputRef.current?.blur();
    }
    if (e.key === "Enter" && filtered.length > 0) handleSelect(filtered[0]);
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={loading ? "Loading minors…" : "Search for a minor..."}
          disabled={loading && options.length === 0}
          className="w-full bg-[#111] border border-[#2a2a2a] rounded px-2.5 py-1.5 text-[11px] text-[#f0f0f0] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/60 disabled:opacity-50 pr-6"
        />
        <svg className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-[#444] pointer-events-none" viewBox="0 0 12 12" fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
        </svg>
      </div>
      {open && filtered.length > 0 && (
        <div className="absolute z-50 top-full left-0 right-0 mt-px bg-[#1a1a1a] border border-[#2a2a2a] rounded max-h-52 overflow-y-auto shadow-xl">
          {filtered.map((opt) => (
            <button
              key={opt.minor_id}
              onMouseDown={() => handleSelect(opt)}
              className="w-full text-left px-2.5 py-[7px] text-[11px] text-[#bbb] hover:bg-[#252525] hover:text-[#f0f0f0] transition-colors"
            >
              {opt.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Specialization combobox ─────────────────────────────────────────────────
// Mirrors MajorCombobox/MinorCombobox; specializations select by major_id.

function SpecializationCombobox({
  options, selectedMajorId, onSelect, loading,
}: {
  options: MajorOption[];
  selectedMajorId: string;
  onSelect: (majorId: string) => void;
  loading: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const specLabel = (o: MajorOption) => o.specialization_name ?? o.major_id;

  const selectedName = useMemo(() => {
    const o = options.find((o) => o.major_id === selectedMajorId);
    return o ? specLabel(o) : "";
  }, [options, selectedMajorId]);

  useEffect(() => {
    if (!open) setQuery(selectedName);
  }, [selectedName, open]);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery(selectedName);
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [selectedName]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options
      .filter((o) => !q || specLabel(o).toLowerCase().includes(q))
      .slice(0, 40);
  }, [options, query]);

  function handleSelect(opt: MajorOption) {
    onSelect(opt.major_id);
    setQuery(specLabel(opt));
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      setQuery(selectedName);
      inputRef.current?.blur();
    }
    if (e.key === "Enter" && filtered.length > 0) handleSelect(filtered[0]);
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={loading ? "Loading specializations…" : "Search for a specialization..."}
          disabled={loading && options.length === 0}
          className="w-full bg-[#111] border border-[#2a2a2a] rounded px-2.5 py-1.5 text-[11px] text-[#f0f0f0] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/60 disabled:opacity-50 pr-6"
        />
        <svg className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-[#444] pointer-events-none" viewBox="0 0 12 12" fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
        </svg>
      </div>
      {open && filtered.length > 0 && (
        <div className="absolute z-50 top-full left-0 right-0 mt-px bg-[#1a1a1a] border border-[#2a2a2a] rounded max-h-52 overflow-y-auto shadow-xl">
          {filtered.map((opt) => (
            <button
              key={opt.major_id}
              onMouseDown={() => handleSelect(opt)}
              className="w-full text-left px-2.5 py-[7px] text-[11px] text-[#bbb] hover:bg-[#252525] hover:text-[#f0f0f0] transition-colors"
            >
              {specLabel(opt)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Course tooltip ─────────────────────────────────────────────────────────────

function CourseTooltip({
  courseId, info, style, onMouseEnter, onMouseLeave,
}: {
  courseId: string;
  info: CourseDetail | undefined;
  style: React.CSSProperties;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}) {
  const [stats, setStats] = useState<CourseStats | null>(null);
  const [topProfs, setTopProfs] = useState<TopProfessor[]>(() => _topProfsCache.get(courseId) ?? []);
  const [topProfsLoading, setTopProfsLoading] = useState(!_topProfsCache.has(courseId));

  useEffect(() => {
    setStats(null);
    fetch(`/api/course-stats?id=${encodeURIComponent(courseId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setStats(d ?? null))
      .catch(() => setStats(null));
  }, [courseId]);

  useEffect(() => {
    if (_topProfsCache.has(courseId)) return;
    setTopProfsLoading(true);
    fetch(`/api/top-professors?id=${encodeURIComponent(courseId)}`)
      .then((r) => (r.ok ? r.json() : { professors: [] }))
      .then((d) => {
        const profs: TopProfessor[] = d?.professors ?? [];
        _topProfsCache.set(courseId, profs);
        setTopProfs(profs);
      })
      .catch(() => {})
      .finally(() => setTopProfsLoading(false));
  }, [courseId]);

  const termsSet = useMemo(() => new Set(info?.terms ?? []), [info]);

  return (
    <div
      style={style}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="w-[300px] bg-[#1e1e1e] border border-[#3a3a3a] rounded-lg shadow-2xl overflow-hidden pointer-events-auto"
    >
      {/* ── Course info ── */}
      <div className="px-3 py-2.5 border-b border-[#2a2a2a]">
        <p className="text-[13px] font-bold text-[#e8e8e8] leading-tight">{courseId}</p>
        {info?.title && (
          <p className="text-[11px] text-[#999] mt-0.5 leading-snug">{info.title}</p>
        )}
        {info?.description && (
          <p className="text-[10px] text-[#555] mt-1.5 leading-relaxed line-clamp-3">
            {info.description}
          </p>
        )}
        <div className="flex items-center gap-3 mt-1.5">
          <span className="text-[10px] text-[#666]">
            {info?.min_units ?? "?"} UNITS
          </span>
          {stats?.difficulty_score != null && (
            <span className="text-[10px] text-[#eab308]">
              Difficulty {stats.difficulty_score.toFixed(1)}/10
            </span>
          )}
          <span className="text-[10px] text-[#22c55e]">
            {info?.avg_gpa != null && isFinite(info.avg_gpa)
              ? `Avg GPA: ${info.avg_gpa.toFixed(2)}`
              : "Avg GPA: No data"}
          </span>
        </div>
      </div>

      {/* ── Offering history ── */}
      <div className="px-3 py-2 border-b border-[#2a2a2a]">
        <p className="text-[8px] font-bold uppercase tracking-widest text-[#444] mb-1.5">Offered</p>
        <div style={{ display: "grid", gridTemplateColumns: "22px repeat(3, 1fr)", gap: "2px 4px" }}>
          <span />
          {["Fall", "Winter", "Spring"].map((q) => (
            <span key={q} className="text-[7px] text-[#444] text-center font-mono">{q}</span>
          ))}
          {[...TIP_YEARS].reverse().flatMap((yr) => [
            <span key={`${yr}l`} className="text-[7px] text-[#444] font-mono">{String(yr).slice(2)}</span>,
            ...["Fall", "Winter", "Spring"].map((q) => (
              <span
                key={`${yr}${q}`}
                title={`${q} ${yr}`}
                className={`text-[9px] text-center leading-tight ${
                  termsSet.has(`${yr} ${q}`) ? "text-[#22c55e]" : "text-[#2a2a2a]"
                }`}
              >
                {termsSet.has(`${yr} ${q}`) ? "✓" : "·"}
              </span>
            )),
          ])}
        </div>
      </div>

      {/* ── Top professors ── */}
      <div className="px-3 py-2">
        <p className="text-[8px] font-bold uppercase tracking-widest text-[#444] mb-1.5">
          Top Professors · Past 3 Years
        </p>
        {topProfsLoading ? (
          <p className="text-[10px] text-[#3a3a3a]">Loading…</p>
        ) : topProfs.length === 0 ? (
          <p className="text-[10px] text-[#3a3a3a]">No recent professor data</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {topProfs.map((prof) => (
              <div key={prof.ucinetid} className="text-[9px] leading-snug">
                <span className="font-semibold text-[#ccc]">{prof.name}</span>
                {prof.avg_quality != null && (
                  <span className="text-[#22c55e]">{"  "}Quality: {prof.avg_quality.toFixed(1)}</span>
                )}
                {prof.avg_difficulty != null && (
                  <span className="text-[#aaa]">{"  "}Difficulty: {prof.avg_difficulty.toFixed(1)}</span>
                )}
                {prof.overall_avg_gpa != null && Number.isFinite(prof.overall_avg_gpa) && (
                  <span className="text-[#22c55e]">{"  "}GPA: {prof.overall_avg_gpa.toFixed(2)}</span>
                )}
                {prof.quarters_taught.length > 0 && (
                  <span className="text-[#555]">{"  "}Taught: {prof.quarters_taught.map(fullQuarter).join(", ")}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Coverage pills ───────────────────────────────────────────────────────────
// Small colored tags on a placed card showing what an autofilled course covers:
// blue = major requirement, green = GE category (with code), purple = minor.

function CoverageTags({ tags }: { tags: CoverageTag[] }) {
  if (tags.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 mt-1">
      {tags.map((t, i) => {
        const label = t.kind === "major" ? "Major" : t.kind === "minor" ? "Minor" : t.code;
        const cls =
          t.kind === "major"
            ? "bg-[#3b82f6]/20 border-[#3b82f6]/40 text-[#93c5fd]"
            : t.kind === "minor"
            ? "bg-purple-500/20 border-purple-500/40 text-purple-300"
            : "bg-emerald-500/20 border-emerald-500/40 text-emerald-300";
        return (
          <span
            key={i}
            className={`rounded-full border px-1.5 py-[1px] text-[8.5px] font-bold leading-none tracking-wide ${cls}`}
          >
            {label}
          </span>
        );
      })}
    </div>
  );
}

// ── Placed card ────────────────────────────────────────────────────────────────

function PlacedCard({
  courseId, quarterKey, title, units, level, diffScore, gpa, isLocked, onToggleLock, onRemove, tags,
}: {
  courseId: string;
  quarterKey: string;
  title?: string | null;
  units?: number | null;
  level?: string | null;
  diffScore?: number | null;
  gpa?: number | null;
  isLocked: boolean;
  onToggleLock: (id: string) => void;
  onRemove: (id: string, qKey: string) => void;
  tags?: CoverageTag[];
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `placed|${quarterKey}|${courseId}`,
    data: { type: "placed", courseId, quarterKey } satisfies DragData,
  });

  const borderColor = isLocked
    ? "#f59e0b"
    : diffScore != null
    ? diffScoreColor(diffScore)
    : diffColor(level);

  const style: React.CSSProperties = {
    ...(transform ? { transform: CSS.Translate.toString(transform) } : {}),
    borderLeft: `3px solid ${borderColor}`,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group/card flex items-stretch gap-1.5 rounded-r-md pl-1 pr-0 select-none
        bg-[#1e1e1e] border border-l-0 border-[#2a2a2a] shadow-sm transition-colors min-h-[44px]
        ${isDragging ? "opacity-20" : "hover:bg-[#252525] hover:border-[#333]"}`}
    >
      {/* drag handle — left, vertically centered */}
      <span
        {...listeners}
        {...attributes}
        className="flex items-center text-[26px] text-[#2a2a2a] group-hover/card:text-[#444] cursor-grab active:cursor-grabbing shrink-0 leading-none self-center"
      >
        ⠿
      </span>

      {/* content — course code (larger) + name (smaller) + coverage pills */}
      <div className="flex-1 min-w-0 flex flex-col justify-center gap-0.5 py-1">
        <p className="text-[19px] font-bold text-[#e8e8e8] leading-tight truncate">{courseId}</p>
        {title && (
          <p title={title} className="text-[10px] text-[#999] leading-snug overflow-hidden text-ellipsis whitespace-nowrap">{title}</p>
        )}
        {tags && <CoverageTags tags={tags} />}
      </div>

      {/* units (top) + avg gpa (below) — right side, left of action strip */}
      <div className="flex flex-col items-end justify-center gap-0.5 shrink-0 py-1.5 pr-1 text-right">
        <span className="text-[11px] font-medium text-[#888] tabular-nums">{units ?? "?"} <span className="font-normal text-[#5a5a5a]">UNITS</span></span>
        {gpa != null && isFinite(gpa) ? (
          <span className="text-[10px] font-mono font-medium" style={{ color: "#9a9a9a" }}>{gpa.toFixed(2)} AVG GPA</span>
        ) : (
          <span className="text-[9px] font-mono text-[#5a5a5a] whitespace-nowrap">No GPA Data</span>
        )}
      </div>

      {/* right-edge action strip — full card height: remove (top) / lock (bottom) */}
      <div className="flex flex-col shrink-0 w-7 self-stretch border-l border-[#2a2a2a]">
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => onRemove(courseId, quarterKey)}
          title="Remove"
          className="flex-1 flex items-center justify-center rounded-tr-md text-[#555] hover:text-[#e8e8e8] hover:bg-[#333] text-[18px] leading-none transition-colors border-b border-[#2a2a2a]"
        >
          ×
        </button>
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => onToggleLock(courseId)}
          title={isLocked ? "Unlock" : "Lock to quarter"}
          className="flex-1 flex items-center justify-center rounded-br-md hover:bg-[#333] transition-colors"
        >
          <LockIcon locked={isLocked} />
        </button>
      </div>
    </div>
  );
}

// ── Placed card row (with tooltip + prereq warning) ────────────────────────────

function PlacedCardRow({
  courseId, courseInfoMap, prereqWarning, onDismissWarning,
  ...cardProps
}: {
  courseId: string;
  courseInfoMap: Record<string, CourseDetail>;
  prereqWarning?: string;
  onDismissWarning?: () => void;
} & Omit<React.ComponentProps<typeof PlacedCard>, "courseId">) {
  const [tipVisible, setTipVisible] = useState(false);
  const [tipPos, setTipPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const showTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const rowRef = useRef<HTMLDivElement>(null);

  function scheduleShow() {
    clearTimeout(hideTimer.current);
    clearTimeout(showTimer.current);
    showTimer.current = setTimeout(() => {
      if (!rowRef.current) return;
      const rect = rowRef.current.getBoundingClientRect();
      const goRight = window.innerWidth - rect.right >= 320;
      setTipPos({
        top: Math.min(rect.top, window.innerHeight - 370),
        left: goRight ? rect.right + 8 : Math.max(8, rect.left - 308),
      });
      setTipVisible(true);
    }, 300);
  }

  function scheduleHide() {
    clearTimeout(showTimer.current);
    hideTimer.current = setTimeout(() => setTipVisible(false), 120);
  }

  function cancelHide() {
    clearTimeout(hideTimer.current);
  }

  useEffect(() => () => {
    clearTimeout(showTimer.current);
    clearTimeout(hideTimer.current);
  }, []);

  return (
    <div ref={rowRef} onMouseEnter={scheduleShow} onMouseLeave={scheduleHide}>
      <PlacedCard courseId={courseId} {...cardProps} />

      {prereqWarning && (
        <div className="flex items-center gap-1 text-[9px] text-red-400 px-2 py-0.5">
          <span className="truncate">⚠ {prereqWarning}</span>
          {onDismissWarning && (
            <button
              onClick={onDismissWarning}
              className="ml-auto shrink-0 hover:text-red-300 leading-none"
            >
              ×
            </button>
          )}
        </div>
      )}

      {tipVisible && (
        <CourseTooltip
          courseId={courseId}
          info={courseInfoMap[courseId]}
          style={{
            position: "fixed",
            top: tipPos.top,
            left: tipPos.left,
            zIndex: 9999,
          }}
          onMouseEnter={cancelHide}
          onMouseLeave={scheduleHide}
        />
      )}
    </div>
  );
}

// ── Quarter cell ───────────────────────────────────────────────────────────────

function QuarterCell({
  qKey, label, dim,
  courseIds, courseInfoMap, difficultyMap, lockedCourses, onToggleLock, onRemove,
  prereqWarnings, onDismissWarning,
  removable, onRemoveQuarter,
  maxUnitsPerQuarter, coverageTags,
}: {
  qKey: string;
  label: string;
  dim: boolean;
  courseIds: string[];
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  lockedCourses: Set<string>;
  onToggleLock: (id: string) => void;
  onRemove: (id: string, qKey: string) => void;
  prereqWarnings: Record<string, string>;
  onDismissWarning: (id: string) => void;
  removable?: boolean;
  onRemoveQuarter?: () => void;
  maxUnitsPerQuarter?: number;
  coverageTags: Record<string, CoverageTag[]>;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `zone|${qKey}`,
    data: { quarterKey: qKey },
  });

  const quarterDiff = calculateQuarterDifficulty(
    courseIds.map((id) => ({
      difficulty_score: difficultyMap[id] ?? null,
      units: courseInfoMap[id]?.min_units ?? null,
    })),
  );

  const units = courseIds.reduce(
    (sum, id) => sum + (courseInfoMap[id]?.min_units ?? 4),
    0,
  );

  return (
    <div className={`flex flex-col border-r border-[#2a2a2a] last:border-r-0 ${dim ? "opacity-55" : ""}`}>
      {/* header */}
      <div className={`flex items-center px-2 h-7 border-b border-[#2a2a2a] shrink-0 bg-[#242424] border-l-2 ${dim ? "border-l-[#FFC72C]/30" : "border-l-[#3b82f6]/40"}`}>
        <span className={`text-[11px] font-bold tracking-wide ${dim ? "text-[#5a5648]" : "text-[#888]"}`}>
          {label}
        </span>
        {quarterDiff != null && (
          <span
            style={{ color: diffScoreColor(quarterDiff.combined) }}
            className="text-[9px] ml-1.5 cursor-default"
            title={[
              `Difficulty: ${quarterDiff.combined.toFixed(1)}/10`,
              `Course difficulty: ${quarterDiff.difficultyComponent.toFixed(1)}  Unit load: ${quarterDiff.totalUnits} units`,
              `(60% course difficulty + 40% unit load)`,
            ].join("\n")}
          >
            ◆ {quarterDiff.combined.toFixed(1)}
          </span>
        )}
        {units > 0 && (() => {
          const overload = maxUnitsPerQuarter != null
            ? units > maxUnitsPerQuarter
            : units > 19;
          return (
            <span
              className={`text-[10px] ml-auto mr-1 font-semibold tabular-nums ${overload ? "text-amber-500" : "text-[#777]"}`}
              title={overload ? `⚠ Exceeds ${maxUnitsPerQuarter ?? 16}-unit cap` : undefined}
            >
              {overload && "⚠ "}{units} <span className="font-normal text-[#555]">UNITS</span>
            </span>
          );
        })()}
        {removable && onRemoveQuarter && (
          <button
            onClick={onRemoveQuarter}
            title="Remove summer quarter — its courses return to the sidebar"
            className={`${units > 0 ? "" : "ml-auto"} flex items-center gap-0.5 rounded px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-500/80 hover:text-amber-300 hover:bg-amber-950/40 transition-colors leading-none shrink-0`}
          >
            ✕ Remove
          </button>
        )}
      </div>

      {/* droppable body */}
      <div
        ref={setNodeRef}
        className={`flex-1 flex flex-col gap-[5px] p-2 min-h-[160px] transition-colors
          ${isOver ? "bg-[#0d1a2d]" : "bg-[#1e1e1e]"}`}
      >
        {courseIds.length === 0 && !isOver && (
          <div className="m-0.5 flex-1 flex items-center justify-center border border-dashed border-[#2c2c2c] rounded-md">
            <span className="text-[9px] text-[#3a3a3a] font-medium select-none">Drop courses here</span>
          </div>
        )}
        {courseIds.map((cid) => (
          <PlacedCardRow
            key={cid}
            courseId={cid}
            quarterKey={qKey}
            title={courseInfoMap[cid]?.title}
            units={courseInfoMap[cid]?.min_units}
            level={courseInfoMap[cid]?.course_level}
            diffScore={difficultyMap[cid] ?? null}
            gpa={courseInfoMap[cid]?.avg_gpa ?? null}
            isLocked={lockedCourses.has(cid)}
            onToggleLock={onToggleLock}
            onRemove={onRemove}
            courseInfoMap={courseInfoMap}
            prereqWarning={prereqWarnings[cid]}
            onDismissWarning={() => onDismissWarning(cid)}
            tags={coverageTags[cid]}
          />
        ))}
      </div>
    </div>
  );
}

// ── Course pill (sidebar grid) ─────────────────────────────────────────────────

function CoursePill({
  courseId, title, units, isPlaced, isApCredit, unavailable, diffScore, compact,
}: {
  courseId: string;
  title?: string | null;
  units?: number | null;
  isPlaced: boolean;
  isApCredit?: boolean;
  unavailable?: boolean;
  diffScore?: number | null;
  compact?: boolean;   // smaller code font for "pick N of X" pools (long lists)
}) {
  // Pick-N pools list many courses; keep their codes compact. Everywhere else
  // uses the larger, easier-to-read size.
  const codeSize = compact ? "text-[9px]" : "text-[15px]";

  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `sidebar|${courseId}`,
    data: { type: "sidebar", courseId } satisfies DragData,
    disabled: isPlaced,
  });

  const style = transform ? { transform: CSS.Translate.toString(transform) } : undefined;
  const tooltip = unavailable
    ? "Course details unavailable"
    : title
    ? `${courseId} — ${title}${units ? ` (${units}u)` : ""}`
    : courseId;
  const dot = diffScore != null ? (
    <span style={{ color: diffScoreColor(diffScore) }} className="text-[6px] mr-0.5 shrink-0 leading-none">●</span>
  ) : null;

  if (isApCredit) {
    return (
      <div
        title={`${tooltip} — satisfied by AP credit`}
        className="flex items-center gap-1 rounded-full px-2.5 py-1 bg-emerald-950/40 border border-emerald-700/40 min-w-0 overflow-hidden"
      >
        {dot}
        <span className={`${codeSize} font-medium leading-none truncate text-emerald-400`}>{courseId}</span>
        <span className="text-[7px] font-bold leading-none text-emerald-500 uppercase tracking-wide shrink-0">AP</span>
      </div>
    );
  }

  if (isPlaced) {
    return (
      <div
        title={tooltip}
        className="flex items-center justify-center rounded-full px-2.5 py-1 bg-[#3b82f6]/20 border border-[#3b82f6]/35 min-w-0 overflow-hidden"
      >
        {dot}
        <span className={`${codeSize} font-medium leading-none truncate text-[#93c5fd]`}>
          {courseId}
        </span>
      </div>
    );
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...listeners}
      {...attributes}
      title={tooltip}
      className={`flex items-center justify-center rounded-full px-2.5 py-1 border cursor-grab active:cursor-grabbing select-none transition-colors min-w-0 overflow-hidden
        ${unavailable
          ? "border-dashed border-[#252525] bg-transparent"
          : isDragging
            ? "border-[#3b82f6]/40 bg-[#3b82f6]/10 opacity-40"
            : "border-[#333] bg-transparent hover:border-[#555] hover:bg-[#1a1a1a]"
        }`}
    >
      {!unavailable && dot}
      <span className={`${codeSize} font-medium leading-none truncate ${unavailable ? "text-[#333]" : "text-[#bbb]"}`}>
        {courseId}
      </span>
    </div>
  );
}

// ── Requirement group ─────────────────────────────────────────────────────────

function RequirementGroup({
  req, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery, initialOpen, getCoverage,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  initialOpen?: boolean;
  getCoverage: GetCoverage;
}) {
  const [open, setOpen] = useState(initialOpen ?? false);

  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter(
      (cid) =>
        cid.toLowerCase().includes(q) ||
        (courseInfoMap[cid]?.title ?? "").toLowerCase().includes(q),
    );
  }, [req.courses, searchQuery, courseInfoMap]);

  if (filtered.length === 0) return null;

  const { placed, needed, done } = getCoverage(req);
  const partial = !done && placed > 0;
  const accentColor = done ? "#22c55e" : partial ? "#3b82f6" : "transparent";

  return (
    <div
      className="mx-1 mb-[2px] overflow-hidden"
      style={{ borderLeft: `3px solid ${accentColor}` }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center px-2.5 py-2 hover:bg-[#1c1c1c] transition-colors text-left gap-2 bg-[#141414]"
      >
        <div className="flex-1 min-w-0">
          <span className="text-[12px] font-normal text-[#ccc] block truncate">
            {req.group_name}
          </span>
          {req.courses_needed < req.courses.length && (
            <span className="text-[10px] text-amber-500/70 leading-none font-medium">
              pick {req.courses_needed} of {req.courses.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="text-[10px] px-1.5 py-[2px] rounded bg-[#1e1e1e] font-mono tabular-nums text-[#555]">
            {placed}/{needed}
          </span>
          <ChevronIcon open={open} />
        </div>
      </button>
      {open && (
        <div className="grid grid-cols-3 gap-1.5 p-2 bg-[#0f0f0f]">
          {filtered.map((cid) => (
            <CoursePill
              key={cid}
              courseId={cid}
              title={courseInfoMap[cid]?.title}
              units={courseInfoMap[cid]?.min_units}
              isPlaced={placedSet.has(normId(cid))}
              isApCredit={apCreditedSet.has(cid)}
              unavailable={!courseInfoMap[cid]}
              diffScore={difficultyMap[cid] ?? null}
              compact={req.courses_needed < req.courses.length}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Flat group (inline pills, no expand) ──────────────────────────────────────

function FlatGroup({
  req, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery, getCoverage,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  getCoverage: GetCoverage;
}) {
  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter(
      (cid) => cid.toLowerCase().includes(q) || (courseInfoMap[cid]?.title ?? "").toLowerCase().includes(q),
    );
  }, [req.courses, searchQuery, courseInfoMap]);

  if (filtered.length === 0) return null;

  const { done } = getCoverage(req);
  const isChoice = req.courses_needed < req.courses.length;
  const showLabel = isChoice || req.courses.length > 1;

  return (
    <div className="px-2 pt-1.5 pb-2">
      {showLabel && (
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="text-[11px] text-[#5a5a5a] flex-1 truncate">{req.group_name}</span>
          {isChoice && (
            <span className={`text-[9px] font-mono shrink-0 ${done ? "text-[#22c55e]" : "text-[#444]"}`}>
              pick {req.courses_needed}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-1.5">
        {filtered.map((cid) => (
          <CoursePill
            key={cid}
            courseId={cid}
            title={courseInfoMap[cid]?.title}
            units={courseInfoMap[cid]?.min_units}
            isPlaced={placedSet.has(normId(cid))}
            isApCredit={apCreditedSet.has(cid)}
            unavailable={!courseInfoMap[cid]}
            diffScore={difficultyMap[cid] ?? null}
          />
        ))}
      </div>
    </div>
  );
}

// ── Bucket section ────────────────────────────────────────────────────────────

function BucketSection({
  bucketKey, groups, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery, defaultOpen, getCoverage,
}: {
  bucketKey: BucketKey;
  groups: ReqGroup[];
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  defaultOpen: boolean;
  getCoverage: GetCoverage;
}) {
  const [open, setOpen] = useState(defaultOpen);

  if (groups.length === 0) return null;

  const covs = groups.map((r) => getCoverage(r));
  const totalNeeded = covs.reduce((s, c) => s + c.needed, 0);
  const totalPlaced = covs.reduce((s, c) => s + Math.min(c.placed, c.needed), 0);
  const done = totalPlaced >= totalNeeded;
  const partial = !done && totalPlaced > 0;
  const firstIncomplete = covs.findIndex((c) => !c.done);

  return (
    <div className="mb-px">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center px-2 py-[9px] hover:bg-[#1a1a1a] transition-colors text-left gap-2"
      >
        <span className="flex-1 text-[8px] font-bold uppercase tracking-[0.15em] text-[#444]">
          {BUCKET_LABELS[bucketKey]}
        </span>
        <span
          className={`text-[9px] px-1.5 py-[2px] rounded bg-[#1e1e1e] font-mono tabular-nums ${
            done ? "text-[#22c55e]" : partial ? "text-[#3b82f6]" : "text-[#555]"
          }`}
        >
          {totalPlaced}/{totalNeeded}
        </span>
        <ChevronIcon open={open} />
      </button>
      {open && (
        <div className={bucketKey === "elective" || bucketKey === "choice" ? "border-l-2 border-[#1e1e1e] ml-3" : "pt-0.5 pb-1"}>
          {groups.map((r, i) =>
            bucketKey === "elective" || bucketKey === "choice" || r.courses.length > INLINE_THRESHOLD || r.courses_needed < r.courses.length ? (
              <RequirementGroup
                key={r.id}
                req={r}
                placedSet={placedSet}
                apCreditedSet={apCreditedSet}
                courseInfoMap={courseInfoMap}
                difficultyMap={difficultyMap}
                searchQuery={searchQuery}
                initialOpen={i === firstIncomplete}
                getCoverage={getCoverage}
              />
            ) : (
              <FlatGroup
                key={r.id}
                req={r}
                placedSet={placedSet}
                apCreditedSet={apCreditedSet}
                courseInfoMap={courseInfoMap}
                difficultyMap={difficultyMap}
                searchQuery={searchQuery}
                getCoverage={getCoverage}
              />
            ),
          )}
        </div>
      )}
    </div>
  );
}

// ── GE section ─────────────────────────────────────────────────────────────────

function GESection({
  req, placedSet, apCreditedSet, apSatisfiedGEs, courseInfoMap, difficultyMap, searchQuery, initialOpen, getCoverage,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  apSatisfiedGEs: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  initialOpen?: boolean;
  getCoverage: GetCoverage;
}) {
  const [open, setOpen] = useState(initialOpen ?? false);

  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter(
      (cid) =>
        cid.toLowerCase().includes(q) ||
        (courseInfoMap[cid]?.title ?? "").toLowerCase().includes(q),
    );
  }, [req.courses, searchQuery, courseInfoMap]);

  if (filtered.length === 0) return null;

  // AP exam directly satisfies this GE category (e.g. AP World History → GE-VIII)
  const apDirect = apSatisfiedGEs.has(req.requirement_group ?? "");

  const cov = getCoverage(req);
  const satisfied = apDirect ? cov.needed : Math.min(cov.placed, cov.needed);
  const done = apDirect ? true : cov.done;
  const partial = !done && satisfied > 0;
  const accentColor = done ? "#22c55e" : partial ? "#3b82f6" : "transparent";

  return (
    <div
      className="mx-1 mb-[2px] overflow-hidden"
      style={{ borderLeft: `3px solid ${accentColor}` }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center px-2 py-[7px] hover:bg-[#1c1c1c] transition-colors text-left gap-2 bg-[#141414]"
      >
        <div className="flex-1 min-w-0">
          <span className="text-[10px] font-normal text-[#ccc] block truncate">{req.group_name}</span>
          {req.courses_needed < req.courses.length && (
            <span className="text-[9px] text-amber-500/70 leading-none font-medium">
              pick {req.courses_needed} of {req.courses.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {apDirect && (
            <span className="text-[7px] font-bold uppercase tracking-wide px-1 py-[2px] rounded bg-emerald-900/40 border border-emerald-700/40 text-emerald-400">
              AP
            </span>
          )}
          <span className="text-[9px] px-1.5 py-[2px] rounded bg-[#1e1e1e] font-mono tabular-nums text-[#555]">
            {satisfied}/{cov.needed}
          </span>
          <ChevronIcon open={open} />
        </div>
      </button>
      {open && (
        <div className="grid grid-cols-3 gap-1 p-1.5 bg-[#0f0f0f] max-h-[300px] overflow-y-auto">
          {filtered.map((cid) => (
            <CoursePill
              key={cid}
              courseId={cid}
              title={courseInfoMap[cid]?.title}
              units={courseInfoMap[cid]?.min_units}
              isPlaced={placedSet.has(normId(cid))}
              isApCredit={apCreditedSet.has(cid)}
              unavailable={!courseInfoMap[cid]}
              diffScore={difficultyMap[cid] ?? null}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Top-bar: AP Credits popover ─────────────────────────────────────────────

function APCreditsMenu({
  apScores, setApScores, apExamNames,
}: {
  apScores: Record<string, number>;
  setApScores: React.Dispatch<React.SetStateAction<Record<string, number>>>;
  apExamNames: string[];
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const count = Object.keys(apScores).length;

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const filtered = apExamNames.filter(
    (n) => !query || n.toLowerCase().includes(query.toLowerCase()),
  );

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-1.5 rounded-md border px-2.5 h-7 text-[11px] font-medium transition-colors
          ${count > 0
            ? "border-[#3b82f6]/40 bg-[#3b82f6]/10 text-[#93c5fd]"
            : "border-[#2a2a2a] bg-[#1a1a1a] text-[#999] hover:text-[#e8e8e8] hover:border-[#3a3a3a]"}`}
      >
        <svg viewBox="0 0 16 16" className="w-3.5 h-3.5" fill="none">
          <path d="M8 2L2 5l6 3 6-3-6-3z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/>
          <path d="M4 6.5V10c0 1 1.8 2 4 2s4-1 4-2V6.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/>
        </svg>
        AP Credits
        {count > 0 && (
          <span className="ml-0.5 rounded-full bg-[#3b82f6] text-white text-[9px] font-bold leading-none px-1.5 py-0.5 tabular-nums">
            {count}
          </span>
        )}
        <svg viewBox="0 0 12 12" className={`w-2.5 h-2.5 transition-transform ${open ? "rotate-180" : ""}`} fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
        </svg>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1.5 w-72 rounded-lg border border-[#2a2a2a] bg-[#1a1a1a] shadow-2xl z-50 overflow-hidden">
          <div className="px-3 py-2.5 border-b border-[#2a2a2a]">
            <p className="text-[11px] font-semibold text-[#e8e8e8]">AP / Exam Credit</p>
            <p className="text-[9.5px] text-[#666] mt-0.5 leading-snug">
              Add your scores so satisfied courses are pre-filled.
            </p>
          </div>
          <div className="px-2.5 pt-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search AP exams…"
              className="w-full rounded border border-[#2a2a2a] bg-[#141414] px-2 py-1.5 text-[10px] text-[#ccc] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/40"
            />
          </div>
          <div className="max-h-64 overflow-y-auto px-2.5 py-2 space-y-0.5">
            {filtered.map((examName) => {
              const score = apScores[examName];
              return (
                <div key={examName} className="flex items-center justify-between gap-2 py-0.5">
                  <span className="text-[10px] text-[#aaa] leading-tight flex-1 min-w-0 truncate">
                    {examName.replace(/^AP /, "")}
                  </span>
                  <div className="flex gap-0.5 shrink-0">
                    {([3, 4, 5] as const).map((s) => (
                      <button
                        key={s}
                        onClick={() =>
                          setApScores((prev) => {
                            if (prev[examName] === s) {
                              const next = { ...prev };
                              delete next[examName];
                              return next;
                            }
                            return { ...prev, [examName]: s };
                          })
                        }
                        className={`w-6 h-6 rounded text-[9px] font-bold transition-all
                          ${score === s
                            ? "bg-[#3b82f6] text-white"
                            : "bg-[#141414] border border-[#2a2a2a] text-[#555] hover:text-[#ccc] hover:border-[#3a3a3a]"}`}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
            {filtered.length === 0 && (
              <p className="text-[10px] text-[#444] text-center py-4">No exams match</p>
            )}
          </div>
          {count > 0 && (
            <div className="border-t border-[#2a2a2a] px-2.5 py-2 flex items-center justify-between">
              <span className="text-[9.5px] text-[#666]">{count} exam{count !== 1 ? "s" : ""} added</span>
              <button
                onClick={() => setApScores({})}
                className="text-[10px] text-[#777] hover:text-red-400 transition-colors"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Top-bar: Saved Plans popover ────────────────────────────────────────────

function PlansMenu({
  signedIn, savedPlans, maxPlans, onSave, onLoad, onDelete, onSignIn,
}: {
  signedIn: boolean;
  savedPlans: SavedPlanMeta[];
  maxPlans: number;
  onSave: (name: string) => Promise<{ ok: boolean; reason?: string }>;
  onLoad: (id: number) => void;
  onDelete: (id: number) => void;
  onSignIn: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const atCap = savedPlans.length >= maxPlans;

  async function handleSave() {
    if (!name.trim() || saving) return;
    setSaving(true);
    setMsg(null);
    const res = await onSave(name.trim());
    setSaving(false);
    if (res.ok) {
      setName("");
      setMsg("Saved");
      setTimeout(() => setMsg(null), 1800);
    } else if (res.reason === "cap_reached") {
      setMsg(`Limit reached — delete a plan to save a new one.`);
    } else {
      setMsg("Could not save — try again.");
    }
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-md border border-[#2a2a2a] bg-[#1a1a1a] px-2.5 h-7 text-[11px] font-medium text-[#999] hover:text-[#e8e8e8] hover:border-[#3a3a3a] transition-colors"
      >
        <svg viewBox="0 0 16 16" className="w-3.5 h-3.5" fill="none">
          <path d="M3 3.5A1.5 1.5 0 014.5 2h5.8a1.5 1.5 0 011.06.44l1.2 1.2A1.5 1.5 0 0113 4.7V12.5A1.5 1.5 0 0111.5 14h-7A1.5 1.5 0 013 12.5v-9z" stroke="currentColor" strokeWidth="1.2"/>
          <path d="M5.5 2.5v3h4v-3M5.5 9.5h5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
        </svg>
        Plans
        {signedIn && (
          <span className="ml-0.5 text-[9px] text-[#555] tabular-nums">{savedPlans.length}/{maxPlans}</span>
        )}
        <svg viewBox="0 0 12 12" className={`w-2.5 h-2.5 transition-transform ${open ? "rotate-180" : ""}`} fill="none">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
        </svg>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1.5 w-80 rounded-lg border border-[#2a2a2a] bg-[#1a1a1a] shadow-2xl z-50 overflow-hidden">
          {!signedIn ? (
            <div className="px-4 py-5 text-center">
              <p className="text-[12px] font-semibold text-[#e8e8e8] mb-1">Sign in to save plans</p>
              <p className="text-[10px] text-[#666] leading-snug mb-3">
                Save up to {maxPlans} schedule versions and switch between them anytime.
              </p>
              <button
                onClick={onSignIn}
                className="w-full rounded-md bg-[#3b82f6] hover:bg-[#2563eb] text-white text-[11px] font-semibold py-2 transition-colors"
              >
                Sign in
              </button>
            </div>
          ) : (
            <>
              <div className="px-3 py-2.5 border-b border-[#2a2a2a]">
                <div className="flex items-center justify-between mb-1.5">
                  <p className="text-[11px] font-semibold text-[#e8e8e8]">Save current schedule</p>
                  <span className="text-[9px] text-[#555] tabular-nums">{savedPlans.length}/{maxPlans} saved</span>
                </div>
                <div className="flex gap-1.5">
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") handleSave(); }}
                    placeholder="Plan name…"
                    maxLength={80}
                    className="flex-1 min-w-0 rounded border border-[#2a2a2a] bg-[#141414] px-2 py-1.5 text-[10px] text-[#ccc] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/40"
                  />
                  <button
                    onClick={handleSave}
                    disabled={!name.trim() || saving}
                    className="rounded bg-[#3b82f6] hover:bg-[#2563eb] disabled:bg-[#222] disabled:text-[#555] text-white text-[10px] font-semibold px-3 transition-colors"
                  >
                    {saving ? "…" : "Save"}
                  </button>
                </div>
                {msg && <p className="text-[9.5px] text-[#888] mt-1.5">{msg}</p>}
                {atCap && !msg && (
                  <p className="text-[9.5px] text-amber-500/80 mt-1.5">
                    At the {maxPlans}-plan limit — reusing a name overwrites; delete one to add another.
                  </p>
                )}
              </div>
              <div className="max-h-64 overflow-y-auto">
                {savedPlans.length === 0 ? (
                  <p className="text-[10px] text-[#555] text-center py-6">No saved plans yet</p>
                ) : (
                  savedPlans.map((p) => (
                    <div key={p.id} className="flex items-center gap-2 px-3 py-2 border-b border-[#222] last:border-b-0 hover:bg-[#1f1f1f] transition-colors">
                      <div className="flex-1 min-w-0">
                        <p className="text-[11px] text-[#e0e0e0] font-medium truncate">{p.name}</p>
                        <p className="text-[9px] text-[#555]">
                          {p.plan_data?.selectedDisplayName || "—"}
                          {p.updated_at ? ` · ${new Date(p.updated_at).toLocaleDateString()}` : ""}
                        </p>
                      </div>
                      <button
                        onClick={() => { onLoad(p.id); setOpen(false); }}
                        className="shrink-0 rounded border border-[#2a2a2a] text-[9.5px] text-[#aaa] hover:text-white hover:border-[#3b82f6]/50 px-2 py-1 transition-colors"
                      >
                        Load
                      </button>
                      <button
                        onClick={() => onDelete(p.id)}
                        title="Delete plan"
                        className="shrink-0 text-[#444] hover:text-red-400 text-[13px] leading-none w-4 text-center transition-colors"
                      >
                        ×
                      </button>
                    </div>
                  ))
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function PlannerClient() {
  // ── Data state ─────────────────────────────────────────────────────────────
  const [majorList, setMajorList] = useState<MajorOption[]>([]);
  const [requirements, setRequirements] = useState<ReqGroup[]>([]);
  const [geRequirements, setGeRequirements] = useState<ReqGroup[]>([]);
  const [minorList, setMinorList] = useState<MinorOption[]>([]);
  const [minorRequirements, setMinorRequirements] = useState<ReqGroup[]>([]);
  const [courseInfoMap, setCourseInfoMap] = useState<Record<string, CourseDetail>>({});
  // programNames removed — spec names now come from MajorOption.specialization_name

  // ── Error + retry state ────────────────────────────────────────────────────
  const [majorListError, setMajorListError] = useState<string | null>(null);
  const [reqError, setReqError] = useState<string | null>(null);
  const [geError, setGeError] = useState<string | null>(null);
  const [majorListRetry, setMajorListRetry] = useState(0);
  const [reqRetry, setReqRetry] = useState(0);
  const [geRetry, setGeRetry] = useState(0);
  const [minorListError, setMinorListError] = useState<string | null>(null);
  const [minorReqError, setMinorReqError] = useState<string | null>(null);
  const [minorListRetry, setMinorListRetry] = useState(0);
  const [minorReqRetry, setMinorReqRetry] = useState(0);
  const [loadingMinorReqs, setLoadingMinorReqs] = useState(false);

  // ── UI state ───────────────────────────────────────────────────────────────
  const [sidebarTab, setSidebarTab] = useState<"major" | "ge" | "minor">("major");
  const [selectedDisplayName, setSelectedDisplayName] = useState("");
  const [selectedMajorId, setSelectedMajorId] = useState("");
  const [selectedMinorId, setSelectedMinorId] = useState("");
  const [numYears,    setNumYears]      = useState(DEFAULT_YEARS);
  const [maxUnits,    setMaxUnits]      = useState(16);
  const [plannedCourses, setPlannedCourses] = useState<PlannedCourses>({});
  const [activeData, setActiveData] = useState<DragData | null>(null);
  const [loadingReqs, setLoadingReqs] = useState(false);
  const [summerYears, setSummerYears] = useState<Set<number>>(new Set());
  const [lockedCourses, setLockedCourses] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState("");
  const [autoFillLoading, setAutoFillLoading] = useState(false);
  const [optimizeLoading, setOptimizeLoading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [prereqWarnings, setPrereqWarnings] = useState<Record<string, string>>({});
  // Coverage pills for cross-covering autofilled courses (ephemeral — recomputed
  // on each autofill, auto-pruned when a course leaves the plan; not persisted).
  const [coverageTags, setCoverageTags] = useState<Record<string, CoverageTag[]>>({});
  const [difficultyMap, setDifficultyMap] = useState<Record<string, number>>({});
  const [optimizerOnline, setOptimizerOnline] = useState<boolean | null>(null);
  const [apScores, setApScores] = useState<Record<string, number>>({});
  const [apExamNames, setApExamNames] = useState<string[]>([]);
  const [apCreditedSet, setApCreditedSet] = useState<Set<string>>(new Set());
  const [apSatisfiedGEs, setApSatisfiedGEs] = useState<Set<string>>(new Set());
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [lockConflictErrors, setLockConflictErrors] = useState<string[] | null>(null);
  const [pendingLock, setPendingLock] = useState<{ courseId: string; warnings: string[] } | null>(null);
  const [clearSuccess, setClearSuccess] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [savedPlans, setSavedPlans] = useState<SavedPlanMeta[]>([]);
  // Prereq-chain proposal awaiting user confirmation (additive — does not block the
  // dropped course; only offers to add its missing prerequisites).
  const [prereqProposal, setPrereqProposal] = useState<{
    courseId:   string;
    placements: { course_id: string; quarter: string; reason?: string }[];
    plan:       PlannedCourses;
  } | null>(null);
  const [prereqApplying, setPrereqApplying] = useState(false);
  // Backend-computed requirement coverage — the authoritative source for badges,
  // bucket counts and "all satisfied". Null until the first fetch returns; kept at
  // the previous value while a refetch is in flight (no flicker).
  const [requirementsState, setRequirementsState] = useState<RequirementsState | null>(null);

  const supabase = useMemo(() => createClient(), []);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveEnabledRef = useRef(false);
  const courseInfoMapRef = useRef<Record<string, CourseDetail>>({});
  courseInfoMapRef.current = courseInfoMap;
  const [globalResults, setGlobalResults] = useState<CourseDetail[]>([]);

  // ── Derived ────────────────────────────────────────────────────────────────
  // Graduation quarter is structural, no longer a user input: the Spring of the
  // last displayed year. Kept so optimizer/profile/save calls remain valid until
  // the optimizer is rewired separately.
  const gradQuarter = useMemo(() => qkey(numYears, "spring"), [numYears]);

  // Normalized set of placed (+ AP-credited) course IDs.  Membership tests below
  // normalize the queried ID too, so pool IDs in any alias/spacing form match.
  const placedSet = useMemo(() => {
    const s = new Set<string>();
    for (const ids of Object.values(plannedCourses)) ids.forEach((id) => s.add(normId(id)));
    // AP-credited courses count as "placed" for sidebar coverage checks
    apCreditedSet.forEach((id) => s.add(normId(id)));
    return s;
  }, [plannedCourses, apCreditedSet]);

  const totalUnits = useMemo(
    () =>
      Object.values(plannedCourses).reduce(
        (sum, ids) => sum + ids.reduce((s, id) => s + (courseInfoMap[id]?.min_units ?? 4), 0),
        0,
      ),
    [plannedCourses, courseInfoMap],
  );

  // True when at least one course sits in a quarter — the Optimize button needs
  // a non-empty plan to rebalance.
  const hasPlacedCourses = useMemo(
    () => Object.values(plannedCourses).some((ids) => ids.length > 0),
    [plannedCourses],
  );

  // Parent majors: rows with no specialization_name — these populate the first dropdown
  const parentMajors = useMemo(
    () => majorList.filter((m) => !m.specialization_name),
    [majorList],
  );

  // Specialization options for the currently selected parent major
  const specializations = useMemo(
    () =>
      selectedDisplayName
        ? majorList.filter((m) => m.display_name === selectedDisplayName && m.specialization_name)
        : [],
    [majorList, selectedDisplayName],
  );

  const selectedLabel = useMemo(() => {
    if (!selectedMajorId) return "";
    const spec = specializations.find((s) => s.major_id === selectedMajorId);
    return spec?.specialization_name
      ? `${selectedDisplayName} — ${spec.specialization_name}`
      : selectedDisplayName || selectedMajorId;
  }, [selectedMajorId, selectedDisplayName, specializations]);

  // ── Coverage source of truth ─────────────────────────────────────────────
  // getCoverage(req) is the SINGLE place coverage is computed. For a choice /
  // elective / GE pick-N group it reads the backend's backbone-aware numbers; for
  // a take-all required group (no backend entry) — or before the first backend
  // response — it falls back to the client count (correct & unchanged for those).
  const getCoverage = useMemo<GetCoverage>(() => {
    const byKey = new Map<string, BackendChoiceGroup>();
    if (requirementsState) {
      for (const g of requirementsState.choice_groups) {
        if (g.group_id) byKey.set(g.group_id, g);
        if (g.label) byKey.set(g.label, g);
      }
    }
    return (req: ReqGroup): Coverage => {
      const be = byKey.get(req.requirement_group ?? "") ?? byKey.get(req.group_name);
      if (be) {
        return {
          placed: be.placed,
          needed: be.choose_n,
          remaining: be.remaining,
          done: be.remaining === 0,
        };
      }
      const placed = req.courses.filter((c) => placedSet.has(normId(c))).length;
      return {
        placed,
        needed: req.courses_needed,
        remaining: Math.max(0, req.courses_needed - placed),
        done: placed >= req.courses_needed,
      };
    };
  }, [requirementsState, placedSet]);

  // Client-only coverage — used for the MINOR tab, which the backend
  // requirements_state (major-scoped) doesn't cover. Keeps minor behavior identical
  // and avoids any major/minor group-name collision in the backend lookup.
  const clientCoverage = useMemo<GetCoverage>(() => {
    return (req: ReqGroup): Coverage => {
      const placed = req.courses.filter((c) => placedSet.has(normId(c))).length;
      return {
        placed,
        needed: req.courses_needed,
        remaining: Math.max(0, req.courses_needed - placed),
        done: placed >= req.courses_needed,
      };
    };
  }, [placedSet]);

  // Fetch backend coverage AFTER a placement settles (debounced — not mid-drag).
  // Keep the previous requirementsState while in flight so badges don't flicker.
  useEffect(() => {
    if (!selectedMajorId) {
      setRequirementsState(null);
      return;
    }
    const handle = setTimeout(() => {
      fetch("/api/requirements-state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: {
            major_id: selectedMajorId,
            completed_courses: [],
            planned_courses: plannedCourses,
            graduation_year: parseInt(gradQuarter.split("_")[0]),
            units_per_quarter: maxUnits,
          },
          waived_ges: [],
          ap_scores: apScores,
        }),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (data && Array.isArray(data.choice_groups)) {
            setRequirementsState(data as RequirementsState);
          }
        })
        .catch(() => {
          /* keep previous coverage on error */
        });
    }, 400);
    return () => clearTimeout(handle);
  }, [plannedCourses, selectedMajorId, maxUnits, apScores, gradQuarter]);

  const totalRequired = useMemo(
    () => requirements.reduce((s, r) => s + getCoverage(r).needed, 0),
    [requirements, getCoverage],
  );

  const placedRequired = useMemo(
    () =>
      requirements.reduce((s, r) => {
        const cov = getCoverage(r);
        return s + Math.min(cov.placed, cov.needed);
      }, 0),
    [requirements, getCoverage],
  );

  const minorTotalRequired = useMemo(
    () => minorRequirements.reduce((s, r) => s + r.courses_needed, 0),
    [minorRequirements],
  );

  const minorPlacedRequired = useMemo(
    () =>
      [...placedSet].filter((id) => minorRequirements.some((r) => r.courses.some((c) => normId(c) === id))).length,
    [placedSet, minorRequirements],
  );

  // Programs API (anteaterapi.com/v2/rest/programs) is IP-banned until the
  // Cloudflare block from rate-limit abuse clears. Spec names fall back to
  // raw major_id codes until then.

  // ── Batch difficulty fetch ─────────────────────────────────────────────────
  const fetchDifficulties = useCallback(async (ids: string[]) => {
    if (ids.length === 0) return;
    try {
      const res = await fetch("/api/course-difficulties", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
      if (!res.ok) return;
      const d = await res.json();
      if (d?.scores) setDifficultyMap((prev) => ({ ...prev, ...d.scores }));
    } catch {}
  }, []);

  // Apply a serialized plan to all editor state (mount autosave + loading a
  // saved version both route through here).
  const applyPlanData = useCallback((plan: PlanData) => {
    setPlannedCourses(plan.plannedCourses);
    setSelectedMajorId(plan.selectedMajorId);
    setSelectedDisplayName(plan.selectedDisplayName);
    setSelectedMinorId(plan.selectedMinorId ?? "");
    setNumYears(plan.numYears ?? DEFAULT_YEARS);
    setMaxUnits(plan.maxUnits);
    setLockedCourses(new Set(plan.lockedCourses));
    setApScores(plan.apScores);
    setSummerYears(new Set(plan.summerYears));
  }, []);

  // ── Load plan + sync profile on mount ─────────────────────────────────────
  useEffect(() => {
    supabase.auth.getUser().then(async ({ data }) => {
      if (!data.user) { saveEnabledRef.current = true; return; }
      setSignedIn(true);
      const plan = await loadPlan();
      if (plan) applyPlanData(plan);
      listNamedPlans().then(setSavedPlans).catch(() => {});
      saveEnabledRef.current = true;
      syncUserProfile({
        majorCode: plan?.selectedMajorId ?? "",
        gradQuarter: qkey(plan?.numYears ?? DEFAULT_YEARS, "spring"),
        preferredMaxUnits: plan?.maxUnits ?? 16,
      }).catch(() => {});
    }).catch(() => { saveEnabledRef.current = true; });
  }, [supabase, applyPlanData]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Auto-save (debounced 500ms) on plan state change ───────────────────────
  useEffect(() => {
    if (!saveEnabledRef.current) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      savePlan({
        plannedCourses,
        selectedMajorId,
        selectedDisplayName,
        selectedMinorId,
        numYears,
        gradQuarter,         // derived (Spring of last year); kept for /plans + optimizer
        maxUnits,
        lockedCourses: [...lockedCourses],
        apScores,
        summerYears: [...summerYears],
      }).catch(() => {});
    }, 500);
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
  }, [plannedCourses, selectedMajorId, selectedDisplayName, selectedMinorId, numYears, gradQuarter, maxUnits, lockedCourses, apScores, summerYears]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Auto-fetch details for placed courses not in courseInfoMap ────────────
  useEffect(() => {
    const placed = [...new Set(Object.values(plannedCourses).flat())];
    const missing = placed.filter((id) => !courseInfoMapRef.current[id]);
    if (missing.length === 0) return;
    fetchCourseDetails(missing).then((details) => {
      if (!details.length) return;
      setCourseInfoMap((prev) => {
        const next = { ...prev };
        for (const c of details) next[c.id] = c;
        return next;
      });
      fetchDifficulties(missing);
    }).catch(() => {});
  }, [plannedCourses, fetchDifficulties]);

  // ── Prune coverage pills for courses no longer in the plan ────────────────
  // Keeps tags in sync when a course is moved out / removed manually. Tags set
  // immediately after an autofill survive because the new plan still holds them.
  useEffect(() => {
    const placedNow = new Set(Object.values(plannedCourses).flat());
    setCoverageTags((prev) => {
      let changed = false;
      const next: Record<string, CoverageTag[]> = {};
      for (const [id, t] of Object.entries(prev)) {
        if (placedNow.has(id)) next[id] = t;
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [plannedCourses]);

  // ── Global course search (any course, not just requirements) ──────────────
  useEffect(() => {
    const q = searchQuery.trim();
    if (q.length < 2) { setGlobalResults([]); return; }
    const timer = setTimeout(async () => {
      try {
        const qNorm = q.replace(/\s+/g, "").toUpperCase();
        const { data } = await supabase
          .from("courses")
          .select("id, title, min_units, description, course_level, terms, avg_gpa, ge_list")
          .or(`id.ilike.${qNorm}%,title.ilike.%${q}%`)
          .limit(20);
        const results = (data ?? []) as CourseDetail[];
        setGlobalResults(results);
        if (results.length > 0) {
          setCourseInfoMap((prev) => {
            const next = { ...prev };
            for (const c of results) next[c.id] = c;
            return next;
          });
          fetchDifficulties(results.map((c) => c.id));
        }
      } catch { setGlobalResults([]); }
    }, 250);
    return () => clearTimeout(timer);
  }, [searchQuery, supabase, fetchDifficulties]);

  // ── Fetch major list ───────────────────────────────────────────────────────
  useEffect(() => {
    setMajorListError(null);
    fetchMajors()
      .then(setMajorList)
      .catch((e: Error) => setMajorListError(e.message));
  }, [majorListRetry]);

  // ── Fetch minor list ───────────────────────────────────────────────────────
  useEffect(() => {
    setMinorListError(null);
    fetchMinors()
      .then(setMinorList)
      .catch((e: Error) => setMinorListError(e.message));
  }, [minorListRetry]);

  // ── Fetch AP exam names once on mount ──────────────────────────────────────
  useEffect(() => {
    fetchApExamNames().then(setApExamNames).catch(() => {});
  }, []);

  // ── Re-resolve AP credits whenever apScores changes ────────────────────────
  useEffect(() => {
    resolveApCredits(apScores)
      .then(({ courses, geGroups }: ApCreditResult) => {
        setApCreditedSet(courses);
        setApSatisfiedGEs(geGroups);
      })
      .catch(() => {
        setApCreditedSet(new Set());
        setApSatisfiedGEs(new Set());
      });
  }, [apScores]);

  // ── Fetch GE requirements ──────────────────────────────────────────────────
  useEffect(() => {
    setGeError(null);
    fetchGERequirements()
      .then(async (reqs) => {
        setGeRequirements(reqs);
        const allIds = [...new Set(reqs.flatMap((r) => r.courses))];
        const details = await fetchCourseDetails(allIds);
        setCourseInfoMap((prev) => {
          const next = { ...prev };
          for (const c of details) next[c.id] = c;
          return next;
        });
        fetchDifficulties(allIds);
      })
      .catch((e: Error) => setGeError(e.message));
  }, [geRetry, fetchDifficulties]);

  // ── Fetch major requirements + course details ──────────────────────────────
  useEffect(() => {
    if (!selectedMajorId) {
      setRequirements([]);
      return;
    }
    setLoadingReqs(true);
    setReqError(null);
    // For specialization rows, also fetch parent program requirements (merged coverage).
    const parentId = specializations.length > 0
      ? selectedMajorId.replace(/[A-Z]$/, "")
      : undefined;
    fetchMajorRequirements(selectedMajorId, parentId)
      .then(async (reqs) => {
        setRequirements(reqs);
        const allIds = [...new Set(reqs.flatMap((r) => r.courses))];
        const details = await fetchCourseDetails(allIds);
        setCourseInfoMap((prev) => {
          const next = { ...prev };
          for (const c of details) next[c.id] = c;
          return next;
        });
        fetchDifficulties(allIds);
      })
      .catch((e: Error) => setReqError(e.message))
      .finally(() => setLoadingReqs(false));
  }, [selectedMajorId, reqRetry, fetchDifficulties]);

  // ── Fetch minor requirements + course details ───────────────────────────────
  useEffect(() => {
    if (!selectedMinorId) {
      setMinorRequirements([]);
      return;
    }
    setLoadingMinorReqs(true);
    setMinorReqError(null);
    fetchMinorRequirements(selectedMinorId)
      .then(async (reqs) => {
        setMinorRequirements(reqs);
        const allIds = [...new Set(reqs.flatMap((r) => r.courses))];
        const details = await fetchCourseDetails(allIds);
        setCourseInfoMap((prev) => {
          const next = { ...prev };
          for (const c of details) next[c.id] = c;
          return next;
        });
        fetchDifficulties(allIds);
      })
      .catch((e: Error) => setMinorReqError(e.message))
      .finally(() => setLoadingMinorReqs(false));
  }, [selectedMinorId, minorReqRetry, fetchDifficulties]);

  // ── Prereq validation ──────────────────────────────────────────────────────
  const validatePlan = useCallback(async (placed: PlannedCourses) => {
    const lockedCourses: Record<string, string> = {};
    Object.entries(placed).forEach(([quarter, courses]) => {
      courses.forEach((courseId) => { lockedCourses[courseId] = quarter; });
    });
    if (Object.keys(lockedCourses).length === 0) return;
    const payload = { locked_courses: lockedCourses, completed_courses: [], ap_scores: apScores };
    try {
      const res = await fetch("/api/validate-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) return;
      const data = (await res.json()) as { valid: boolean; conflicts: string[]; online?: boolean };
      setOptimizerOnline(data.online ?? true);
      if (data.valid || data.conflicts.length === 0) {
        setPrereqWarnings({});
      } else {
        const next: Record<string, string> = {};
        for (const conflict of data.conflicts) {
          // Format 1: "COURSEID missing prereq: PREREQID"
          if (conflict.includes(" missing prereq: ")) {
            const [courseId, prereqId] = conflict.split(" missing prereq: ");
            if (courseId && prereqId) {
              next[courseId.trim()] = `Missing prereq: ${prereqId.trim()} not in plan`;
            }
            continue;
          }
          // Format 2: "COURSEID locked to QUARTER: BLOCKER must be placed in an earlier quarter"
          const courseId = conflict.split(" locked to")[0]?.trim();
          if (!courseId) continue;
          const afterColon = conflict.split(": ").slice(1).join(": ");
          const blockerFull = afterColon
            ? afterColon.replace(" must be placed in an earlier quarter", "").trim()
            : "";
          const firstBlocker = blockerFull.split(",")[0].trim();
          const blockerCourse = firstBlocker.split(" (")[0].trim();
          const blocker = !blockerCourse || blockerCourse.startsWith("a ")
            ? "a prerequisite"
            : blockerCourse;
          next[courseId] = `${blocker} must come before this`;
        }
        setPrereqWarnings(next);
      }
    } catch {
      setOptimizerOnline(false);
    }
  }, [apScores]);

  // ── Saved-version handlers (named plans, cap MAX_SAVED_PLANS) ──────────────
  const handleSaveNamed = useCallback(async (name: string) => {
    const res = await saveNamedPlan(name, {
      plannedCourses, selectedMajorId, selectedDisplayName, selectedMinorId,
      numYears, gradQuarter, maxUnits,
      lockedCourses: [...lockedCourses], apScores, summerYears: [...summerYears],
    });
    if (res.ok) listNamedPlans().then(setSavedPlans).catch(() => {});
    return res.ok ? { ok: true } : { ok: false, reason: res.reason };
  }, [plannedCourses, selectedMajorId, selectedDisplayName, selectedMinorId, numYears, gradQuarter, maxUnits, lockedCourses, apScores, summerYears]);

  const handleLoadNamed = useCallback(async (id: number) => {
    const plan = await loadPlanById(id);
    if (plan) { applyPlanData(plan); validatePlan(plan.plannedCourses); }
  }, [applyPlanData, validatePlan]);

  const handleDeleteNamed = useCallback(async (id: number) => {
    await deletePlanById(id);
    listNamedPlans().then(setSavedPlans).catch(() => {});
  }, []);

  const handleSignIn = useCallback(() => {
    supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    }).catch(() => {});
  }, [supabase]);

  // ── Export the current schedule grid to a single-page PDF ──────────────────
  const handleDownloadPDF = useCallback(() => {
    const seasons = [
      { key: "fall", label: "Fall" },
      { key: "winter", label: "Winter" },
      { key: "spring", label: "Spring" },
    ];
    const years: PdfYear[] = Array.from({ length: numYears }, (_, i) => i + 1).map((y) => {
      const quarters = [...seasons];
      if (summerYears.has(y)) quarters.push({ key: "summer", label: "Summer" });
      return {
        label: `Year ${y}`,
        quarters: quarters.map((s) => {
          const ids = plannedCourses[qkey(y, s.key)] ?? [];
          return {
            label: s.label,
            units: ids.reduce((sum, id) => sum + (courseInfoMap[id]?.min_units ?? 4), 0),
            courses: ids.map((id) => ({
              code: id,
              title: courseInfoMap[id]?.title ?? null,
              units: courseInfoMap[id]?.min_units ?? null,
              difficulty: difficultyMap[id] ?? null,
            })),
          };
        }),
      };
    });
    exportScheduleToPDF({ majorName: selectedLabel, totalUnits, years });
  }, [numYears, summerYears, plannedCourses, courseInfoMap, difficultyMap, selectedLabel, totalUnits]);

  // ── DnD ────────────────────────────────────────────────────────────────────
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  function handleDragStart(e: DragStartEvent) {
    setActiveData(e.active.data.current as DragData);
  }

  function handleDragEnd(e: DragEndEvent) {
    setActiveData(null);
    const src = e.active.data.current as DragData;
    const dst =
      e.over?.id?.toString().startsWith("zone|")
        ? (e.over.data.current as { quarterKey: string })
        : undefined;

    if (!dst) {
      if (src.type === "placed" && src.quarterKey) {
        setPlannedCourses((prev) => {
          const next = {
            ...prev,
            [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
          };
          validatePlan(next);
          return next;
        });
      }
      return;
    }

    const tq = dst.quarterKey;
    if (src.type === "sidebar") {
      if (plannedCourses[tq]?.includes(src.courseId)) return;
      const next = { ...plannedCourses, [tq]: [...(plannedCourses[tq] ?? []), src.courseId] };
      setPlannedCourses(next);
      validatePlan(next);
      // Additive: offer to add any missing prerequisite chain (does not block the drop).
      offerPrereqs(next, src.courseId);
    } else if (src.type === "placed" && src.quarterKey && src.quarterKey !== tq) {
      setPlannedCourses((prev) => {
        const next = {
          ...prev,
          [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
          [tq]: [...(prev[tq] ?? []), src.courseId],
        };
        validatePlan(next);
        return next;
      });
    }
  }

  // Detect a just-added course's missing prereq chain and OFFER to add it.
  // Never mutates the plan — only sets prereqProposal for the user to confirm.
  async function offerPrereqs(planSnapshot: PlannedCourses, courseId: string) {
    if (!selectedMajorId) return;
    try {
      const res = await fetch("/api/propose-prereqs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: {
            major_id: selectedMajorId,
            completed_courses: [],
            planned_courses: planSnapshot,
            graduation_year: parseInt(gradQuarter.split("_")[0]),
            units_per_quarter: maxUnits,
          },
          course_id: courseId,
        }),
      });
      if (!res.ok) return; // best-effort: the course stays placed regardless
      const data = await res.json();
      if (data?.status === "infeasible") {
        const reasons: string[] = Array.isArray(data.conflicts)
          ? data.conflicts.map((c: { reason?: string }) => c?.reason ?? String(c))
          : [];
        if (reasons.length) setToast(`${fmtCourse(courseId)}: ${reasons[0]}`);
        return;
      }
      const placements = Array.isArray(data?.proposed_placements)
        ? data.proposed_placements
        : [];
      if (Array.isArray(data?.missing) && data.missing.length > 0 && placements.length > 0) {
        setPrereqProposal({ courseId, placements, plan: planSnapshot });
      }
    } catch {
      // offline / network error — skip the offer; the course remains placed
    }
  }

  // Commit an accepted prereq proposal (separate from detection).
  async function applyProposal() {
    if (!prereqProposal) return;
    setPrereqApplying(true);
    try {
      const res = await fetch("/api/apply-prereqs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: {
            major_id: selectedMajorId,
            completed_courses: [],
            planned_courses: prereqProposal.plan,
            graduation_year: parseInt(gradQuarter.split("_")[0]),
            units_per_quarter: maxUnits,
          },
          proposed_placements: prereqProposal.placements,
        }),
      });
      const data = await res.json();
      const plan = data?.planned_courses as PlannedCourses | undefined;
      if (res.ok && plan) {
        setPlannedCourses(plan);
        validatePlan(plan);
      } else {
        setToast("Could not add prerequisites");
      }
    } catch (err) {
      setToast(err instanceof Error ? err.message : "Could not add prerequisites");
    } finally {
      setPrereqApplying(false);
      setPrereqProposal(null);
    }
  }

  // ── Handlers ───────────────────────────────────────────────────────────────
  const handleMajorNameChange = useCallback(
    (displayName: string) => {
      setSelectedDisplayName(displayName);
      setRequirements([]);
      setReqError(null);
      // Find specs for this parent; if none, use the parent's own major_id
      const specs = majorList.filter((m) => m.display_name === displayName && m.specialization_name);
      if (specs.length > 0) {
        setSelectedMajorId(specs[0].major_id);
      } else {
        const parent = majorList.find((m) => m.display_name === displayName && !m.specialization_name);
        setSelectedMajorId(parent?.major_id ?? "");
      }
    },
    [majorList],
  );

  const toggleLock = useCallback(async (id: string) => {
    // Unlocking — always immediate, no validation needed
    if (lockedCourses.has(id)) {
      setLockedCourses((prev) => { const n = new Set(prev); n.delete(id); return n; });
      return;
    }

    // Find which quarter this course currently lives in
    const quarter = Object.entries(plannedCourses).find(([, cs]) => cs.includes(id))?.[0];
    if (!quarter) {
      setLockedCourses((prev) => new Set([...prev, id]));
      return;
    }

    // Build locked_courses: the new course + already-locked courses
    const toValidate: Record<string, string> = { [id]: quarter };
    for (const [q, cs] of Object.entries(plannedCourses)) {
      for (const c of cs) {
        if (lockedCourses.has(c)) toValidate[c] = q;
      }
    }

    try {
      const res = await fetch("/api/validate-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ locked_courses: toValidate, completed_courses: [], ap_scores: apScores }),
      });
      if (!res.ok) {
        setLockedCourses((prev) => new Set([...prev, id]));
        return;
      }
      const data = await res.json() as { valid: boolean; conflicts: string[] };
      const myConflicts = (data.conflicts ?? []).filter(
        (c) => c.startsWith(id + " missing prereq:") || c.startsWith(id + " locked to")
      );
      if (!myConflicts.length) {
        setLockedCourses((prev) => new Set([...prev, id]));
        return;
      }
      setPendingLock({ courseId: id, warnings: myConflicts });
    } catch {
      // Validation offline — lock anyway
      setLockedCourses((prev) => new Set([...prev, id]));
    }
  }, [lockedCourses, plannedCourses, apScores]); // eslint-disable-line react-hooks/exhaustive-deps

  const removeCourse = useCallback((id: string, qKey: string) => {
    setPlannedCourses((prev) => {
      const next = {
        ...prev,
        [qKey]: prev[qKey]?.filter((c) => c !== id) ?? [],
      };
      validatePlan(next);
      return next;
    });
    setPrereqWarnings((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, [validatePlan]);

  const dismissWarning = useCallback((id: string) => {
    setPrereqWarnings((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  // GE categories an ALREADY-PLACED course counts toward. Autofilled cards keep
  // their (capped) GE pills as the source of truth; everything else falls back
  // to authoritative ge_list. Keeps the "filled" tally consistent with pills.
  const placedGECats = useCallback(
    (id: string, groups: ReqGroup[]): string[] => {
      const t = coverageTags[id];
      if (t && t.some((x) => x.kind === "ge")) {
        return t
          .filter((x): x is Extract<CoverageTag, { kind: "ge" }> => x.kind === "ge")
          .map((x) => GE_LABEL_TO_GROUP[x.code] ?? "")
          .filter((g) => g && groups.some((r) => (r.requirement_group ?? "") === g));
      }
      return getCourseGECategories(courseInfoMapRef.current[id], groups);
    },
    [coverageTags],
  );

  // ── Unified pool → /api/whatif optimizer ────────────────────────────────
  // Shared by all three autofill tabs: pool = unlocked existing + the tab's new
  // picks + auto-pulled missing prereqs; sent to /api/whatif so the real
  // optimizer handles difficulty balancing, prereq ordering, and term
  // availability. Locked courses never move. On infeasible/error the current
  // schedule is left untouched.
  const buildAndOptimizePool = useCallback(
    async (newPicks: string[], newTags: Record<string, CoverageTag[]> = {}) => {
      // (a) unlocked existing + locked positions.
      const lockedMap: Record<string, string> = {};
      const unlockedExisting: string[] = [];
      for (const [q, ids] of Object.entries(plannedCourses))
        for (const id of ids) {
          if (lockedCourses.has(id)) lockedMap[id] = q;
          else unlockedExisting.push(id);
        }

      // (b) merge with new picks (dedupe by normalized id).
      const seenNorm = new Set<string>();
      const pool: string[] = [];
      for (const id of [...unlockedExisting, ...newPicks]) {
        const nn = normId(id);
        if (seenNorm.has(nn)) continue;
        seenNorm.add(nn);
        pool.push(id);
      }

      // (c) inject missing prereqs (transitive). have = pool ∪ locked ∪ AP.
      const apNorm = new Set([...apCreditedSet].map(normId));
      const have = new Set<string>([...pool.map(normId), ...Object.keys(lockedMap).map(normId), ...apNorm]);
      let trees = await fetchPrereqTrees(pool); // ← prereq fetch #1
      let frontier = [...pool];
      let guard = 0;
      while (frontier.length && guard++ < 20) {
        const missing: string[] = [];
        for (const cid of frontier) {
          const tree = trees[cid] ?? trees[normId(cid)];
          if (!tree) continue;
          for (const m of requiredMissingCourses(tree, have)) {
            const nn = normId(m);
            if (!have.has(nn)) { have.add(nn); missing.push(m); pool.push(m); }
          }
        }
        if (missing.length === 0) break;
        trees = { ...trees, ...(await fetchPrereqTrees(missing)) }; // ← prereq fetch #2 (closure loop)
        frontier = missing;
      }

      if (pool.length === 0) {
        setToast("Nothing to schedule — everything is locked or already placed.");
        return;
      }

      // (d) payload plan: ALL grid quarters as keys (whatif only redistributes
      // among quarters present in the plan). Locked courses stay put; unlocked
      // pool courses are distributed round-robin under the unit cap with a
      // topological pass (a course never seeds before an in-pool prereq) — this
      // gives the optimizer a feasible starting state instead of one stuffed
      // quarter.
      const quarters: string[] = [];
      for (let y = 1; y <= numYears; y++) {
        for (const s of ["fall", "winter", "spring"]) quarters.push(qkey(y, s));
        if (summerYears.has(y)) quarters.push(qkey(y, "summer"));
      }
      const planned_courses: Record<string, string[]> = {};
      for (const q of quarters) planned_courses[q] = [];
      const quarterUnits = new Array(quarters.length).fill(0);
      const qIndex = new Map(quarters.map((q, i) => [q, i] as const));
      const unitsOf = (id: string) => courseInfoMapRef.current[id]?.min_units ?? 4;

      // Locked courses stay in their quarters; count their units against them.
      for (const [cid, q] of Object.entries(lockedMap)) {
        (planned_courses[q] ??= []).push(cid);
        const i = qIndex.get(q);
        if (i != null) quarterUnits[i] += unitsOf(cid);
      }

      // In-pool prereqs of a course (course-leaves of its tree that are in the pool).
      const poolNorm = new Set(pool.map(normId));
      const prereqsInPool = (id: string): string[] =>
        collectPrereqCourseLeaves(trees[id] ?? trees[normId(id)]).filter(
          (p) => poolNorm.has(normId(p)) && normId(p) !== normId(id),
        );

      // Topological order: prereqs before dependents.
      const ordered: string[] = [];
      const seenTopo = new Set<string>();
      const visit = (id: string, stack: Set<string>) => {
        const nn = normId(id);
        if (seenTopo.has(nn) || stack.has(nn)) return;
        stack.add(nn);
        for (const p of prereqsInPool(id)) {
          const dep = pool.find((x) => normId(x) === normId(p));
          if (dep) visit(dep, stack);
        }
        stack.delete(nn);
        if (!seenTopo.has(nn)) { seenTopo.add(nn); ordered.push(id); }
      };
      for (const c of pool) visit(c, new Set());

      // Prereq floor: 1 + the latest quarter any in-pool prereq is assigned to.
      const assignedIdx = new Map<string, number>();
      const floorOf = (c: string): number => {
        let floor = 0;
        for (const p of prereqsInPool(c)) {
          const pi = assignedIdx.get(normId(p));
          if (pi != null) floor = Math.max(floor, pi + 1);
        }
        return floor;
      };
      // Seed in topological order at the EARLIEST slot >= floor (prereqs land as
      // early as possible, so dependents always have a later slot). Prefer
      // under-cap quarters; overload only as a last resort (optimizer fixes units).
      for (const c of ordered) {
        const u = unitsOf(c);
        const floor = Math.min(floorOf(c), quarters.length - 1);
        let chosen = -1;
        for (let i = floor; i < quarters.length; i++) {
          if (quarterUnits[i] + u <= maxUnits) { chosen = i; break; }
        }
        if (chosen === -1) chosen = floor; // no capped room at/after floor → overload at floor
        planned_courses[quarters[chosen]].push(c);
        quarterUnits[chosen] += u;
        assignedIdx.set(normId(c), chosen);
      }

      // Enforce "course strictly after every in-pool prereq", iterating until
      // stable so a move cascades to that course's own dependents. (Whenever the
      // grid has room this guarantees prereqQuarter + 1; a course can only stay
      // put if its prereq is already in the last quarter — genuine window-too-
      // short, surfaced later by validation.)
      let changed = true;
      let safety = 0;
      const maxIter = (ordered.length + 1) * (quarters.length + 1);
      while (changed && safety++ < maxIter) {
        changed = false;
        for (const c of ordered) {
          const floor = floorOf(c);
          if (floor > quarters.length - 1) continue; // can't fit after prereq in this grid
          const cur = assignedIdx.get(normId(c));
          if (cur == null || cur >= floor) continue;
          const u = unitsOf(c);
          let target = -1;
          for (let i = floor; i < quarters.length; i++) {
            if (quarterUnits[i] + u <= maxUnits) { target = i; break; }
          }
          if (target === -1) target = floor;
          if (target === cur) continue;
          planned_courses[quarters[cur]] = planned_courses[quarters[cur]].filter((x) => x !== c);
          quarterUnits[cur] -= u;
          planned_courses[quarters[target]].push(c);
          quarterUnits[target] += u;
          assignedIdx.set(normId(c), target);
          changed = true;
        }
      }

      // (e) send to /api/whatif — same shape as the Optimize Schedule button.
      const res = await fetch("/api/whatif", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: {
            major_id: selectedMajorId,
            completed_courses: [],
            planned_courses,
            graduation_year: parseInt(gradQuarter.split("_")[0]),
            units_per_quarter: maxUnits,
          },
          locked_courses: lockedMap,
          major_id: selectedMajorId,
          graduation_quarter: gradQuarter,
          units_per_quarter: maxUnits,
          waived_ges: [],
          ap_scores: apScores,
        }),
      });
      const data = await res.json();

      // (g) error / infeasible — surface UI, never wipe the schedule.
      if (!res.ok) {
        if (
          data?.detail?.error === "lock_conflict" &&
          Array.isArray(data.detail.conflicts) &&
          data.detail.conflicts.length > 0
        ) {
          setLockConflictErrors(data.detail.conflicts as string[]);
        } else {
          const msg =
            typeof data?.detail === "object"
              ? (data.detail.message ?? JSON.stringify(data.detail))
              : (data?.detail ?? data?.error ?? "Optimizer error");
          setToast(String(msg));
        }
        return;
      }
      setLockConflictErrors(null);
      if (data?.status === "infeasible") {
        const reasons: string[] = Array.isArray(data.conflicts)
          ? data.conflicts.map((c: { reason?: string }) => c?.reason ?? String(c))
          : [];
        setToast(
          reasons.length
            ? `Can't optimize with these locks — ${reasons.join(" · ")}`
            : "Can't optimize around these locked courses.",
        );
        return;
      }

      const planResult = data?.plans?.[0]?.planned_courses as PlannedCourses | undefined;
      if (!planResult) {
        setToast("No plan returned from optimizer");
        return;
      }

      // (f) apply; defensively re-pin locked courses to their locked quarters.
      const finalPlan: PlannedCourses = {};
      for (const [q, ids] of Object.entries(planResult)) finalPlan[q] = [...ids];
      for (const [cid, q] of Object.entries(lockedMap)) {
        for (const qq of Object.keys(finalPlan)) finalPlan[qq] = finalPlan[qq].filter((x) => x !== cid);
        (finalPlan[q] ??= []).push(cid);
      }
      setPlannedCourses(finalPlan);
      validatePlan(finalPlan);

      const inPlan = new Set(Object.values(finalPlan).flat());
      setCoverageTags((prev) => {
        const next: Record<string, CoverageTag[]> = { ...prev, ...newTags };
        for (const k of Object.keys(next)) if (!inPlan.has(k)) delete next[k];
        return next;
      });
    },
    [plannedCourses, lockedCourses, apCreditedSet, numYears, summerYears, selectedMajorId, gradQuarter, maxUnits, apScores, validatePlan],
  );

  // ── GE autofill (no optimizer) ─────────────────────────────────────────────
  // Coverage is authoritative (ge_list via getCourseGECategories) — NEVER array
  // membership. Array membership only supplies the candidate POOL per category.
  const handleGEAutofill = useCallback(async () => {
    const info = courseInfoMapRef.current;
    const allGroups = geRequirements.filter((r) => !apSatisfiedGEs.has(r.requirement_group ?? ""));
    const keyOf = (r: ReqGroup) => r.requirement_group ?? r.group_name;
    const groupByKey = new Map(allGroups.map((g) => [keyOf(g), g] as const));

    // need = remaining slots per group, decremented by what's authoritatively placed.
    const need = new Map<string, number>();
    for (const g of allGroups) need.set(keyOf(g), g.courses_needed ?? 1);
    for (const ids of Object.values(plannedCourses))
      for (const id of ids)
        for (const cat of placedGECats(id, allGroups)) {
          const n = need.get(cat);
          if (n != null) need.set(cat, n - 1);
        }
    for (const [k, v] of [...need]) if (v <= 0) need.delete(k); // keep only unfilled

    const unmetMajor = new Set<string>();
    for (const r of requirements) if (getCoverage(r).remaining > 0) r.courses.forEach((c) => unmetMajor.add(normId(c)));

    const placedIds = new Set(Object.values(plannedCourses).flat().map(normId));
    const picks: string[] = [];
    const tags: Record<string, CoverageTag[]> = {};
    const chosen = new Set<string>();
    const available = (cid: string) => !!info[cid] && !placedIds.has(normId(cid)) && !chosen.has(normId(cid));

    let guard = 0;
    while (need.size > 0 && guard++ < 300) {
      const unfilled = allGroups.filter((g) => (need.get(keyOf(g)) ?? 0) > 0);
      const key = [...need.keys()].sort((a, b) => (GE_ORDER[a] ?? 99) - (GE_ORDER[b] ?? 99))[0];
      const g = groupByKey.get(key);
      if (!g) { need.delete(key); continue; }

      // Pool from array membership; coverage filter is authoritative.
      const candidates = g.courses.filter((c) => available(c) && getCourseGECategories(info[c], unfilled).includes(key));
      if (candidates.length === 0) { need.delete(key); continue; }

      // Scarcity: how many other available courses authoritatively cover each unfilled cat.
      const universe = [...new Set(unfilled.flatMap((x) => x.courses))].filter(available);
      const catsOf = new Map<string, string[]>();
      for (const c of universe) catsOf.set(c, getCourseGECategories(info[c], unfilled));
      const eligibleCount = new Map<string, number>();
      for (const c of universe) for (const cat of catsOf.get(c)!) eligibleCount.set(cat, (eligibleCount.get(cat) ?? 0) + 1);
      const scarcity = (cat: string) => {
        const others = (eligibleCount.get(cat) ?? 0) - 1; // exclude the course itself
        return others <= 0 ? Infinity : 1 / others;
      };
      // Cap a course's covered-unfilled categories to 2 by scarcity (tie: order).
      const capTo2 = (cats: string[]) =>
        cats.length <= 2
          ? cats
          : [...cats].sort((a, b) => scarcity(b) - scarcity(a) || (GE_ORDER[a] ?? 99) - (GE_ORDER[b] ?? 99)).slice(0, 2);

      let bestScore = -1;
      let best: { c: string; capped: string[] }[] = [];
      let fallback: string | null = null;
      for (const c of candidates) {
        if (!fallback) fallback = c;
        const capped = capTo2(catsOf.get(c) ?? getCourseGECategories(info[c], unfilled));
        if (!capped.includes(key)) continue; // this course is better spent on scarcer cats
        let score = capped.length >= 2 ? 3 : 2;
        if (unmetMajor.has(normId(c))) score += 1;
        if (score > bestScore) { bestScore = score; best = [{ c, capped }]; }
        else if (score === bestScore) best.push({ c, capped });
      }

      let pick: string;
      let capped: string[];
      if (best.length) {
        const ch = best[Math.floor(Math.random() * best.length)];
        pick = ch.c;
        capped = ch.capped;
      } else {
        pick = fallback!; // none kept `key` in its cap → credit this one to `key` only
        capped = [key];
      }

      const t: CoverageTag[] = capped.map((cat) => ({ kind: "ge", code: GE_CODE_LABELS[cat] ?? cat }));
      if (unmetMajor.has(normId(pick))) t.push({ kind: "major" });
      picks.push(pick);
      tags[pick] = t;
      chosen.add(normId(pick));
      for (const cat of capped) {
        const n = need.get(cat);
        if (n != null) {
          if (n - 1 <= 0) need.delete(cat);
          else need.set(cat, n - 1);
        }
      }
    }
    await buildAndOptimizePool(picks, tags);
  }, [plannedCourses, geRequirements, apSatisfiedGEs, requirements, getCoverage, placedGECats, buildAndOptimizePool]);

  // ── Minor autofill (no optimizer) ──────────────────────────────────────────
  const handleMinorAutofill = useCallback(async () => {
    const info = courseInfoMapRef.current;
    const placedIds = new Set(Object.values(plannedCourses).flat().map(normId));
    const geGroups = geRequirements.filter((r) => !apSatisfiedGEs.has(r.requirement_group ?? ""));
    const keyOf = (r: ReqGroup) => r.requirement_group ?? r.group_name;

    // Unfilled GE groups, authoritative (ge_list) — never array membership.
    const geNeed = new Map<string, number>();
    for (const g of geGroups) geNeed.set(keyOf(g), g.courses_needed ?? 1);
    for (const ids of Object.values(plannedCourses))
      for (const id of ids)
        for (const cat of placedGECats(id, geGroups)) {
          const n = geNeed.get(cat);
          if (n != null) geNeed.set(cat, n - 1);
        }
    const geUnfilled = geGroups.filter((g) => (geNeed.get(keyOf(g)) ?? 0) > 0);

    const unmetMajor = new Set<string>();
    for (const r of requirements) if (getCoverage(r).remaining > 0) r.courses.forEach((c) => unmetMajor.add(normId(c)));

    // GE categories this course authoritatively covers among still-unfilled
    // groups, capped at 2 (canonical order) to match the GE-autofill cap.
    const geCoveredUnfilled = (cid: string) => getCourseGECategories(info[cid], geUnfilled).slice(0, 2);

    const picks: string[] = [];
    const tags: Record<string, CoverageTag[]> = {};
    const chosen = new Set<string>();

    const avail = (cid: string) => !!info[cid] && !placedIds.has(normId(cid)) && !chosen.has(normId(cid));
    const tagFor = (cid: string): CoverageTag[] => {
      const t: CoverageTag[] = [{ kind: "minor" }];
      for (const cat of geCoveredUnfilled(cid)) t.push({ kind: "ge", code: GE_CODE_LABELS[cat] ?? cat });
      if (unmetMajor.has(normId(cid))) t.push({ kind: "major" });
      return t;
    };

    for (const req of minorRequirements) {
      const isPickN = req.courses_needed < req.courses.length;
      if (!isPickN) {
        // Required course(s): always place if not already on the schedule.
        for (const c of req.courses) {
          if (!avail(c)) continue;
          picks.push(c);
          tags[c] = tagFor(c);
          chosen.add(normId(c));
        }
      } else {
        // Pick-N: only place a candidate that cross-covers an unfilled GE / unmet major.
        let bestScore = 0;
        let best: string[] = [];
        for (const c of req.courses.filter(avail)) {
          let score = 0;
          if (geCoveredUnfilled(c).length > 0) score += 2;
          if (unmetMajor.has(normId(c))) score += 1;
          if (score > bestScore) {
            bestScore = score;
            best = [c];
          } else if (score === bestScore && score > 0) best.push(c);
        }
        if (bestScore > 0 && best.length) {
          const pick = best[Math.floor(Math.random() * best.length)];
          picks.push(pick);
          tags[pick] = tagFor(pick);
          chosen.add(normId(pick));
        }
        // else: no cross-cover → skip this group entirely.
      }
    }
    await buildAndOptimizePool(picks, tags);
  }, [plannedCourses, geRequirements, apSatisfiedGEs, minorRequirements, requirements, getCoverage, placedGECats, buildAndOptimizePool]);

  const toggleSummer = useCallback((year: number) => {
    setSummerYears((prev) => {
      const next = new Set(prev);
      if (next.has(year)) {
        next.delete(year);
        setPlannedCourses((p) => ({ ...p, [qkey(year, "summer")]: [] }));
      } else {
        next.add(year);
      }
      return next;
    });
  }, []);

  const addYear = useCallback(() => setNumYears((n) => n + 1), []);

  // Remove a year: drop its quarters (those courses return to the unplaced pool)
  // and shift every later year down one slot.  Year 1 can never be removed.
  const removeYear = useCallback((target: number) => {
    if (target < 2 || numYears <= 1) return;
    const seasons = ["fall", "winter", "spring", "summer"] as const;
    setPlannedCourses((prev) => {
      const next: PlannedCourses = {};
      for (let ny = 1; ny < numYears; ny++) {
        const oldYear = ny < target ? ny : ny + 1;
        for (const s of seasons) {
          const oldKey = qkey(oldYear, s);
          if (prev[oldKey]?.length) next[qkey(ny, s)] = prev[oldKey];
        }
      }
      validatePlan(next);
      return next;
    });
    setSummerYears((prev) => {
      const next = new Set<number>();
      prev.forEach((sy) => {
        if (sy === target) return;            // removed year's summer is dropped
        next.add(sy < target ? sy : sy - 1);  // shift later summers down
      });
      return next;
    });
    setNumYears((n) => n - 1);
  }, [numYears, validatePlan]);

  // Classify requirements into logical display buckets
  const bucketed = useMemo(() => {
    const lower: ReqGroup[] = [], upper: ReqGroup[] = [], choice: ReqGroup[] = [], elective: ReqGroup[] = [];
    for (const req of requirements) {
      switch (classifyGroup(req)) {
        case "lower":    lower.push(req);    break;
        case "upper":    upper.push(req);    break;
        case "choice":   choice.push(req);   break;
        case "elective": elective.push(req); break;
      }
    }
    return { lower, upper, choice, elective };
  }, [requirements]);

  const firstIncompleteBucket = useMemo(() => {
    const order: BucketKey[] = ["lower", "upper", "choice", "elective"];
    return order.find((k) => bucketed[k].some((r) => !getCoverage(r).done)) ?? null;
  }, [bucketed, getCoverage]);

  // Minor requirements pooled into the same display buckets as the major
  const minorBucketed = useMemo(() => {
    const lower: ReqGroup[] = [], upper: ReqGroup[] = [], choice: ReqGroup[] = [], elective: ReqGroup[] = [];
    for (const req of minorRequirements) {
      switch (classifyGroup(req)) {
        case "lower":    lower.push(req);    break;
        case "upper":    upper.push(req);    break;
        case "choice":   choice.push(req);   break;
        case "elective": elective.push(req); break;
      }
    }
    return { lower, upper, choice, elective };
  }, [minorRequirements]);

  const firstIncompleteMinorBucket = useMemo(() => {
    const order: BucketKey[] = ["lower", "upper", "choice", "elective"];
    return order.find((k) => minorBucketed[k].some((r) => !clientCoverage(r).done)) ?? null;
  }, [minorBucketed, clientCoverage]);

  const firstIncompleteGE = geRequirements.findIndex((r) => !getCoverage(r).done);

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <DndContext sensors={sensors} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
      <div className="flex overflow-hidden" style={{ height: "calc(100vh - 56px)" }}>

        {/* ── Sidebar ──────────────────────────────────────────────────────── */}
        <aside className="w-[260px] shrink-0 flex flex-col bg-[#181818] border-r border-[#2a2a2a]">

          {/* Tabs */}
          <div className="flex border-b border-[#2a2a2a] shrink-0">
            {(["major", "ge", "minor"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setSidebarTab(tab)}
                className={`flex-1 py-2 text-[10px] font-bold tracking-widest uppercase transition-colors
                  ${sidebarTab === tab
                    ? "text-[#f0f0f0] border-b-2 border-[#3b82f6]"
                    : "text-[#444] hover:text-[#666]"}`}
              >
                {tab === "major" ? "Major" : tab === "ge" ? "GE" : "Minor"}
              </button>
            ))}
          </div>

          {/* Fixed top */}
          <div className="px-2 pt-2 flex flex-col gap-1.5 shrink-0">
            {sidebarTab !== "minor" && (
              majorListError ? (
                <ErrorBanner
                  message="Failed to load majors"
                  onRetry={() => setMajorListRetry((n) => n + 1)}
                />
              ) : (
                <MajorCombobox
                  options={parentMajors}
                  selectedDisplayName={selectedDisplayName}
                  onSelect={handleMajorNameChange}
                  loading={majorList.length === 0 && !majorListError}
                />
              )
            )}

            {sidebarTab !== "minor" && specializations.length > 0 && (
              <SpecializationCombobox
                options={specializations}
                selectedMajorId={selectedMajorId}
                onSelect={setSelectedMajorId}
                loading={majorList.length === 0 && !majorListError}
              />
            )}

            {sidebarTab === "minor" && (
              minorListError ? (
                <ErrorBanner
                  message="Failed to load minors"
                  onRetry={() => setMinorListRetry((n) => n + 1)}
                />
              ) : (
                <MinorCombobox
                  options={minorList}
                  selectedMinorId={selectedMinorId}
                  onSelect={setSelectedMinorId}
                  loading={minorList.length === 0 && !minorListError}
                />
              )
            )}

            <div className="relative">
              <svg
                className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-[#333] pointer-events-none"
                viewBox="0 0 12 12" fill="none"
              >
                <circle cx="5" cy="5" r="3.5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M8 8l2.5 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
              <input
                type="text"
                placeholder="Search courses..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-7 pr-2 py-1.5 bg-[#111] border border-[#2a2a2a] rounded text-[11px] text-[#ccc] placeholder-[#333] focus:outline-none focus:border-[#3b82f6]/60"
              />
            </div>

            {sidebarTab !== "minor" && selectedMajorId && !loadingReqs && !reqError && (
              <p className="text-[9px] text-[#444] pb-0.5">
                <span className="text-[#666] font-semibold">{placedRequired}</span>
                {" "}of{" "}
                <span className="text-[#666] font-semibold">{totalRequired}</span>
                {" "}courses placed
                {requirementsState?.all_satisfied && (
                  <span className="text-emerald-500 font-semibold"> · All requirements met</span>
                )}
              </p>
            )}

            {sidebarTab === "minor" && selectedMinorId && !loadingMinorReqs && !minorReqError && (
              <p className="text-[9px] text-[#444] pb-0.5">
                <span className="text-[#666] font-semibold">{minorPlacedRequired}</span>
                {" "}of{" "}
                <span className="text-[#666] font-semibold">{minorTotalRequired}</span>
                {" "}courses placed
              </p>
            )}
          </div>

          <div className="border-t border-[#2a2a2a] mt-1 shrink-0" />

          {/* Scrollable list */}
          <div className="flex-1 overflow-y-auto py-1 min-h-0">
            {sidebarTab === "major" && (
              <>
                {loadingReqs && (
                  <p className="text-[10px] text-[#444] text-center py-8">Loading requirements…</p>
                )}
                {reqError && (
                  <ErrorBanner
                    message="Failed to load courses"
                    onRetry={() => setReqRetry((n) => n + 1)}
                  />
                )}
                {!loadingReqs && !reqError && (
                  <>
                    {(["lower", "upper", "choice", "elective"] as const).map((key) => (
                      <BucketSection
                        key={`${key}-${selectedMajorId}`}
                        bucketKey={key}
                        groups={bucketed[key]}
                        placedSet={placedSet}
                        apCreditedSet={apCreditedSet}
                        courseInfoMap={courseInfoMap}
                        difficultyMap={difficultyMap}
                        searchQuery={searchQuery}
                        defaultOpen={firstIncompleteBucket === key}
                        getCoverage={getCoverage}
                      />
                    ))}
                    {!selectedMajorId && (
                      <p className="text-[10px] text-[#333] text-center py-12">
                        Search for your major above
                      </p>
                    )}

                    {/* ── Global search results ── */}
                    {searchQuery.trim().length >= 2 && globalResults.length > 0 && (
                      <div className="mx-1 mt-2 mb-1">
                        <p className="text-[8px] font-bold uppercase tracking-[0.15em] text-[#444] px-1 pb-1">
                          All Courses
                        </p>
                        <div className="grid grid-cols-3 gap-1 p-1.5 bg-[#0f0f0f] rounded">
                          {globalResults.map((c) => (
                            <CoursePill
                              key={c.id}
                              courseId={c.id}
                              title={c.title}
                              units={c.min_units}
                              isPlaced={placedSet.has(normId(c.id))}
                              isApCredit={apCreditedSet.has(c.id)}
                              unavailable={false}
                              diffScore={difficultyMap[c.id] ?? null}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </>
            )}

            {sidebarTab === "ge" && (
              <>
                {geError ? (
                  <ErrorBanner
                    message="Failed to load GE requirements"
                    onRetry={() => setGeRetry((n) => n + 1)}
                  />
                ) : (
                  <>
                    <div className="px-2 pt-2 pb-1">
                      <span className="text-[8px] font-bold uppercase tracking-[0.15em] text-[#333]">
                        General Education
                      </span>
                    </div>
                    {geRequirements.map((r, i) => (
                      <GESection
                        key={r.id} req={r} placedSet={placedSet}
                        apCreditedSet={apCreditedSet}
                        apSatisfiedGEs={apSatisfiedGEs}
                        courseInfoMap={courseInfoMap} difficultyMap={difficultyMap}
                        searchQuery={searchQuery}
                        initialOpen={i === firstIncompleteGE}
                        getCoverage={getCoverage}
                      />
                    ))}
                    {geRequirements.length === 0 && (
                      <p className="text-[10px] text-[#333] text-center py-12">Loading…</p>
                    )}
                  </>
                )}
              </>
            )}

            {sidebarTab === "minor" && (
              <>
                {loadingMinorReqs && (
                  <p className="text-[10px] text-[#444] text-center py-8">Loading requirements…</p>
                )}
                {minorReqError && (
                  <ErrorBanner
                    message="Failed to load courses"
                    onRetry={() => setMinorReqRetry((n) => n + 1)}
                  />
                )}
                {!loadingMinorReqs && !minorReqError && (
                  <>
                    {(["lower", "upper", "choice", "elective"] as const).map((key) => (
                      <BucketSection
                        key={`minor-${key}-${selectedMinorId}`}
                        bucketKey={key}
                        groups={minorBucketed[key]}
                        placedSet={placedSet}
                        apCreditedSet={apCreditedSet}
                        courseInfoMap={courseInfoMap}
                        difficultyMap={difficultyMap}
                        searchQuery={searchQuery}
                        defaultOpen={firstIncompleteMinorBucket === key}
                        getCoverage={clientCoverage}
                      />
                    ))}
                    {!selectedMinorId && (
                      <p className="text-[10px] text-[#333] text-center py-12">
                        Search for a minor above
                      </p>
                    )}
                  </>
                )}
              </>
            )}
          </div>

          {/* Auto-fill */}
          <div className="px-2.5 py-2.5 border-t border-[#2a2a2a] shrink-0">

            {/* Unit load selector */}
            <div className="mb-2.5">
              <span className="text-[8px] font-bold uppercase tracking-[0.12em] text-[#333] block mb-1.5">
                Units / Quarter
              </span>
              <div className="flex gap-1">
                {UNIT_PRESETS.map((p) => (
                  <button
                    key={p.value}
                    onClick={() => setMaxUnits(p.value)}
                    className={`flex-1 rounded py-1 text-center transition-all
                      ${maxUnits === p.value
                        ? "bg-[#3b82f6]/15 border border-[#3b82f6]/50 text-[#3b82f6]"
                        : "bg-[#1a1a1a] border border-[#2a2a2a] text-[#444] hover:text-[#666]"}`}
                  >
                    <span className="block text-[9px] font-bold">{p.label}</span>
                  </button>
                ))}
              </div>
              {UNIT_PRESETS.find((p) => p.value === maxUnits)?.warning && (
                <p className="mt-1.5 text-[8.5px] text-amber-500/80 leading-snug">
                  ⚠ Heavy course loads significantly increase difficulty and dropout risk — only recommended if required for your graduation timeline.
                </p>
              )}
            </div>

            {toast && (
              <div className="flex items-start gap-1.5 mb-2 px-2 py-1.5 rounded bg-red-950/30 border border-red-900/40">
                <span className="text-[9px] text-red-400 flex-1 leading-snug">{toast}</span>
                <button
                  onClick={() => setToast(null)}
                  className="text-red-500 hover:text-red-300 text-[11px] leading-none shrink-0 mt-px"
                >
                  ×
                </button>
              </div>
            )}

            {lockConflictErrors && (
              <div className="mb-2 px-2 py-2 rounded border border-amber-700/40 bg-amber-950/20">
                <div className="flex items-start justify-between mb-1.5">
                  <span className="text-[8.5px] font-semibold text-amber-400 leading-snug">
                    Locked courses have missing prerequisites:
                  </span>
                  <button
                    onClick={() => setLockConflictErrors(null)}
                    className="text-amber-600 hover:text-amber-400 text-[11px] leading-none shrink-0 ml-1"
                  >
                    ×
                  </button>
                </div>
                <ul className="space-y-1 mb-1.5">
                  {lockConflictErrors.map((c, i) => (
                    <li key={i} className="text-[8px] text-amber-300/80 leading-snug">
                      · {parseLockConflict(c)}
                    </li>
                  ))}
                </ul>
                <p className="text-[7.5px] text-amber-500/70 leading-snug">
                  Unlock these courses or add their prerequisites to continue.
                </p>
              </div>
            )}
            <button
              disabled={!selectedMajorId || autoFillLoading || (sidebarTab === "minor" && !selectedMinorId)}
              onClick={async () => {
                if (!selectedMajorId || autoFillLoading) return;
                if (sidebarTab === "minor" && !selectedMinorId) return;
                setAutoFillLoading(true);
                setToast(null);
                try {
                  if (sidebarTab === "ge") {
                    await handleGEAutofill();   // local placement — never the optimizer
                  } else if (sidebarTab === "minor") {
                    await handleMinorAutofill(); // local placement — never the optimizer
                  } else {
                    // MAJOR: pick the remaining take-all required major courses
                    // (choice/elective pools stay user-driven), then optimize the
                    // unified pool via /api/whatif. whatif doesn't collect major
                    // requirements, so the picks are computed here on the frontend.
                    const exclude = new Set<string>([
                      ...Object.values(plannedCourses).flat().map(normId),
                      ...[...apCreditedSet].map(normId),
                    ]);
                    const seedExtra: string[] = [];
                    const taken = new Set<string>();
                    for (const r of requirements) {
                      const isChoice = r.courses_needed < r.courses.length || r.requirement_type === "elective";
                      if (isChoice) continue; // take-all required groups only
                      for (const c of r.courses) {
                        const nn = normId(c);
                        if (exclude.has(nn) || taken.has(nn)) continue;
                        taken.add(nn);
                        seedExtra.push(c);
                      }
                    }
                    await buildAndOptimizePool(seedExtra, {});
                  }
                } catch (err) {
                  setToast(err instanceof Error ? err.message : "Optimizer unavailable");
                } finally {
                  setAutoFillLoading(false);
                }
              }}
              className={`w-full flex items-center justify-center gap-2 rounded py-2.5 text-[11px] font-bold tracking-wide transition-all
                ${selectedMajorId && !autoFillLoading && !(sidebarTab === "minor" && !selectedMinorId)
                  ? "bg-[#3b82f6] hover:bg-[#2563eb] text-white"
                  : "bg-[#1a1a1a] text-[#3a3a3a] cursor-not-allowed"}`}
            >
              {autoFillLoading ? (
                <>
                  <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 12 12" fill="none">
                    <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="20 8"/>
                  </svg>
                  Generating…
                </>
              ) : (
                <>
                  <span>✦</span>{" "}
                  {sidebarTab === "ge"
                    ? "Auto-fill GE Requirements"
                    : sidebarTab === "minor"
                    ? "Auto-fill Minor Requirements"
                    : "Auto-fill Major Requirements"}
                </>
              )}
            </button>

            <button
              disabled={!selectedMajorId || !hasPlacedCourses || optimizeLoading || autoFillLoading}
              onClick={async () => {
                if (!selectedMajorId || !hasPlacedCourses || optimizeLoading || autoFillLoading) return;
                setOptimizeLoading(true);
                setToast(null);
                try {
                  // Pin only manually-locked courses to their quarter; every other
                  // placed course is the pool optimize_around_locks repositions.
                  const lockedMap: Record<string, string> = {};
                  for (const [quarter, courses] of Object.entries(plannedCourses)) {
                    for (const cid of courses) {
                      if (lockedCourses.has(cid)) lockedMap[cid] = quarter;
                    }
                  }

                  const res = await fetch("/api/whatif", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      plan: {
                        major_id: selectedMajorId,
                        completed_courses: [],
                        planned_courses: plannedCourses,
                        graduation_year: parseInt(gradQuarter.split("_")[0]),
                        units_per_quarter: maxUnits,
                      },
                      locked_courses: lockedMap,
                      major_id: selectedMajorId,
                      graduation_quarter: gradQuarter,
                      units_per_quarter: maxUnits,
                      waived_ges: [],
                      ap_scores: apScores,
                    }),
                  });
                  const data = await res.json();
                  if (!res.ok) {
                    if (
                      data?.detail?.error === "lock_conflict" &&
                      Array.isArray(data.detail.conflicts) &&
                      data.detail.conflicts.length > 0
                    ) {
                      setLockConflictErrors(data.detail.conflicts as string[]);
                    } else {
                      const msg =
                        typeof data?.detail === "object"
                          ? (data.detail.message ?? JSON.stringify(data.detail))
                          : (data?.detail ?? data?.error ?? "Optimizer error");
                      setToast(String(msg));
                    }
                  } else {
                    setLockConflictErrors(null);
                    if (data?.status === "infeasible") {
                      const reasons: string[] = Array.isArray(data.conflicts)
                        ? data.conflicts.map(
                            (c: { reason?: string }) => c?.reason ?? String(c),
                          )
                        : [];
                      setToast(
                        reasons.length
                          ? `Can't optimize with these locks — ${reasons.join(" · ")}`
                          : "Can't optimize around these locked courses.",
                      );
                    } else {
                      const plan = data?.plans?.[0]?.planned_courses as
                        | PlannedCourses
                        | undefined;
                      if (plan) {
                        setPlannedCourses(plan);
                        validatePlan(plan);
                      } else {
                        setToast("No plan returned from optimizer");
                      }
                    }
                  }
                } catch (err) {
                  setToast(err instanceof Error ? err.message : "Optimizer unavailable");
                } finally {
                  setOptimizeLoading(false);
                }
              }}
              className={`w-full mt-1.5 flex items-center justify-center gap-2 rounded py-2.5 text-[11px] font-bold tracking-wide transition-all
                ${selectedMajorId && hasPlacedCourses && !optimizeLoading && !autoFillLoading
                  ? "bg-[#7c3aed] hover:bg-[#6d28d9] text-white"
                  : "bg-[#1a1a1a] text-[#3a3a3a] cursor-not-allowed"}`}
            >
              {optimizeLoading ? (
                <>
                  <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 12 12" fill="none">
                    <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="20 8"/>
                  </svg>
                  Optimizing…
                </>
              ) : (
                <>Optimize Schedule</>
              )}
            </button>

            <button
              onClick={() => setShowClearConfirm(true)}
              className="w-full mt-1.5 rounded py-2 text-[11px] font-medium tracking-wide border border-red-900/50 text-red-500 hover:bg-red-950/30 hover:border-red-700/60 transition-all"
            >
              Clear Schedule
            </button>

            {clearSuccess && (
              <p className="mt-1.5 text-center text-[10px] text-green-500">Schedule cleared</p>
            )}
          </div>
        </aside>

        {/* ── Clear Schedule confirmation dialog ───────────────────────────── */}
        {pendingLock && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
            <div className="w-80 rounded-xl border border-white/[0.1] bg-[#1a1a1a] p-6 shadow-2xl">
              <h2 className="text-sm font-semibold text-white mb-3">Lock this course?</h2>
              <div className="mb-4 space-y-2">
                {pendingLock.warnings.map((c, i) => (
                  <p key={i} className="text-xs text-amber-400/90 leading-relaxed">
                    ⚠ {parseLockConflict(c)}
                  </p>
                ))}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => setPendingLock(null)}
                  className="flex-1 rounded-lg border border-white/[0.1] py-2 text-xs font-medium text-zinc-300 hover:bg-white/[0.06] transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={() => {
                    setLockedCourses((prev) => new Set([...prev, pendingLock.courseId]));
                    setPendingLock(null);
                  }}
                  className="flex-1 rounded-lg bg-amber-600 hover:bg-amber-500 py-2 text-xs font-medium text-white transition-colors"
                >
                  Lock Anyway
                </button>
              </div>
            </div>
          </div>
        )}

        {prereqProposal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
            <div className="w-96 rounded-xl border border-white/[0.1] bg-[#1a1a1a] p-6 shadow-2xl">
              <h2 className="text-sm font-semibold text-white mb-1">
                Add missing prerequisites?
              </h2>
              <p className="text-xs text-zinc-400 leading-relaxed mb-3">
                {fmtCourse(prereqProposal.courseId)} needs{" "}
                {prereqProposal.placements.length} prerequisite
                {prereqProposal.placements.length === 1 ? "" : "s"} that aren&apos;t in
                your plan yet. Add them in valid quarters?
              </p>
              <div className="mb-5 max-h-56 overflow-y-auto rounded-lg border border-white/[0.06] bg-[#141414] divide-y divide-white/[0.05]">
                {prereqProposal.placements.map((p) => (
                  <div key={p.course_id} className="flex items-center justify-between px-3 py-2">
                    <span className="text-xs font-medium text-zinc-200">
                      {fmtCourse(p.course_id)}
                    </span>
                    <span className="text-[11px] text-[#3b82f6] tabular-nums">
                      → {formatQuarterKey(p.quarter)}
                    </span>
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => setPrereqProposal(null)}
                  disabled={prereqApplying}
                  className="flex-1 rounded-lg border border-white/[0.1] py-2 text-xs font-medium text-zinc-300 hover:bg-white/[0.06] transition-colors disabled:opacity-50"
                >
                  Not now
                </button>
                <button
                  onClick={applyProposal}
                  disabled={prereqApplying}
                  className="flex-1 rounded-lg bg-[#3b82f6] hover:bg-[#2563eb] py-2 text-xs font-medium text-white transition-colors disabled:opacity-50"
                >
                  {prereqApplying ? "Adding…" : "Add prerequisites"}
                </button>
              </div>
            </div>
          </div>
        )}

        {showClearConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
            <div className="w-80 rounded-xl border border-white/[0.1] bg-[#1a1a1a] p-6 shadow-2xl">
              <h2 className="text-sm font-semibold text-white mb-2">Clear Schedule?</h2>
              <p className="text-xs text-zinc-400 leading-relaxed mb-5">
                This will clear your entire schedule and AP scores. This cannot be undone.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setShowClearConfirm(false)}
                  className="flex-1 rounded-lg border border-white/[0.1] py-2 text-xs font-medium text-zinc-300 hover:bg-white/[0.06] transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={async () => {
                    setShowClearConfirm(false);
                    setPlannedCourses({});
                    setApScores({});
                    setLockedCourses(new Set());
                    setSummerYears(new Set());
                    setSelectedMinorId("");
                    setNumYears(DEFAULT_YEARS);
                    setMaxUnits(19);
                    await deletePlan().catch(() => {});
                    setClearSuccess(true);
                    setTimeout(() => setClearSuccess(false), 2500);
                  }}
                  className="flex-1 rounded-lg bg-red-600 hover:bg-red-500 py-2 text-xs font-medium text-white transition-colors"
                >
                  Clear Schedule
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ── Main ─────────────────────────────────────────────────────────── */}
        <main className="flex-1 flex flex-col overflow-hidden bg-[#111]">

          {/* Top bar */}
          <div className="h-12 shrink-0 flex items-center px-6 border-b border-[#2a2a2a] bg-[#141414] gap-3">
            <span className="text-[13px] font-medium text-[#bbb] truncate max-w-[240px]">
              {selectedLabel || <span className="text-[#555] font-normal">No major selected</span>}
            </span>
            <div className="flex-1" />

            <APCreditsMenu apScores={apScores} setApScores={setApScores} apExamNames={apExamNames} />

            <PlansMenu
              signedIn={signedIn}
              savedPlans={savedPlans}
              maxPlans={MAX_SAVED_PLANS}
              onSave={handleSaveNamed}
              onLoad={handleLoadNamed}
              onDelete={handleDeleteNamed}
              onSignIn={handleSignIn}
            />

            <button
              onClick={handleDownloadPDF}
              title="Download a PDF of this schedule"
              className="flex items-center gap-1.5 rounded-md border border-[#2a2a2a] bg-[#1a1a1a] px-2.5 h-7 text-[11px] font-medium text-[#999] hover:text-[#e8e8e8] hover:border-[#3a3a3a] transition-colors"
            >
              <svg viewBox="0 0 16 16" className="w-3.5 h-3.5" fill="none">
                <path d="M8 2v8m0 0L5 7m3 3l3-3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
                <path d="M3 12.5h10" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
              </svg>
              PDF
            </button>

            <div className="w-px h-5 bg-[#2a2a2a]" />

            <div className="flex items-baseline gap-1.5">
              <span className="text-[16px] font-bold text-[#e8e8e8] tabular-nums leading-none">{totalUnits}</span>
              <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#555]">Units</span>
            </div>
          </div>

          {/* Optimizer offline banner */}
          {optimizerOnline === false && (
            <div className="px-4 py-1.5 bg-amber-950/30 border-b border-amber-900/40 shrink-0 flex items-center gap-2">
              <span className="text-[10px] text-amber-400">
                ⚠ Optimizer offline — prereq checking disabled
              </span>
            </div>
          )}

          {/* Grid */}
          <div className="flex-1 overflow-auto p-4">
            <div className="border border-[#2a2a2a] rounded-lg overflow-hidden">
              {Array.from({ length: numYears }, (_, i) => i + 1).map((year) => {
                const hasSummer = summerYears.has(year);
                const summerQk = qkey(year, "summer");

                return (
                  <div key={year} className="flex border-b border-[#2a2a2a] last:border-b-0">
                    {/* Year label + remove */}
                    <div className="w-9 shrink-0 flex flex-col items-center justify-center gap-1.5 py-1.5 border-r border-[#2a2a2a] bg-gradient-to-b from-[#141414] to-[#0d0d0d]">
                      <span
                        className="flex-1 flex items-center text-[9px] font-bold uppercase tracking-[0.2em] text-[#6a6a6a] select-none"
                        style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
                      >
                        {`Year ${year}`}
                      </span>
                      {year > 1 && (
                        <button
                          onClick={() => removeYear(year)}
                          title={`Remove Year ${year} — its courses return to the sidebar`}
                          className="shrink-0 flex items-center justify-center w-5 h-5 rounded border border-dashed border-[#2a2a2a] text-[10px] leading-none text-[#5a5a5a] hover:text-red-400 hover:border-red-700/50 hover:bg-red-950/30 transition-colors"
                        >
                          ✕
                        </button>
                      )}
                    </div>

                    {/* Quarter grid */}
                    <div
                      className="flex-1 grid"
                      style={{ gridTemplateColumns: hasSummer ? "1fr 1fr 1fr 0.6fr" : "1fr 1fr 1fr" }}
                    >
                      {BASE_QUARTERS.map((q) => {
                        const qk = qkey(year, q.key);
                        return (
                          <QuarterCell
                            key={qk} qKey={qk} label={q.label} dim={false}
                            courseIds={plannedCourses[qk] ?? []}
                            courseInfoMap={courseInfoMap}
                            difficultyMap={difficultyMap}
                            lockedCourses={lockedCourses}
                            onToggleLock={toggleLock}
                            onRemove={removeCourse}
                            prereqWarnings={prereqWarnings}
                            onDismissWarning={dismissWarning}
                            maxUnitsPerQuarter={maxUnits}
                            coverageTags={coverageTags}
                          />
                        );
                      })}
                      {hasSummer && (
                        <QuarterCell
                          key={summerQk} qKey={summerQk} label="Summer" dim
                          courseIds={plannedCourses[summerQk] ?? []}
                          courseInfoMap={courseInfoMap}
                          difficultyMap={difficultyMap}
                          lockedCourses={lockedCourses}
                          onToggleLock={toggleLock}
                          onRemove={removeCourse}
                          prereqWarnings={prereqWarnings}
                          onDismissWarning={dismissWarning}
                          coverageTags={coverageTags}
                          removable onRemoveQuarter={() => toggleSummer(year)}
                          maxUnitsPerQuarter={maxUnits}
                        />
                      )}
                    </div>

                    {/* + Summer */}
                    <div className="w-6 shrink-0 flex items-center justify-center border-l border-[#2a2a2a] bg-[#0f0f0f]">
                      {!hasSummer && (
                        <button
                          onClick={() => toggleSummer(year)}
                          title="Add Summer"
                          className="flex flex-col items-center gap-1 text-[#5a5a5a] hover:text-[#FFC72C]/70 transition-colors"
                        >
                          <span className="text-[11px] font-bold leading-none">+</span>
                          <span
                            className="text-[8px] font-semibold uppercase tracking-[0.15em] leading-none"
                            style={{ writingMode: "vertical-rl" }}
                          >
                            Summer
                          </span>
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Add Year */}
            <button
              onClick={addYear}
              className="mt-3 w-full flex items-center justify-center gap-1.5 rounded-lg border border-dashed border-[#2a2a2a] py-2.5 text-[10px] font-semibold uppercase tracking-[0.15em] text-[#5a5a5a] hover:text-[#FFC72C]/70 hover:border-[#3a3a3a] transition-colors"
            >
              <span className="text-[13px] leading-none">+</span> Add Year
            </button>
          </div>
        </main>
      </div>

      {/* Feedback link — subtle, fixed bottom-right */}
      <a
        href="mailto:rtmcdani@uci.edu"
        className="fixed bottom-3 right-4 z-40 text-[10px] text-[#444] hover:text-[#888] transition-colors"
      >
        Mail any feedback to rtmcdani@uci.edu
      </a>

      {/* Drag overlay */}
      <DragOverlay dropAnimation={null}>
        {activeData && (
          <div className="flex items-center gap-1.5 rounded border border-[#3b82f6]/40 bg-[#111d2e] px-2.5 py-1 text-[10px] shadow-2xl pointer-events-none">
            <span className="font-bold text-[#f0f0f0]">{activeData.courseId}</span>
          </div>
        )}
      </DragOverlay>
    </DndContext>
  );
}
