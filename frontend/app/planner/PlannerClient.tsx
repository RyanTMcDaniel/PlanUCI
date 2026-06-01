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
  fetchGERequirements,
  fetchApExamNames,
  resolveApCredits,
} from "@/lib/api/courses";
import { createClient } from "@/lib/supabase/client";
import { savePlan, loadPlan, syncUserProfile, deletePlan } from "@/lib/api/plans";
// ── Types ─────────────────────────────────────────────────────────────────────

type PlannedCourses = Record<string, string[]>;

interface DragData {
  type: "sidebar" | "placed";
  courseId: string;
  quarterKey?: string;
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

const _QUARTER_ABBREV: Record<string, string> = {
  Fall: "F", Winter: "W", Spring: "S", Summer: "Su",
};
function abbrevQuarter(q: string): string {
  const [season, year] = q.split(" ");
  return (_QUARTER_ABBREV[season] ?? season[0]) + (year?.slice(2) ?? "");
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
      className={`w-3 h-3 transition-colors ${locked ? "text-amber-400" : "text-[#555] group-hover/card:text-[#e8e8e8]"}`}>
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
          {["F", "W", "S"].map((q) => (
            <span key={q} className="text-[7px] text-[#444] text-center font-mono">{q}</span>
          ))}
          {TIP_YEARS.flatMap((yr) => [
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
                  <span className="text-[#555]">{"  "}Taught: {prof.quarters_taught.map(abbrevQuarter).join(", ")}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Placed card ────────────────────────────────────────────────────────────────

function PlacedCard({
  courseId, quarterKey, title, units, level, diffScore, gpa, isLocked, onToggleLock, onRemove,
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
      className={`group/card flex items-center gap-1.5 rounded-r pl-2 pr-1 py-1 select-none
        bg-[#1e1e1e] border border-l-0 border-[#2a2a2a] transition-colors min-h-[44px]
        ${isDragging ? "opacity-20" : "hover:bg-[#252525] hover:border-[#333]"}`}
    >
      {/* drag handle */}
      <span
        {...listeners}
        {...attributes}
        className="text-[10px] text-[#2a2a2a] group-hover/card:text-[#444] cursor-grab active:cursor-grabbing shrink-0 leading-none self-start mt-1"
      >
        ⠿
      </span>

      {/* course info */}
      <div className="flex-1 min-w-0">
        <p className="text-[13px] font-bold text-[#e8e8e8] leading-tight truncate">{courseId}</p>
        {title && (
          <p className="text-[10px] text-[#999] leading-snug truncate">{title}</p>
        )}
      </div>

      {/* right side */}
      <div className="flex flex-col items-end gap-0.5 shrink-0">
        {gpa != null && isFinite(gpa) && (
          <span className="text-[9px] font-mono" style={{ color: "#888" }}>{gpa.toFixed(2)} GPA</span>
        )}
        <span className="text-[10px] text-[#666]">{units ?? "?"} UNITS</span>
        <div className="flex gap-1 opacity-0 group-hover/card:opacity-100 transition-opacity">
          <button
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => onToggleLock(courseId)}
            title={isLocked ? "Unlock" : "Lock to quarter"}
          >
            <LockIcon locked={isLocked} />
          </button>
          <button
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => onRemove(courseId, quarterKey)}
            title="Remove"
            className="text-[#555] hover:text-[#e8e8e8] text-[12px] leading-none w-3 text-center transition-colors"
          >
            ×
          </button>
        </div>
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
  maxUnitsPerQuarter,
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
      <div className="flex items-center px-2 h-7 border-b border-[#2a2a2a] shrink-0 bg-[#242424]">
        <span className={`text-[10px] font-semibold ${dim ? "text-[#383838]" : "text-[#666]"}`}>
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
              className={`text-[9px] ml-auto mr-1 ${overload ? "text-amber-500 font-semibold" : "text-[#555]"}`}
              title={overload ? `⚠ Exceeds ${maxUnitsPerQuarter ?? 16}-unit cap` : undefined}
            >
              {overload && "⚠ "}{units} UNITS
            </span>
          );
        })()}
        {removable && onRemoveQuarter && (
          <button onClick={onRemoveQuarter}
            className="text-[9px] text-[#383838] hover:text-[#666] transition-colors leading-none">
            ✕
          </button>
        )}
      </div>

      {/* droppable body */}
      <div
        ref={setNodeRef}
        className={`flex-1 flex flex-col gap-[4px] p-1.5 min-h-[160px] transition-colors
          ${isOver ? "bg-[#0d1a2d]" : "bg-[#1e1e1e]"}`}
      >
        {courseIds.length === 0 && !isOver && (
          <div className="m-1 flex-1 border border-dashed border-[#242424] rounded" />
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
          />
        ))}
      </div>
    </div>
  );
}

// ── Course pill (sidebar grid) ─────────────────────────────────────────────────

function CoursePill({
  courseId, title, units, isPlaced, isApCredit, unavailable, diffScore,
}: {
  courseId: string;
  title?: string | null;
  units?: number | null;
  isPlaced: boolean;
  isApCredit?: boolean;
  unavailable?: boolean;
  diffScore?: number | null;
}) {
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
        className="flex items-center gap-1 rounded-full px-2 py-[3px] bg-emerald-950/40 border border-emerald-700/40 min-w-0 overflow-hidden"
      >
        {dot}
        <span className="text-[9px] font-medium leading-none truncate text-emerald-400">{courseId}</span>
        <span className="text-[6.5px] font-bold leading-none text-emerald-500 uppercase tracking-wide shrink-0">AP</span>
      </div>
    );
  }

  if (isPlaced) {
    return (
      <div
        title={tooltip}
        className="flex items-center justify-center rounded-full px-2 py-[3px] bg-[#3b82f6]/20 border border-[#3b82f6]/35 min-w-0 overflow-hidden"
      >
        {dot}
        <span className="text-[9px] font-medium leading-none truncate text-[#93c5fd]">
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
      className={`flex items-center justify-center rounded-full px-2 py-[3px] border cursor-grab active:cursor-grabbing select-none transition-colors min-w-0 overflow-hidden
        ${unavailable
          ? "border-dashed border-[#252525] bg-transparent"
          : isDragging
            ? "border-[#3b82f6]/40 bg-[#3b82f6]/10 opacity-40"
            : "border-[#333] bg-transparent hover:border-[#555] hover:bg-[#1a1a1a]"
        }`}
    >
      {!unavailable && dot}
      <span className={`text-[9px] font-medium leading-none truncate ${unavailable ? "text-[#333]" : "text-[#bbb]"}`}>
        {courseId}
      </span>
    </div>
  );
}

// ── Requirement group ─────────────────────────────────────────────────────────

function RequirementGroup({
  req, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery, initialOpen,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  initialOpen?: boolean;
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

  const placed = req.courses.filter((c) => placedSet.has(c)).length;
  const done = placed >= req.courses_needed;
  const partial = !done && placed > 0;
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
          <span className="text-[10px] font-normal text-[#ccc] block truncate">
            {req.group_name}
          </span>
          {req.courses_needed < req.courses.length && (
            <span className="text-[9px] text-amber-500/70 leading-none font-medium">
              pick {req.courses_needed} of {req.courses.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="text-[9px] px-1.5 py-[2px] rounded bg-[#1e1e1e] font-mono tabular-nums text-[#555]">
            {placed}/{req.courses_needed}
          </span>
          <ChevronIcon open={open} />
        </div>
      </button>
      {open && (
        <div className="grid grid-cols-3 gap-1 p-1.5 bg-[#0f0f0f]">
          {filtered.map((cid) => (
            <CoursePill
              key={cid}
              courseId={cid}
              title={courseInfoMap[cid]?.title}
              units={courseInfoMap[cid]?.min_units}
              isPlaced={placedSet.has(cid)}
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

// ── Flat group (inline pills, no expand) ──────────────────────────────────────

function FlatGroup({
  req, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
}) {
  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter(
      (cid) => cid.toLowerCase().includes(q) || (courseInfoMap[cid]?.title ?? "").toLowerCase().includes(q),
    );
  }, [req.courses, searchQuery, courseInfoMap]);

  if (filtered.length === 0) return null;

  const placed = req.courses.filter((c) => placedSet.has(c)).length;
  const done = placed >= req.courses_needed;
  const isChoice = req.courses_needed < req.courses.length;
  const showLabel = isChoice || req.courses.length > 1;

  return (
    <div className="px-2 pt-1 pb-1.5">
      {showLabel && (
        <div className="flex items-center gap-1.5 mb-1">
          <span className="text-[9px] text-[#4a4a4a] flex-1 truncate">{req.group_name}</span>
          {isChoice && (
            <span className={`text-[8px] font-mono shrink-0 ${done ? "text-[#22c55e]" : "text-[#444]"}`}>
              pick {req.courses_needed}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        {filtered.map((cid) => (
          <CoursePill
            key={cid}
            courseId={cid}
            title={courseInfoMap[cid]?.title}
            units={courseInfoMap[cid]?.min_units}
            isPlaced={placedSet.has(cid)}
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
  bucketKey, groups, placedSet, apCreditedSet, courseInfoMap, difficultyMap, searchQuery, defaultOpen,
}: {
  bucketKey: BucketKey;
  groups: ReqGroup[];
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  if (groups.length === 0) return null;

  const totalNeeded = groups.reduce((s, r) => s + r.courses_needed, 0);
  const totalPlaced = groups.reduce(
    (s, r) => s + Math.min(r.courses.filter((c) => placedSet.has(c)).length, r.courses_needed),
    0,
  );
  const done = totalPlaced >= totalNeeded;
  const partial = !done && totalPlaced > 0;
  const firstIncomplete = groups.findIndex(
    (r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed,
  );

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
  req, placedSet, apCreditedSet, apSatisfiedGEs, courseInfoMap, difficultyMap, searchQuery, initialOpen,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  apCreditedSet: Set<string>;
  apSatisfiedGEs: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  difficultyMap: Record<string, number>;
  searchQuery: string;
  initialOpen?: boolean;
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

  const satisfied = apDirect
    ? req.courses_needed
    : Math.min(req.courses.filter((c) => placedSet.has(c)).length, req.courses_needed);
  const done = satisfied >= req.courses_needed;
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
            {satisfied}/{req.courses_needed}
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
              isPlaced={placedSet.has(cid)}
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
  const [toast, setToast] = useState<string | null>(null);
  const [prereqWarnings, setPrereqWarnings] = useState<Record<string, string>>({});
  const [difficultyMap, setDifficultyMap] = useState<Record<string, number>>({});
  const [optimizerOnline, setOptimizerOnline] = useState<boolean | null>(null);
  const [apScores, setApScores] = useState<Record<string, number>>({});
  const [apExamNames, setApExamNames] = useState<string[]>([]);
  const [apCreditedSet, setApCreditedSet] = useState<Set<string>>(new Set());
  const [apSatisfiedGEs, setApSatisfiedGEs] = useState<Set<string>>(new Set());
  const [apSectionOpen, setApSectionOpen] = useState(false);
  const [apSearch, setApSearch] = useState("");
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [lockConflictErrors, setLockConflictErrors] = useState<string[] | null>(null);
  const [pendingLock, setPendingLock] = useState<{ courseId: string; warnings: string[] } | null>(null);
  const [clearSuccess, setClearSuccess] = useState(false);

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

  const placedSet = useMemo(() => {
    const s = new Set<string>();
    for (const ids of Object.values(plannedCourses)) ids.forEach((id) => s.add(id));
    // AP-credited courses count as "placed" for sidebar coverage checks
    apCreditedSet.forEach((id) => s.add(id));
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

  const totalRequired = useMemo(
    () => requirements.reduce((s, r) => s + r.courses_needed, 0),
    [requirements],
  );

  const placedRequired = useMemo(
    () =>
      [...placedSet].filter((id) => requirements.some((r) => r.courses.includes(id))).length,
    [placedSet, requirements],
  );

  const minorTotalRequired = useMemo(
    () => minorRequirements.reduce((s, r) => s + r.courses_needed, 0),
    [minorRequirements],
  );

  const minorPlacedRequired = useMemo(
    () =>
      [...placedSet].filter((id) => minorRequirements.some((r) => r.courses.includes(id))).length,
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

  // ── Load plan + sync profile on mount ─────────────────────────────────────
  useEffect(() => {
    supabase.auth.getUser().then(async ({ data }) => {
      if (!data.user) { saveEnabledRef.current = true; return; }
      const plan = await loadPlan();
      if (plan) {
        setPlannedCourses(plan.plannedCourses);
        setSelectedMajorId(plan.selectedMajorId);
        setSelectedDisplayName(plan.selectedDisplayName);
        setSelectedMinorId(plan.selectedMinorId ?? "");
        setNumYears(plan.numYears ?? DEFAULT_YEARS);
        setMaxUnits(plan.maxUnits);
        setLockedCourses(new Set(plan.lockedCourses));
        setApScores(plan.apScores);
        setSummerYears(new Set(plan.summerYears));
      }
      saveEnabledRef.current = true;
      syncUserProfile({
        majorCode: plan?.selectedMajorId ?? "",
        gradQuarter: qkey(plan?.numYears ?? DEFAULT_YEARS, "spring"),
        preferredMaxUnits: plan?.maxUnits ?? 16,
      }).catch(() => {});
    }).catch(() => { saveEnabledRef.current = true; });
  }, [supabase]); // eslint-disable-line react-hooks/exhaustive-deps

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

  // ── Global course search (any course, not just requirements) ──────────────
  useEffect(() => {
    const q = searchQuery.trim();
    if (q.length < 2) { setGlobalResults([]); return; }
    const timer = setTimeout(async () => {
      try {
        const qNorm = q.replace(/\s+/g, "").toUpperCase();
        const { data } = await supabase
          .from("courses")
          .select("id, title, min_units, description, course_level, terms, avg_gpa")
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
      setPlannedCourses((prev) => {
        if (prev[tq]?.includes(src.courseId)) return prev;
        const next = { ...prev, [tq]: [...(prev[tq] ?? []), src.courseId] };
        validatePlan(next);
        return next;
      });
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
    return order.find((k) =>
      bucketed[k].some((r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed),
    ) ?? null;
  }, [bucketed, placedSet]);

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
    return order.find((k) =>
      minorBucketed[k].some((r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed),
    ) ?? null;
  }, [minorBucketed, placedSet]);

  const firstIncompleteGE = geRequirements.findIndex(
    (r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed,
  );

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
              <select
                value={selectedMajorId}
                onChange={(e) => setSelectedMajorId(e.target.value)}
                className="w-full bg-[#111] border border-[#2a2a2a] rounded px-2.5 py-1.5 text-[11px] text-[#f0f0f0] focus:outline-none focus:border-[#3b82f6]/60"
              >
                {specializations.map((spec) => (
                  <option key={spec.major_id} value={spec.major_id}>
                    {spec.specialization_name ?? spec.major_id}
                  </option>
                ))}
              </select>
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
                              isPlaced={placedSet.has(c.id)}
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
            {/* AP Credits section */}
            <div className="mb-2.5 border border-[#2a2a2a] rounded">
              <button
                onClick={() => setApSectionOpen((o) => !o)}
                className="w-full flex items-center justify-between px-2 py-1.5 text-left"
              >
                <span className="text-[8px] font-bold uppercase tracking-[0.12em] text-[#333]">
                  AP Credits
                </span>
                <span className="flex items-center gap-1.5">
                  {Object.keys(apScores).length > 0 && (
                    <span className="text-[8px] text-[#3b82f6] font-bold">
                      {Object.keys(apScores).length} exam{Object.keys(apScores).length !== 1 ? "s" : ""}
                    </span>
                  )}
                  <span className="text-[10px] text-[#444]">{apSectionOpen ? "▲" : "▼"}</span>
                </span>
              </button>
              {apSectionOpen && (
                <div className="border-t border-[#2a2a2a] px-2 pt-1.5 pb-2">
                  <input
                    type="text"
                    value={apSearch}
                    onChange={(e) => setApSearch(e.target.value)}
                    placeholder="Search AP exams…"
                    className="w-full rounded border border-[#2a2a2a] bg-[#141414] px-2 py-1 text-[9px] text-[#aaa] placeholder-[#444] focus:outline-none focus:border-[#3b82f6]/40 mb-1.5"
                  />
                  <div className="max-h-40 overflow-y-auto space-y-0.5">
                    {apExamNames
                      .filter((n) => !apSearch || n.toLowerCase().includes(apSearch.toLowerCase()))
                      .map((examName) => {
                        const score = apScores[examName];
                        return (
                          <div key={examName} className="flex items-center justify-between gap-1.5 py-0.5">
                            <span className="text-[8.5px] text-[#666] leading-tight flex-1 min-w-0 truncate">
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
                                  className={`w-5 h-5 rounded text-[8px] font-bold transition-all
                                    ${score === s
                                      ? "bg-[#3b82f6] text-white"
                                      : "bg-[#1a1a1a] border border-[#2a2a2a] text-[#444] hover:text-[#999]"
                                    }`}
                                >
                                  {s}
                                </button>
                              ))}
                            </div>
                          </div>
                        );
                      })}
                    {apExamNames.filter((n) => !apSearch || n.toLowerCase().includes(apSearch.toLowerCase())).length === 0 && (
                      <p className="text-[8px] text-[#444] text-center py-2">No exams match</p>
                    )}
                  </div>
                  {Object.keys(apScores).length > 0 && (
                    <button
                      onClick={() => setApScores({})}
                      className="mt-1.5 w-full text-[8px] text-[#444] hover:text-[#666] text-center"
                    >
                      Clear all
                    </button>
                  )}
                </div>
              )}
            </div>

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
              disabled={!selectedMajorId || autoFillLoading}
              onClick={async () => {
                if (!selectedMajorId || autoFillLoading) return;
                setAutoFillLoading(true);
                setToast(null);
                try {
                  // Build locked course→quarter map from the current plan
                  const lockedMap: Record<string, string> = {};
                  for (const [quarter, courses] of Object.entries(plannedCourses)) {
                    for (const cid of courses) {
                      if (lockedCourses.has(cid)) lockedMap[cid] = quarter;
                    }
                  }
                  const hasLocks = Object.keys(lockedMap).length > 0;

                  const res = await fetch(hasLocks ? "/api/whatif" : "/api/optimizer", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: hasLocks
                      ? JSON.stringify({
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
                        })
                      : JSON.stringify({
                          major_id: selectedMajorId,
                          completed_courses: [],
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
                    const plan = data?.variants?.[0]?.planned_courses as PlannedCourses | undefined;
                    if (plan) {
                      setPlannedCourses(plan);
                      validatePlan(plan);
                    } else {
                      setToast("No plan returned from optimizer");
                    }
                  }
                } catch (err) {
                  setToast(err instanceof Error ? err.message : "Optimizer unavailable");
                } finally {
                  setAutoFillLoading(false);
                }
              }}
              className={`w-full flex items-center justify-center gap-2 rounded py-2.5 text-[11px] font-bold tracking-wide transition-all
                ${selectedMajorId && !autoFillLoading
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
                <><span>✦</span> Auto-fill Plan</>
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
          <div className="h-10 shrink-0 flex items-center px-5 border-b border-[#2a2a2a] bg-[#141414] gap-3">
            <span className="text-[11px] text-[#666] truncate max-w-[260px]">
              {selectedLabel || <span className="text-[#333]">No major selected</span>}
            </span>
            <div className="flex-1" />
            <span className="text-[10px] font-bold text-[#666]">
              {totalUnits} <span className="text-[#444] font-normal">UNITS</span>
            </span>
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
                    {/* Year label */}
                    <div className="w-8 shrink-0 flex items-center justify-center border-r border-[#2a2a2a] bg-[#0f0f0f]">
                      <span
                        className="text-[6px] font-bold uppercase tracking-[0.15em] text-[#2a2a2a] select-none"
                        style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
                      >
                        {`Year ${year}`}
                      </span>
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
                          />
                        );
                      })}
                      {hasSummer && (
                        <QuarterCell
                          key={summerQk} qKey={summerQk} label="Sum" dim
                          courseIds={plannedCourses[summerQk] ?? []}
                          courseInfoMap={courseInfoMap}
                          difficultyMap={difficultyMap}
                          lockedCourses={lockedCourses}
                          onToggleLock={toggleLock}
                          onRemove={removeCourse}
                          prereqWarnings={prereqWarnings}
                          onDismissWarning={dismissWarning}
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
                          className="flex flex-col items-center text-[#252525] hover:text-[#4a4a4a] transition-colors"
                        >
                          <span className="text-[10px] font-bold leading-none">+</span>
                          <span
                            className="text-[6px] font-bold uppercase leading-none"
                            style={{ writingMode: "vertical-rl" }}
                          >
                            Sum
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
              className="mt-3 w-full flex items-center justify-center gap-1.5 rounded-lg border border-dashed border-[#2a2a2a] py-2.5 text-[10px] font-bold uppercase tracking-widest text-[#444] hover:text-[#888] hover:border-[#3a3a3a] transition-colors"
            >
              <span className="text-[13px] leading-none">+</span> Add Year
            </button>
          </div>
        </main>
      </div>

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
