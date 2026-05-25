"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
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

// ── Constants ─────────────────────────────────────────────────────────────────

const START_YEAR = 2026;
const YEARS = [1, 2, 3, 4];
const BASE_QUARTERS = [
  { key: "fall",   label: "Fall"   },
  { key: "winter", label: "Winter" },
  { key: "spring", label: "Spring" },
];

function qkey(year: number, q: string) {
  return `${START_YEAR + year - 1}_${q}`;
}

function generateGradOptions() {
  const out: { value: string; label: string }[] = [];
  const seq = ["winter", "spring", "fall"];
  let year = 2026;
  let qi = 1; // start at spring

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

// ── Icons ─────────────────────────────────────────────────────────────────────

function LockIcon({ locked }: { locked: boolean }) {
  return (
    <svg viewBox="0 0 14 14" fill="none"
      className={`w-2.5 h-2.5 transition-colors ${locked ? "text-amber-400" : "text-zinc-700 group-hover/card:text-zinc-500"}`}>
      <rect x="3" y="6" width="8" height="6" rx="1.2" stroke="currentColor" strokeWidth="1.3"/>
      <path d="M5 6V4.5a2 2 0 014 0V6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 12 12" fill="none"
      className={`w-2.5 h-2.5 text-zinc-600 transition-transform shrink-0 ${open ? "rotate-180" : ""}`}>
      <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  );
}

function ErrorBanner({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 px-2 py-1.5 rounded border border-red-900/40 bg-red-950/20 mx-1">
      <span className="text-[10px] text-red-400 truncate">{message}</span>
      <button
        onClick={onRetry}
        className="text-[9px] text-red-300 underline shrink-0 hover:text-red-200"
      >
        Retry
      </button>
    </div>
  );
}

// ── Placed course card (compact single-line) ───────────────────────────────────

function PlacedCard({
  courseId,
  quarterKey,
  title,
  units,
  isLocked,
  onToggleLock,
}: {
  courseId: string;
  quarterKey: string;
  title?: string | null;
  units?: number | null;
  isLocked: boolean;
  onToggleLock: (id: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `placed|${quarterKey}|${courseId}`,
    data: { type: "placed", courseId, quarterKey } satisfies DragData,
  });

  const style: React.CSSProperties = {
    ...(transform ? { transform: CSS.Translate.toString(transform) } : {}),
    ...(isLocked ? { borderLeft: "2px solid rgba(245,158,11,0.6)" } : {}),
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group/card flex items-center gap-1 rounded px-1 py-[3px] select-none transition-colors
        ${isDragging ? "opacity-20" : ""}
        ${isLocked
          ? "bg-amber-500/[0.04] border border-zinc-700/25"
          : "bg-zinc-800/50 border border-zinc-700/30 hover:border-zinc-600/40"
        }`}
    >
      <span
        {...listeners}
        {...attributes}
        className="text-[9px] leading-none text-zinc-800 group-hover/card:text-zinc-600 cursor-grab active:cursor-grabbing shrink-0 select-none"
      >
        ⠿
      </span>
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-600 shrink-0" />
      <span className="text-[10px] font-bold shrink-0" style={{ color: "#e8e8e8" }}>
        {courseId}
      </span>
      {title && (
        <span className="text-[9px] text-zinc-600 truncate flex-1 min-w-0">{title}</span>
      )}
      {!title && <span className="flex-1" />}
      <span className="text-[9px] text-zinc-700 shrink-0">{units ?? 4}u</span>
      <button
        onPointerDown={(e) => e.stopPropagation()}
        onClick={() => onToggleLock(courseId)}
        className="shrink-0"
        title={isLocked ? "Unlock" : "Lock to quarter"}
      >
        <LockIcon locked={isLocked} />
      </button>
    </div>
  );
}

// ── Quarter cell ──────────────────────────────────────────────────────────────

function QuarterCell({
  qKey, label, dim,
  courseIds, courseInfoMap, lockedCourses, onToggleLock,
  removable, onRemove,
}: {
  qKey: string;
  label: string;
  dim: boolean;
  courseIds: string[];
  courseInfoMap: Record<string, CourseDetail>;
  lockedCourses: Set<string>;
  onToggleLock: (id: string) => void;
  removable?: boolean;
  onRemove?: () => void;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `zone|${qKey}`,
    data: { quarterKey: qKey },
  });

  const units = courseIds.reduce((sum, id) => sum + (courseInfoMap[id]?.min_units ?? 4), 0);

  return (
    <div className={`flex flex-col border-r border-white/[0.04] ${dim ? "opacity-70" : ""}`}>
      <div className={`flex items-center gap-1.5 px-2 py-1 border-b border-white/[0.05] shrink-0
        ${dim ? "bg-[#111]" : "bg-[#131313]"}`}
      >
        <span className={`text-[10px] font-semibold ${dim ? "text-zinc-700" : "text-zinc-400"}`}>
          {label}
        </span>
        <span className="text-[9px] text-zinc-700 ml-1">{units}u</span>
        {removable && onRemove && (
          <button
            onClick={onRemove}
            className="ml-auto text-[9px] text-zinc-700 hover:text-zinc-400 transition-colors leading-none"
          >
            ✕
          </button>
        )}
      </div>
      <div
        ref={setNodeRef}
        className={`flex-1 flex flex-col gap-[3px] p-1.5 transition-colors
          ${dim ? "bg-[#161616]" : "bg-[#1b1b1b]"}
          ${isOver ? "!bg-[#152035]" : ""}`}
      >
        {courseIds.map((cid) => (
          <PlacedCard
            key={cid}
            courseId={cid}
            quarterKey={qKey}
            title={courseInfoMap[cid]?.title}
            units={courseInfoMap[cid]?.min_units}
            isLocked={lockedCourses.has(cid)}
            onToggleLock={onToggleLock}
          />
        ))}
        {courseIds.length === 0 && (
          <div className={`flex-1 flex items-center justify-center text-[9px] min-h-[56px]
            ${isOver ? "text-blue-400/50" : "text-zinc-800"}`}
          >
            {isOver ? "drop here" : ""}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Sidebar card ──────────────────────────────────────────────────────────────

function SidebarCard({
  courseId, title, units, isPlaced,
}: {
  courseId: string;
  title?: string | null;
  units?: number | null;
  isPlaced: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `sidebar|${courseId}`,
    data: { type: "sidebar", courseId } satisfies DragData,
    disabled: isPlaced,
  });

  const style = transform ? { transform: CSS.Translate.toString(transform) } : undefined;

  if (isPlaced) {
    return (
      <div className="flex items-center gap-1.5 rounded px-1.5 py-[3px] opacity-35">
        <span className="text-green-500 text-[9px] shrink-0">✓</span>
        <span className="text-[10px] text-zinc-600 truncate">{courseId}</span>
        {title && <span className="text-[9px] text-zinc-700 truncate flex-1 min-w-0">{title}</span>}
      </div>
    );
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...listeners}
      {...attributes}
      className={`flex items-center gap-1.5 rounded px-1.5 py-[3px] border cursor-grab active:cursor-grabbing select-none transition-colors
        ${isDragging
          ? "border-blue-500/30 bg-blue-500/5 opacity-40"
          : "border-zinc-700/40 bg-zinc-800/30 hover:border-zinc-600/50 hover:bg-zinc-700/30"
        }`}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-600 shrink-0" />
      <span className="text-[10px] font-semibold text-zinc-200 shrink-0">{courseId}</span>
      {title && <span className="text-[9px] text-zinc-500 truncate flex-1 min-w-0">{title}</span>}
      {!title && <span className="flex-1" />}
      <span className="text-[9px] text-zinc-700 shrink-0">{units ?? 4}u</span>
    </div>
  );
}

// ── Requirement group section ──────────────────────────────────────────────────

function RequirementGroup({
  req, placedSet, courseInfoMap, searchQuery,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  courseInfoMap: Record<string, CourseDetail>;
  searchQuery: string;
}) {
  const [open, setOpen] = useState(true);

  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter(
      (cid) =>
        cid.toLowerCase().includes(q) ||
        (courseInfoMap[cid]?.title ?? "").toLowerCase().includes(q)
    );
  }, [req.courses, searchQuery, courseInfoMap]);

  if (filtered.length === 0) return null;

  const placed = filtered.filter((c) => placedSet.has(c)).length;

  return (
    <div className="rounded overflow-hidden border border-white/[0.05]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-2 py-1.5 hover:bg-white/[0.02] transition-colors text-left"
      >
        <span className="text-[10px] font-semibold text-zinc-300 truncate">{req.group_name}</span>
        <div className="flex items-center gap-1.5 ml-2 shrink-0">
          <span className="text-[9px] px-1 py-0.5 rounded bg-zinc-800 text-zinc-500">
            {placed}/{filtered.length}
          </span>
          <ChevronIcon open={open} />
        </div>
      </button>
      {open && (
        <div className="flex flex-col gap-px pb-1.5 px-1">
          {filtered.map((cid) => (
            <SidebarCard
              key={cid}
              courseId={cid}
              title={courseInfoMap[cid]?.title}
              units={courseInfoMap[cid]?.min_units}
              isPlaced={placedSet.has(cid)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── GE section ────────────────────────────────────────────────────────────────

function GESection({
  req, placedSet, searchQuery,
}: {
  req: ReqGroup;
  placedSet: Set<string>;
  searchQuery: string;
}) {
  const [open, setOpen] = useState(false);
  const LIMIT = 40;

  const filtered = useMemo(() => {
    if (!searchQuery) return req.courses;
    const q = searchQuery.toLowerCase();
    return req.courses.filter((cid) => cid.toLowerCase().includes(q));
  }, [req.courses, searchQuery]);

  if (filtered.length === 0) return null;

  const satisfied = filtered.filter((c) => placedSet.has(c)).length;
  const done = satisfied >= req.courses_needed;

  return (
    <div className="rounded overflow-hidden border border-white/[0.05]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-2 py-1.5 hover:bg-white/[0.02] transition-colors text-left"
      >
        <span className="text-[10px] font-semibold text-zinc-300 truncate">{req.group_name}</span>
        <div className="flex items-center gap-1.5 ml-2 shrink-0">
          <span className={`text-[9px] px-1 py-0.5 rounded ${done ? "bg-green-900/50 text-green-400" : "bg-zinc-800 text-zinc-500"}`}>
            {Math.min(satisfied, req.courses_needed)}/{req.courses_needed}
          </span>
          <ChevronIcon open={open} />
        </div>
      </button>
      {open && (
        <div className="flex flex-col gap-px pb-1.5 px-1 max-h-48 overflow-y-auto">
          {filtered.slice(0, LIMIT).map((cid) => (
            <SidebarCard key={cid} courseId={cid} title={null} isPlaced={placedSet.has(cid)} />
          ))}
          {filtered.length > LIMIT && (
            <p className="text-[9px] text-zinc-700 text-center py-1">+{filtered.length - LIMIT} more</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function PlannerClient() {
  // ── Data state ─────────────────────────────────────────────────────────────
  const [majorList, setMajorList] = useState<MajorOption[]>([]);
  const [requirements, setRequirements] = useState<ReqGroup[]>([]);
  const [geRequirements, setGeRequirements] = useState<ReqGroup[]>([]);
  const [courseInfoMap, setCourseInfoMap] = useState<Record<string, CourseDetail>>({});

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

  // ── Derived ────────────────────────────────────────────────────────────────
  const placedSet = useMemo(() => {
    const s = new Set<string>();
    for (const ids of Object.values(plannedCourses)) ids.forEach((id) => s.add(id));
    return s;
  }, [plannedCourses]);

  const totalUnits = useMemo(
    () => Object.values(plannedCourses).reduce(
      (sum, ids) => sum + ids.reduce((s, id) => s + (courseInfoMap[id]?.min_units ?? 4), 0),
      0
    ),
    [plannedCourses, courseInfoMap]
  );

  // Group majorList by display_name (multiple major_ids can share a name = specializations)
  const majorGroups = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const { major_id, display_name } of majorList) {
      const ex = map.get(display_name);
      if (ex) ex.push(major_id);
      else map.set(display_name, [major_id]);
    }
    return map;
  }, [majorList]);

  const sortedMajorNames = useMemo(() => Array.from(majorGroups.keys()).sort(), [majorGroups]);
  const specializations = useMemo(
    () => (selectedDisplayName ? (majorGroups.get(selectedDisplayName) ?? []) : []),
    [majorGroups, selectedDisplayName]
  );

  const remainingCount = useMemo(() => {
    if (!selectedMajorId) return 0;
    const total = requirements.reduce((sum, r) => sum + r.courses_needed, 0);
    return Math.max(0, total - placedSet.size);
  }, [requirements, placedSet, selectedMajorId]);

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
      .then(setGeRequirements)
      .catch((e: Error) => setGeError(e.message));
  }, [geRetry]);

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
      })
      .catch((e: Error) => setReqError(e.message))
      .finally(() => setLoadingReqs(false));
  }, [selectedMajorId, reqRetry]);

  // ── DnD ────────────────────────────────────────────────────────────────────
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  function handleDragStart(e: DragStartEvent) {
    setActiveData(e.active.data.current as DragData);
  }

  function handleDragEnd(e: DragEndEvent) {
    setActiveData(null);
    const src = e.active.data.current as DragData;
    const dst = e.over?.data.current as { quarterKey: string } | undefined;

    if (!dst?.quarterKey) {
      if (src.type === "placed" && src.quarterKey) {
        setPlannedCourses((prev) => ({
          ...prev,
          [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
        }));
      }
      return;
    }

    const tq = dst.quarterKey;
    if (src.type === "sidebar") {
      setPlannedCourses((prev) => {
        if (prev[tq]?.includes(src.courseId)) return prev;
        return { ...prev, [tq]: [...(prev[tq] ?? []), src.courseId] };
      });
    } else if (src.type === "placed" && src.quarterKey && src.quarterKey !== tq) {
      setPlannedCourses((prev) => ({
        ...prev,
        [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
        [tq]: [...(prev[tq] ?? []), src.courseId],
      }));
    }
  }

  // ── Handlers ───────────────────────────────────────────────────────────────
  const handleMajorNameChange = useCallback((name: string) => {
    setSelectedDisplayName(name);
    const ids = majorGroups.get(name) ?? [];
    setSelectedMajorId(ids[0] ?? "");
    setRequirements([]);
    setReqError(null);
  }, [majorGroups]);

  const toggleLock = useCallback((id: string) => {
    setLockedCourses((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
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

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <DndContext sensors={sensors} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
      <div className="flex flex-1 overflow-hidden" style={{ height: "calc(100vh - 56px)" }}>

        {/* ── Sidebar ──────────────────────────────────────────────────────── */}
        <aside className="w-[256px] shrink-0 flex flex-col bg-[#111] border-r border-white/[0.06]">

          {/* Tabs */}
          <div className="flex border-b border-white/[0.07] shrink-0">
            {(["major", "ge"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setSidebarTab(tab)}
                className={`flex-1 py-2 text-[10px] font-bold tracking-widest uppercase transition-colors
                  ${sidebarTab === tab
                    ? "text-white border-b-2 border-blue-500 bg-white/[0.02]"
                    : "text-zinc-600 hover:text-zinc-400"}`}
              >
                {tab === "major" ? "Major" : "GE"}
              </button>
            ))}
          </div>

          {/* Major selectors */}
          <div className="px-2.5 py-2 border-b border-white/[0.05] shrink-0 flex flex-col gap-1.5">
            {majorListError ? (
              <ErrorBanner
                message="Failed to load majors"
                onRetry={() => setMajorListRetry((n) => n + 1)}
              />
            ) : (
              <select
                value={selectedDisplayName}
                onChange={(e) => handleMajorNameChange(e.target.value)}
                disabled={majorList.length === 0}
                className="w-full rounded border border-white/[0.08] bg-[#1a1a1a] px-2 py-1.5 text-[10px] text-zinc-200 focus:outline-none focus:border-blue-500/50 disabled:opacity-50"
              >
                <option value="">{majorList.length === 0 ? "Loading majors…" : "Select major…"}</option>
                {sortedMajorNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            )}

            {specializations.length > 1 && (
              <select
                value={selectedMajorId}
                onChange={(e) => setSelectedMajorId(e.target.value)}
                className="w-full rounded border border-white/[0.08] bg-[#1a1a1a] px-2 py-1.5 text-[10px] text-zinc-200 focus:outline-none focus:border-blue-500/50"
              >
                {specializations.map((id) => <option key={id} value={id}>{id}</option>)}
              </select>
            )}

            {selectedMajorId && !loadingReqs && !reqError && (
              <p className="text-[9px] text-zinc-600">
                <span className="text-zinc-400 font-semibold">{remainingCount}</span> courses remaining
              </p>
            )}
          </div>

          {/* Search */}
          <div className="px-2.5 py-2 border-b border-white/[0.05] shrink-0">
            <div className="relative flex items-center">
              <svg className="absolute left-2 w-3 h-3 text-zinc-600 pointer-events-none" viewBox="0 0 12 12" fill="none">
                <circle cx="5" cy="5" r="3.5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M8 8l2.5 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
              <input
                type="text"
                placeholder="Search courses…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-7 pr-2 py-1 rounded border border-white/[0.07] bg-[#1a1a1a] text-[10px] text-zinc-300 placeholder-zinc-600 focus:outline-none focus:border-blue-500/40"
              />
            </div>
          </div>

          {/* Course list */}
          <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1">

            {sidebarTab === "major" && (
              <>
                {loadingReqs && (
                  <p className="text-[10px] text-zinc-600 text-center py-4">Loading requirements…</p>
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
                        <p className="text-[8px] font-bold uppercase tracking-widest text-zinc-600 pt-0.5 pb-0.5 px-1">Required</p>
                        {requiredGroups.map((r) => (
                          <RequirementGroup key={r.id} req={r} placedSet={placedSet}
                            courseInfoMap={courseInfoMap} searchQuery={searchQuery} />
                        ))}
                      </>
                    )}
                    {electiveGroups.length > 0 && (
                      <>
                        <p className="text-[8px] font-bold uppercase tracking-widest text-zinc-600 pt-2 pb-0.5 px-1">Electives</p>
                        {electiveGroups.map((r) => (
                          <RequirementGroup key={r.id} req={r} placedSet={placedSet}
                            courseInfoMap={courseInfoMap} searchQuery={searchQuery} />
                        ))}
                      </>
                    )}
                    {!selectedMajorId && (
                      <p className="text-[10px] text-zinc-700 text-center py-8">Select a major to see requirements</p>
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
                    <p className="text-[8px] font-bold uppercase tracking-widest text-zinc-600 py-0.5 px-1">General Education</p>
                    {geRequirements.map((r) => (
                      <GESection key={r.id} req={r} placedSet={placedSet} searchQuery={searchQuery} />
                    ))}
                    {geRequirements.length === 0 && (
                      <p className="text-[10px] text-zinc-700 text-center py-8">Loading…</p>
                    )}
                  </>
                )}
              </>
            )}
          </div>

          {/* Auto-fill */}
          <div className="px-2.5 py-2.5 border-t border-white/[0.06] shrink-0">
            <button
              disabled={!selectedMajorId || autoFillLoading}
              onClick={() => {
                if (!selectedMajorId || autoFillLoading) return;
                setAutoFillLoading(true);
                setTimeout(() => setAutoFillLoading(false), 1500);
              }}
              className={`w-full flex items-center justify-center gap-2 rounded-md py-2.5 text-[11px] font-bold tracking-wide transition-all
                ${selectedMajorId && !autoFillLoading
                  ? "bg-blue-600 hover:bg-blue-500 text-white shadow-lg shadow-blue-900/30"
                  : "bg-zinc-800 text-zinc-600 cursor-not-allowed"
                }`}
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
        <main className="flex-1 flex flex-col overflow-hidden bg-[#141414]">

          {/* Top bar */}
          <div className="h-10 shrink-0 flex items-center px-4 border-b border-white/[0.06] bg-[#111] gap-3">
            <span className="text-[10px] font-medium text-zinc-400 truncate max-w-[220px]">
              {selectedDisplayName || <span className="text-zinc-700">—</span>}
            </span>
            <div className="flex-1" />
            <div className="flex items-center gap-1.5">
              <span className="text-[9px] font-semibold uppercase tracking-widest text-zinc-600">Grad</span>
              <select
                value={gradQuarter}
                onChange={(e) => setGradQuarter(e.target.value)}
                className="h-6 rounded border border-white/[0.08] bg-[#1a1a1a] px-1.5 text-[10px] text-zinc-300 focus:outline-none focus:border-blue-500/40"
              >
                {GRAD_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
            <div className="flex-1" />
            <span className="text-[10px] font-bold text-zinc-400">
              {totalUnits}<span className="text-zinc-600 font-normal">u planned</span>
            </span>
          </div>

          {/* Grid */}
          <div className="flex-1 overflow-auto">
            <div className="border border-white/[0.05] m-3 rounded-lg overflow-hidden">
              {YEARS.map((year) => {
                const hasSummer = summerYears.has(year);
                const summerQk = qkey(year, "summer");
                const totalCols = hasSummer ? 4 : 3;

                return (
                  <div key={year} className="flex border-b border-white/[0.05] last:border-b-0">
                    <div className="w-9 shrink-0 flex items-center justify-center border-r border-white/[0.05] bg-[#0f0f0f]">
                      <span
                        className="text-[8px] font-bold uppercase tracking-[0.18em] text-zinc-600 select-none"
                        style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
                      >
                        Year {year}
                      </span>
                    </div>

                    <div className="flex-1 grid" style={{ gridTemplateColumns: `repeat(${totalCols}, 1fr)` }}>
                      {BASE_QUARTERS.map((q) => {
                        const qk = qkey(year, q.key);
                        return (
                          <QuarterCell
                            key={qk} qKey={qk} label={q.label} dim={false}
                            courseIds={plannedCourses[qk] ?? []}
                            courseInfoMap={courseInfoMap}
                            lockedCourses={lockedCourses} onToggleLock={toggleLock}
                          />
                        );
                      })}
                      {hasSummer && (
                        <QuarterCell
                          key={summerQk} qKey={summerQk} label="Summer" dim
                          courseIds={plannedCourses[summerQk] ?? []}
                          courseInfoMap={courseInfoMap}
                          lockedCourses={lockedCourses} onToggleLock={toggleLock}
                          removable onRemove={() => toggleSummer(year)}
                        />
                      )}
                    </div>

                    <div className="w-7 shrink-0 flex items-center justify-center border-l border-white/[0.04] bg-[#0f0f0f]">
                      {!hasSummer && (
                        <button
                          onClick={() => toggleSummer(year)}
                          title="Add Summer"
                          className="flex flex-col items-center gap-0.5 text-zinc-700 hover:text-zinc-400 transition-colors"
                        >
                          <span className="text-[11px] font-bold leading-none">+</span>
                          <span className="text-[7px] font-bold uppercase tracking-wider leading-none"
                            style={{ writingMode: "vertical-rl" }}>Sum</span>
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
          <div className="flex items-center gap-1.5 rounded border border-blue-500/40 bg-[#1e2a3a] px-2 py-1 text-[10px] shadow-xl pointer-events-none">
            <span className="w-1.5 h-1.5 rounded-full bg-zinc-500 shrink-0" />
            <span className="font-bold" style={{ color: "#e8e8e8" }}>{activeData.courseId}</span>
          </div>
        )}
      </DragOverlay>
    </DndContext>
  );
}
