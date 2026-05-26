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
import {
  type ReqGroup,
  type CourseDetail,
  fetchMajorRequirements,
  fetchCourseDetails,
  fetchGERequirements,
} from "@/lib/api/courses";
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

// ── Constants ─────────────────────────────────────────────────────────────────

const START_YEAR = 2026;
const YEARS = [1, 2, 3, 4];
const BASE_QUARTERS = [
  { key: "fall",   label: "Fall"   },
  { key: "winter", label: "Winter" },
  { key: "spring", label: "Spring" },
];
const TIP_YEARS = [2021, 2022, 2023, 2024, 2025];

function qkey(year: number, q: string) {
  return `${START_YEAR + year - 1}_${q}`;
}

function generateGradOptions() {
  const out: { value: string; label: string }[] = [];
  const seq = ["winter", "spring", "fall"];
  let year = 2026;
  let qi = 1;
  while (true) {
    const q = seq[qi];
    out.push({ value: `${year}_${q}`, label: `${q[0].toUpperCase() + q.slice(1)} ${year}` });
    if (year === 2032 && q === "spring") break;
    qi = (qi + 1) % 3;
    if (qi === 0) year++;
  }
  return out;
}

const GRAD_OPTIONS = generateGradOptions();

function diffColor(level: string | null | undefined): string {
  if (!level) return "#3a3a3a";
  if (level.includes("Lower")) return "#22c55e";
  if (level.includes("Upper")) return "#eab308";
  if (level.includes("Graduate")) return "#ef4444";
  return "#3a3a3a";
}

function diffScoreColor(score: number): string {
  if (score <= 3) return "#22c55e";
  if (score <= 6) return "#eab308";
  if (score <= 8) return "#f97316";
  return "#ef4444";
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
  options, selectedDisplayName, programNames, onSelect, loading,
}: {
  options: MajorOption[];
  selectedDisplayName: string;
  programNames: Map<string, string>;
  onSelect: (dbDisplayName: string) => void;
  loading: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selectedLabel = useMemo(() => {
    if (!selectedDisplayName) return "";
    const match = options.find((o) => o.display_name === selectedDisplayName);
    return (match ? programNames.get(match.major_id) : undefined) || selectedDisplayName;
  }, [selectedDisplayName, options, programNames]);

  useEffect(() => {
    if (!open) setQuery(selectedLabel);
  }, [selectedLabel, open]);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery(selectedLabel);
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [selectedLabel]);

  const filtered = useMemo(() => {
    const seen = new Set<string>();
    const q = query.trim().toLowerCase();
    return options
      .filter((o) => {
        if (seen.has(o.display_name)) return false;
        seen.add(o.display_name);
        if (!q) return true;
        const dbName = o.display_name.toLowerCase();
        const apiName = (programNames.get(o.major_id) ?? "").toLowerCase();
        return dbName.includes(q) || apiName.includes(q);
      })
      .slice(0, 40);
  }, [options, query, programNames]);

  function getOptionLabel(opt: MajorOption): string {
    return programNames.get(opt.major_id) || opt.display_name;
  }

  function handleSelect(opt: MajorOption) {
    onSelect(opt.display_name);
    setQuery(getOptionLabel(opt));
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      setQuery(selectedLabel);
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
              {getOptionLabel(opt)}
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
  const [statsLoading, setStatsLoading] = useState(true);

  useEffect(() => {
    setStatsLoading(true);
    setStats(null);
    fetch(`/api/course-stats?id=${encodeURIComponent(courseId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setStats(d ?? null))
      .catch(() => setStats(null))
      .finally(() => setStatsLoading(false));
  }, [courseId]);

  const termsSet = useMemo(() => new Set(info?.terms ?? []), [info]);
  const prof = stats?.professor ?? null;

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

      {/* ── Top professor ── */}
      <div className="px-3 py-2">
        <p className="text-[8px] font-bold uppercase tracking-widest text-[#444] mb-1.5">Top Professor</p>
        {statsLoading ? (
          <p className="text-[10px] text-[#3a3a3a]">Loading…</p>
        ) : prof ? (
          <div>
            <p className="text-[11px] font-semibold text-[#ccc]">{prof.name}</p>
            <div className="flex items-center gap-2 mt-0.5 flex-wrap">
              <span className="text-[10px] font-bold text-[#22c55e]">
                {prof.overall_rating.toFixed(1)}/5 overall
              </span>
              <span className="text-[10px] text-[#555]">·</span>
              <span className="text-[10px] text-[#777]">
                {prof.difficulty_rating.toFixed(1)} difficulty
              </span>
              <span className="text-[10px] text-[#555]">·</span>
              <span className="text-[10px] text-[#555]">{prof.num_ratings} ratings</span>
              {prof.sentiment_label && (
                <>
                  <span className="text-[10px] text-[#555]">·</span>
                  <span className="text-[10px] text-[#3b82f6]">{prof.sentiment_label}</span>
                </>
              )}
            </div>
            <div className="mt-1">
              {stats?.prof_gpa != null && isFinite(stats.prof_gpa) ? (
                <span className="text-[10px] text-[#22c55e]">
                  GPA with this prof: {stats.prof_gpa.toFixed(2)}
                </span>
              ) : (
                <span className="text-[10px] text-[#444]">No GPA data for this prof</span>
              )}
            </div>
          </div>
        ) : (
          <p className="text-[10px] text-[#3a3a3a]">No professor data available</p>
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
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `zone|${qKey}`,
    data: { quarterKey: qKey },
  });

  const units = courseIds.reduce(
    (sum, id) => sum + (courseInfoMap[id]?.min_units ?? 4),
    0,
  );

  const avgDiff =
    courseIds.length > 0
      ? courseIds.reduce((sum, id) => sum + (difficultyMap[id] ?? 5), 0) / courseIds.length
      : null;

  return (
    <div className={`flex flex-col border-r border-[#2a2a2a] last:border-r-0 ${dim ? "opacity-55" : ""}`}>
      {/* header */}
      <div className="flex items-center px-2 h-7 border-b border-[#2a2a2a] shrink-0 bg-[#242424]">
        <span className={`text-[10px] font-semibold ${dim ? "text-[#383838]" : "text-[#666]"}`}>
          {label}
        </span>
        {avgDiff != null && (
          <span style={{ color: diffScoreColor(avgDiff) }} className="text-[9px] ml-1.5">
            ◆ {avgDiff.toFixed(1)}
          </span>
        )}
        {units > 0 && <span className="text-[9px] text-[#555] ml-auto mr-1">{units} UNITS</span>}
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
  courseId, title, units, isPlaced, unavailable, diffScore,
}: {
  courseId: string;
  title?: string | null;
  units?: number | null;
  isPlaced: boolean;
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
  req, placedSet, courseInfoMap, difficultyMap, searchQuery, initialOpen,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
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
            <span className="text-[9px] text-[#444] leading-none">
              {req.courses_needed} of {req.courses.length} required
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
              unavailable={!courseInfoMap[cid]}
              diffScore={difficultyMap[cid] ?? null}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── GE section ─────────────────────────────────────────────────────────────────

function GESection({
  req, placedSet, courseInfoMap, difficultyMap, searchQuery, initialOpen,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
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

  const satisfied = Math.min(
    req.courses.filter((c) => placedSet.has(c)).length,
    req.courses_needed,
  );
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
            <span className="text-[9px] text-[#444] leading-none">
              {req.courses_needed} of {req.courses.length} required
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
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
  const [courseInfoMap, setCourseInfoMap] = useState<Record<string, CourseDetail>>({});
  const [programNames, setProgramNames] = useState<Map<string, string>>(new Map());

  // ── Error + retry state ────────────────────────────────────────────────────
  const [majorListError, setMajorListError] = useState<string | null>(null);
  const [reqError, setReqError] = useState<string | null>(null);
  const [geError, setGeError] = useState<string | null>(null);
  const [majorListRetry, setMajorListRetry] = useState(0);
  const [reqRetry, setReqRetry] = useState(0);
  const [geRetry, setGeRetry] = useState(0);

  // ── UI state ───────────────────────────────────────────────────────────────
  const [sidebarTab, setSidebarTab] = useState<"major" | "ge">("major");
  const [selectedDisplayName, setSelectedDisplayName] = useState("");
  const [selectedMajorId, setSelectedMajorId] = useState("");
  const [gradQuarter, setGradQuarter] = useState("2028_spring");
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

  // ── Derived ────────────────────────────────────────────────────────────────
  const placedSet = useMemo(() => {
    const s = new Set<string>();
    for (const ids of Object.values(plannedCourses)) ids.forEach((id) => s.add(id));
    return s;
  }, [plannedCourses]);

  const totalUnits = useMemo(
    () =>
      Object.values(plannedCourses).reduce(
        (sum, ids) => sum + ids.reduce((s, id) => s + (courseInfoMap[id]?.min_units ?? 4), 0),
        0,
      ),
    [plannedCourses, courseInfoMap],
  );

  const majorGroups = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const { major_id, display_name } of majorList) {
      const ex = map.get(display_name);
      if (ex) ex.push(major_id);
      else map.set(display_name, [major_id]);
    }
    return map;
  }, [majorList]);

  const specializations = useMemo(
    () => (selectedDisplayName ? (majorGroups.get(selectedDisplayName) ?? []) : []),
    [majorGroups, selectedDisplayName],
  );

  const selectedLabel = useMemo(() => {
    if (!selectedMajorId) return "";
    return programNames.get(selectedMajorId) || selectedDisplayName || selectedMajorId;
  }, [selectedMajorId, selectedDisplayName, programNames]);

  const totalRequired = useMemo(
    () => requirements.reduce((s, r) => s + r.courses_needed, 0),
    [requirements],
  );

  const placedRequired = useMemo(
    () =>
      [...placedSet].filter((id) => requirements.some((r) => r.courses.includes(id))).length,
    [placedSet, requirements],
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

  // ── Fetch major list ───────────────────────────────────────────────────────
  useEffect(() => {
    setMajorListError(null);
    fetchMajors()
      .then(setMajorList)
      .catch((e: Error) => setMajorListError(e.message));
  }, [majorListRetry]);

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
    fetchMajorRequirements(selectedMajorId)
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

  // ── Prereq validation ──────────────────────────────────────────────────────
  const validatePlan = useCallback(async (placed: PlannedCourses) => {
    const locked: Record<string, string> = {};
    for (const [qk, ids] of Object.entries(placed)) {
      for (const id of ids) locked[id] = qk;
    }
    if (Object.keys(locked).length === 0) return;
    const payload = { locked_courses: locked };
    console.log("[validate-plan] payload:", payload);
    try {
      const res = await fetch("/api/validate-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) return;
      const data = (await res.json()) as { valid: boolean; conflicts: string[]; online?: boolean };
      console.log("[validate-plan] response:", data);
      setOptimizerOnline(data.online ?? true);
      if (!data.valid && data.conflicts.length > 0) {
        setPrereqWarnings((prev) => {
          const next = { ...prev };
          for (const conflict of data.conflicts) {
            // Backend format: "COURSEID locked to QUARTER: BLOCKER must be placed in an earlier quarter"
            const courseId = conflict.split(" locked to")[0]?.trim();
            if (!courseId) continue;
            const afterColon = conflict.split(": ").slice(1).join(": ");
            const blocker = afterColon
              ? afterColon.replace(" must be placed in an earlier quarter", "").trim()
              : "a prerequisite";
            if (!(courseId in next)) {
              next[courseId] = `Needs: ${blocker}`;
            }
          }
          return next;
        });
      }
    } catch {
      setOptimizerOnline(false);
    }
  }, []);

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
    (dbDisplayName: string) => {
      setSelectedDisplayName(dbDisplayName);
      const ids = majorGroups.get(dbDisplayName) ?? [];
      setSelectedMajorId(ids[0] ?? "");
      setRequirements([]);
      setReqError(null);
    },
    [majorGroups],
  );

  const toggleLock = useCallback((id: string) => {
    setLockedCourses((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

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

  const requiredGroups = requirements.filter((r) => r.requirement_type === "required");
  const electiveGroups = requirements.filter((r) => r.requirement_type === "elective");

  // Index of the first incomplete group in each section (for default-open on load)
  const firstIncompleteReq = requiredGroups.findIndex(
    (r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed,
  );
  const firstIncompleteElec = electiveGroups.findIndex(
    (r) => r.courses.filter((c) => placedSet.has(c)).length < r.courses_needed,
  );
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
            {(["major", "ge"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setSidebarTab(tab)}
                className={`flex-1 py-2 text-[10px] font-bold tracking-widest uppercase transition-colors
                  ${sidebarTab === tab
                    ? "text-[#f0f0f0] border-b-2 border-[#3b82f6]"
                    : "text-[#444] hover:text-[#666]"}`}
              >
                {tab === "major" ? "Major" : "GE"}
              </button>
            ))}
          </div>

          {/* Fixed top */}
          <div className="px-2 pt-2 flex flex-col gap-1.5 shrink-0">
            {majorListError ? (
              <ErrorBanner
                message="Failed to load majors"
                onRetry={() => setMajorListRetry((n) => n + 1)}
              />
            ) : (
              <MajorCombobox
                options={majorList}
                selectedDisplayName={selectedDisplayName}
                programNames={programNames}
                onSelect={handleMajorNameChange}
                loading={majorList.length === 0 && !majorListError}
              />
            )}

            {specializations.length > 1 && (
              <select
                value={selectedMajorId}
                onChange={(e) => setSelectedMajorId(e.target.value)}
                className="w-full bg-[#111] border border-[#2a2a2a] rounded px-2.5 py-1.5 text-[11px] text-[#f0f0f0] focus:outline-none focus:border-[#3b82f6]/60"
              >
                {specializations.map((id) => (
                  <option key={id} value={id}>{programNames.get(id) || id}</option>
                ))}
              </select>
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

            {selectedMajorId && !loadingReqs && !reqError && (
              <p className="text-[9px] text-[#444] pb-0.5">
                <span className="text-[#666] font-semibold">{placedRequired}</span>
                {" "}of{" "}
                <span className="text-[#666] font-semibold">{totalRequired}</span>
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
                    {requiredGroups.length > 0 && (
                      <>
                        <div className="px-2 pt-2 pb-1">
                          <span className="text-[8px] font-bold uppercase tracking-[0.15em] text-[#333]">
                            Required
                          </span>
                        </div>
                        {requiredGroups.map((r, i) => (
                          <RequirementGroup
                            key={r.id} req={r} placedSet={placedSet}
                            courseInfoMap={courseInfoMap} difficultyMap={difficultyMap}
                            searchQuery={searchQuery}
                            initialOpen={i === firstIncompleteReq}
                          />
                        ))}
                      </>
                    )}
                    {electiveGroups.length > 0 && (
                      <>
                        <div className="px-2 pt-3 pb-1">
                          <span className="text-[8px] font-bold uppercase tracking-[0.15em] text-[#333]">
                            Electives
                          </span>
                        </div>
                        {electiveGroups.map((r, i) => (
                          <RequirementGroup
                            key={r.id} req={r} placedSet={placedSet}
                            courseInfoMap={courseInfoMap} difficultyMap={difficultyMap}
                            searchQuery={searchQuery}
                            initialOpen={i === firstIncompleteElec}
                          />
                        ))}
                      </>
                    )}
                    {!selectedMajorId && (
                      <p className="text-[10px] text-[#333] text-center py-12">
                        Search for your major above
                      </p>
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
          </div>

          {/* Auto-fill */}
          <div className="px-2.5 py-2.5 border-t border-[#2a2a2a] shrink-0">
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
            <button
              disabled={!selectedMajorId || autoFillLoading}
              onClick={async () => {
                if (!selectedMajorId || autoFillLoading) return;
                setAutoFillLoading(true);
                setToast(null);
                try {
                  const res = await fetch("/api/optimizer", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      major_id: selectedMajorId,
                      completed_courses: [],
                      graduation_quarter: gradQuarter,
                      units_per_quarter: 16,
                      waived_ges: [],
                    }),
                  });
                  const data = await res.json();
                  if (!res.ok) {
                    const msg =
                      typeof data?.detail === "object"
                        ? (data.detail.message ?? JSON.stringify(data.detail))
                        : (data?.detail ?? data?.error ?? "Optimizer error");
                    setToast(String(msg));
                  } else {
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
          </div>
        </aside>

        {/* ── Main ─────────────────────────────────────────────────────────── */}
        <main className="flex-1 flex flex-col overflow-hidden bg-[#111]">

          {/* Top bar */}
          <div className="h-10 shrink-0 flex items-center px-5 border-b border-[#2a2a2a] bg-[#141414] gap-3">
            <span className="text-[11px] text-[#666] truncate max-w-[260px]">
              {selectedLabel || <span className="text-[#333]">No major selected</span>}
            </span>
            <div className="flex-1" />
            <div className="flex items-center gap-1.5">
              <span className="text-[9px] font-bold uppercase tracking-widest text-[#333]">Grad</span>
              <select
                value={gradQuarter}
                onChange={(e) => setGradQuarter(e.target.value)}
                className="h-6 rounded border border-[#2a2a2a] bg-[#1a1a1a] px-1.5 text-[10px] text-[#999] focus:outline-none focus:border-[#3b82f6]/60"
              >
                {GRAD_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
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
              {YEARS.map((year) => {
                const hasSummer = summerYears.has(year);
                const summerQk = qkey(year, "summer");

                return (
                  <div key={year} className="flex border-b border-[#2a2a2a] last:border-b-0">
                    {/* Year label */}
                    <div className="w-8 shrink-0 flex items-center justify-center border-r border-[#2a2a2a] bg-[#0f0f0f]">
                      <span
                        className="text-[7px] font-bold uppercase tracking-[0.2em] text-[#2a2a2a] select-none"
                        style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
                      >
                        Y{year}
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
