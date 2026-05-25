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
import { createClient } from "@/lib/supabase/client";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Requirement {
  id: number;
  group_name: string;
  requirement_type: "required" | "elective" | "GE";
  courses: string[];
  courses_needed: number;
}

interface CourseInfo {
  id: string;
  title: string | null;
  min_units: number | null;
}

type PlannedCourses = Record<string, string[]>;

interface DragData {
  type: "sidebar" | "placed";
  courseId: string;
  quarterKey?: string;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const START_YEAR = 2026;
const YEARS = [1, 2, 3, 4];
const QUARTERS = [
  { key: "fall",   label: "Fall",   dim: false },
  { key: "winter", label: "Winter", dim: false },
  { key: "spring", label: "Spring", dim: false },
  { key: "summer", label: "Summer", dim: true  },
];

function quarterKey(year: number, qKey: string) {
  return `${START_YEAR + year - 1}_${qKey}`;
}

function generateGradOptions() {
  const options: { value: string; label: string }[] = [];
  for (let y = 2026; y <= 2032; y++) {
    for (const q of ["winter", "spring", "fall"]) {
      options.push({
        value: `${y}_${q}`,
        label: `${q.charAt(0).toUpperCase() + q.slice(1)} ${y}`,
      });
    }
  }
  return options;
}
const GRAD_OPTIONS = generateGradOptions();

// ── Small components ──────────────────────────────────────────────────────────

function CourseChip({
  courseId,
  title,
}: {
  courseId: string;
  title?: string | null;
}) {
  return (
    <div className="min-w-0">
      <div className="font-medium text-zinc-200 leading-tight truncate">{courseId}</div>
      {title && (
        <div className="text-[10px] text-zinc-500 leading-tight truncate">{title}</div>
      )}
    </div>
  );
}

function DraggableSidebarCard({
  courseId,
  title,
  isPlaced,
}: {
  courseId: string;
  title?: string | null;
  isPlaced: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({
      id: `sidebar|${courseId}`,
      data: { type: "sidebar", courseId } satisfies DragData,
      disabled: isPlaced,
    });

  const style = transform ? { transform: CSS.Translate.toString(transform) } : undefined;

  if (isPlaced) {
    return (
      <div className="flex items-center gap-2 rounded-md px-2 py-1.5 border border-zinc-800/60 bg-zinc-900/30 opacity-50">
        <svg className="w-3 h-3 text-green-500 shrink-0" viewBox="0 0 12 12" fill="none">
          <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
        <span className="text-xs text-zinc-500 truncate">{courseId}</span>
      </div>
    );
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...listeners}
      {...attributes}
      className={`flex items-start gap-2 rounded-md px-2 py-1.5 border cursor-grab active:cursor-grabbing select-none transition-colors
        ${isDragging
          ? "border-blue-500/40 bg-blue-500/5 opacity-40"
          : "border-zinc-700/60 bg-zinc-800/50 hover:border-zinc-600/70 hover:bg-zinc-700/50"
        }`}
    >
      <svg className="w-3 h-3 text-zinc-600 shrink-0 mt-0.5" viewBox="0 0 12 12" fill="currentColor">
        <rect x="2" y="2" width="2" height="2" rx="0.5"/>
        <rect x="8" y="2" width="2" height="2" rx="0.5"/>
        <rect x="2" y="5" width="2" height="2" rx="0.5"/>
        <rect x="8" y="5" width="2" height="2" rx="0.5"/>
        <rect x="2" y="8" width="2" height="2" rx="0.5"/>
        <rect x="8" y="8" width="2" height="2" rx="0.5"/>
      </svg>
      <div className="text-xs min-w-0">
        <CourseChip courseId={courseId} title={title} />
      </div>
    </div>
  );
}

function DraggablePlacedCard({
  courseId,
  quarterKey,
  title,
}: {
  courseId: string;
  quarterKey: string;
  title?: string | null;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({
      id: `placed|${quarterKey}|${courseId}`,
      data: { type: "placed", courseId, quarterKey } satisfies DragData,
    });

  const style = transform ? { transform: CSS.Translate.toString(transform) } : undefined;

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...listeners}
      {...attributes}
      className={`rounded border px-2 py-1.5 text-xs cursor-grab active:cursor-grabbing select-none transition-colors
        ${isDragging
          ? "border-blue-500/40 bg-blue-500/5 opacity-30"
          : "border-zinc-700/50 bg-zinc-800/70 hover:border-zinc-600/60"
        }`}
    >
      <CourseChip courseId={courseId} title={title} />
    </div>
  );
}

function QuarterCell({
  qKey,
  label,
  dim,
  courseIds,
  courseInfoMap,
}: {
  qKey: string;
  label: string;
  dim: boolean;
  courseIds: string[];
  courseInfoMap: Record<string, CourseInfo>;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `zone|${qKey}`,
    data: { quarterKey: qKey },
  });

  return (
    <div
      ref={setNodeRef}
      className={`flex flex-col rounded-lg border min-h-[130px] transition-colors
        ${dim ? "border-white/[0.04] bg-[#161616]" : "border-white/[0.07] bg-[#1a1a1a]"}
        ${isOver ? "border-blue-500/50 bg-blue-500/[0.06]" : ""}
      `}
    >
      <div className="px-2.5 pt-2 pb-1">
        <span className={`text-[11px] font-semibold tracking-wide ${dim ? "text-zinc-700" : "text-zinc-500"}`}>
          {label}
        </span>
        {courseIds.length > 0 && (
          <span className="ml-1.5 text-[10px] text-zinc-600">
            {courseIds.length * 4}u
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1 px-2 pb-2 flex-1">
        {courseIds.map((cid) => (
          <DraggablePlacedCard
            key={cid}
            courseId={cid}
            quarterKey={qKey}
            title={courseInfoMap[cid]?.title}
          />
        ))}
        {courseIds.length === 0 && (
          <div className={`flex-1 flex items-center justify-center rounded text-[11px]
            ${isOver ? "text-blue-400/70" : "text-zinc-700"}`}
          >
            {isOver ? "Drop here" : "—"}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

function RequirementGroup({
  req,
  placedSet,
  courseInfoMap,
}: {
  req: Requirement;
  placedSet: Set<string>;
  courseInfoMap: Record<string, CourseInfo>;
}) {
  const [open, setOpen] = useState(true);
  const multi = req.courses.length > 1;

  return (
    <div className="border border-white/[0.05] rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-white/[0.03] transition-colors text-left"
      >
        <span className="text-xs font-medium text-zinc-300 truncate">{req.group_name}</span>
        <div className="flex items-center gap-1.5 shrink-0 ml-2">
          {multi && (
            <span className="text-[10px] text-zinc-600">
              choose {req.courses_needed}
            </span>
          )}
          <svg
            className={`w-3 h-3 text-zinc-600 transition-transform ${open ? "rotate-180" : ""}`}
            viewBox="0 0 12 12" fill="none"
          >
            <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </div>
      </button>
      {open && (
        <div className="px-2 pb-2 flex flex-col gap-1">
          {req.courses.map((cid) => (
            <DraggableSidebarCard
              key={cid}
              courseId={cid}
              title={courseInfoMap[cid]?.title}
              isPlaced={placedSet.has(cid)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function GESection({
  req,
  placedSet,
}: {
  req: Requirement;
  placedSet: Set<string>;
}) {
  const [open, setOpen] = useState(false);
  const LIMIT = 40;

  return (
    <div className="border border-white/[0.05] rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-white/[0.03] transition-colors text-left"
      >
        <span className="text-xs font-medium text-zinc-300 truncate">{req.group_name}</span>
        <div className="flex items-center gap-1.5 shrink-0 ml-2">
          <span className="text-[10px] text-zinc-600">
            choose {req.courses_needed} · {req.courses.length} options
          </span>
          <svg
            className={`w-3 h-3 text-zinc-600 transition-transform ${open ? "rotate-180" : ""}`}
            viewBox="0 0 12 12" fill="none"
          >
            <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </div>
      </button>
      {open && (
        <div className="px-2 pb-2 flex flex-col gap-1 max-h-60 overflow-y-auto">
          {req.courses.slice(0, LIMIT).map((cid) => (
            <DraggableSidebarCard
              key={cid}
              courseId={cid}
              title={null}
              isPlaced={placedSet.has(cid)}
            />
          ))}
          {req.courses.length > LIMIT && (
            <div className="text-[10px] text-zinc-600 text-center py-1">
              +{req.courses.length - LIMIT} more — use the search when available
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function PlannerClient() {
  const supabase = useMemo(() => createClient(), []);

  // ── Data state ─────────────────────────────────────────────────────────────
  const [majorOptions, setMajorOptions] = useState<
    Map<string, string[]> // major_name → [major_id, ...]
  >(new Map());
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [geRequirements, setGeRequirements] = useState<Requirement[]>([]);
  const [courseInfoMap, setCourseInfoMap] = useState<Record<string, CourseInfo>>({});

  // ── UI state ───────────────────────────────────────────────────────────────
  const [sidebarTab, setSidebarTab] = useState<"major" | "ge">("major");
  const [selectedMajorName, setSelectedMajorName] = useState("");
  const [selectedMajorId, setSelectedMajorId] = useState("");
  const [gradQuarter, setGradQuarter] = useState("2028_spring");
  const [plannedCourses, setPlannedCourses] = useState<PlannedCourses>({});
  const [activeData, setActiveData] = useState<DragData | null>(null);
  const [loading, setLoading] = useState(false);

  // ── Derived ────────────────────────────────────────────────────────────────
  const placedSet = useMemo(() => {
    const s = new Set<string>();
    for (const ids of Object.values(plannedCourses)) ids.forEach((id) => s.add(id));
    return s;
  }, [plannedCourses]);

  const totalUnits = useMemo(
    () => Object.values(plannedCourses).reduce((sum, ids) => sum + ids.length * 4, 0),
    [plannedCourses]
  );

  const specializations = useMemo(
    () => (selectedMajorName ? (majorOptions.get(selectedMajorName) ?? []) : []),
    [majorOptions, selectedMajorName]
  );

  // ── Fetch majors on mount ──────────────────────────────────────────────────
  useEffect(() => {
    supabase
      .from("major_requirements")
      .select("major_id, major_name")
      .neq("major_id", "ALL_MAJORS")
      .then(({ data }) => {
        if (!data) return;
        const map = new Map<string, string[]>();
        for (const row of data) {
          if (!row.major_name) continue;
          const existing = map.get(row.major_name);
          if (existing) {
            if (!existing.includes(row.major_id)) existing.push(row.major_id);
          } else {
            map.set(row.major_name, [row.major_id]);
          }
        }
        setMajorOptions(map);
      });
  }, [supabase]);

  // ── Fetch GE requirements on mount ────────────────────────────────────────
  useEffect(() => {
    supabase
      .from("major_requirements")
      .select("id, group_name, requirement_type, courses, courses_needed")
      .eq("major_id", "ALL_MAJORS")
      .then(({ data }) => {
        if (data) setGeRequirements(data as Requirement[]);
      });
  }, [supabase]);

  // ── Fetch requirements when major changes ─────────────────────────────────
  useEffect(() => {
    if (!selectedMajorId) {
      setRequirements([]);
      return;
    }
    setLoading(true);
    supabase
      .from("major_requirements")
      .select("id, group_name, requirement_type, courses, courses_needed")
      .eq("major_id", selectedMajorId)
      .then(async ({ data }) => {
        if (!data) { setLoading(false); return; }
        setRequirements(data as Requirement[]);

        // Fetch course titles for all courses in this major
        const allIds = [...new Set(data.flatMap((r: Requirement) => r.courses))];
        const { data: courseData } = await supabase
          .from("courses")
          .select("id, title, min_units")
          .in("id", allIds);

        if (courseData) {
          const map: Record<string, CourseInfo> = {};
          for (const c of courseData) map[c.id] = c;
          setCourseInfoMap((prev) => ({ ...prev, ...map }));
        }
        setLoading(false);
      });
  }, [selectedMajorId, supabase]);

  // ── DnD sensors ───────────────────────────────────────────────────────────
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  );

  function handleDragStart(event: DragStartEvent) {
    setActiveData(event.active.data.current as DragData);
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveData(null);
    const { active, over } = event;
    const src = active.data.current as DragData;
    const dst = over?.data.current as { quarterKey: string } | undefined;

    if (!dst?.quarterKey) {
      // Dropped outside a zone — remove from quarter if it was placed
      if (src.type === "placed" && src.quarterKey) {
        setPlannedCourses((prev) => ({
          ...prev,
          [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
        }));
      }
      return;
    }

    const targetQ = dst.quarterKey;

    if (src.type === "sidebar") {
      setPlannedCourses((prev) => {
        if (prev[targetQ]?.includes(src.courseId)) return prev;
        return { ...prev, [targetQ]: [...(prev[targetQ] ?? []), src.courseId] };
      });
    } else if (src.type === "placed" && src.quarterKey) {
      if (src.quarterKey === targetQ) return;
      setPlannedCourses((prev) => ({
        ...prev,
        [src.quarterKey!]: prev[src.quarterKey!]?.filter((c) => c !== src.courseId) ?? [],
        [targetQ]: [...(prev[targetQ] ?? []), src.courseId],
      }));
    }
  }

  // ── Handlers ───────────────────────────────────────────────────────────────
  function handleMajorNameChange(name: string) {
    setSelectedMajorName(name);
    const ids = majorOptions.get(name) ?? [];
    setSelectedMajorId(ids[0] ?? "");
    setRequirements([]);
  }

  // ── Grouped requirements ───────────────────────────────────────────────────
  const requiredGroups = requirements.filter((r) => r.requirement_type === "required");
  const electiveGroups = requirements.filter((r) => r.requirement_type === "elective");

  const sortedMajorNames = useMemo(
    () => Array.from(majorOptions.keys()).sort(),
    [majorOptions]
  );

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <DndContext sensors={sensors} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
      <div className="flex flex-1 overflow-hidden" style={{ height: "calc(100vh - 56px)" }}>

        {/* ── Sidebar ──────────────────────────────────────────────────────── */}
        <aside className="w-[280px] shrink-0 flex flex-col border-r border-white/[0.08] bg-[#141414]">

          {/* Sidebar tabs */}
          <div className="flex border-b border-white/[0.08]">
            {(["major", "ge"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setSidebarTab(tab)}
                className={`flex-1 py-2.5 text-xs font-medium transition-colors
                  ${sidebarTab === tab
                    ? "text-white border-b-2 border-blue-500"
                    : "text-zinc-500 hover:text-zinc-300"
                  }`}
              >
                {tab === "major" ? "Major" : "GE"}
              </button>
            ))}
          </div>

          <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2">

            {sidebarTab === "major" && (
              <>
                {/* Major selector */}
                <select
                  value={selectedMajorName}
                  onChange={(e) => handleMajorNameChange(e.target.value)}
                  className="w-full rounded-md border border-white/[0.1] bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-blue-500/50"
                >
                  <option value="">Select major…</option>
                  {sortedMajorNames.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </select>

                {/* Specialization (if multiple ids for same major name) */}
                {specializations.length > 1 && (
                  <select
                    value={selectedMajorId}
                    onChange={(e) => setSelectedMajorId(e.target.value)}
                    className="w-full rounded-md border border-white/[0.1] bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-blue-500/50"
                  >
                    {specializations.map((id) => (
                      <option key={id} value={id}>{id}</option>
                    ))}
                  </select>
                )}

                {loading && (
                  <div className="text-xs text-zinc-600 text-center py-4">Loading…</div>
                )}

                {/* Required courses */}
                {requiredGroups.length > 0 && (
                  <>
                    <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 pt-1">
                      Required Courses
                    </div>
                    {requiredGroups.map((req) => (
                      <RequirementGroup
                        key={req.id}
                        req={req}
                        placedSet={placedSet}
                        courseInfoMap={courseInfoMap}
                      />
                    ))}
                  </>
                )}

                {/* Electives */}
                {electiveGroups.length > 0 && (
                  <>
                    <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 pt-1">
                      Electives
                    </div>
                    {electiveGroups.map((req) => (
                      <RequirementGroup
                        key={req.id}
                        req={req}
                        placedSet={placedSet}
                        courseInfoMap={courseInfoMap}
                      />
                    ))}
                  </>
                )}

                {!loading && !selectedMajorId && (
                  <div className="text-xs text-zinc-600 text-center py-8">
                    Select a major to see requirements
                  </div>
                )}
              </>
            )}

            {sidebarTab === "ge" && (
              <>
                <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-600 pb-1">
                  General Education
                </div>
                {geRequirements.map((req) => (
                  <GESection
                    key={req.id}
                    req={req}
                    placedSet={placedSet}
                  />
                ))}
                {geRequirements.length === 0 && (
                  <div className="text-xs text-zinc-600 text-center py-8">Loading…</div>
                )}
              </>
            )}
          </div>
        </aside>

        {/* ── Main content ─────────────────────────────────────────────────── */}
        <main className="flex-1 flex flex-col overflow-hidden">

          {/* Top bar */}
          <div className="h-12 shrink-0 flex items-center gap-3 px-5 border-b border-white/[0.08] bg-[#141414]">
            <span className="text-xs text-zinc-500 truncate max-w-[200px]">
              {selectedMajorName || "No major selected"}
            </span>

            <div className="h-4 w-px bg-white/[0.08]" />

            <div className="flex items-center gap-1.5">
              <label className="text-xs text-zinc-500">Grad:</label>
              <select
                value={gradQuarter}
                onChange={(e) => setGradQuarter(e.target.value)}
                className="h-7 rounded border border-white/[0.1] bg-zinc-900 px-1.5 text-xs text-zinc-300 focus:outline-none focus:border-blue-500/50"
              >
                {GRAD_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>

            <div className="ml-auto text-xs font-medium text-zinc-400">
              {totalUnits} units planned
            </div>
          </div>

          {/* Grid */}
          <div className="flex-1 overflow-auto p-5">
            <h1 className="text-[11px] font-semibold text-zinc-500 mb-4 uppercase tracking-widest">
              Your Plan
            </h1>

            <div className="flex flex-col gap-6 min-w-[700px]">
              {YEARS.map((year) => (
                <div key={year}>
                  <div className="text-[10px] font-semibold text-zinc-600 uppercase tracking-widest mb-2">
                    Year {year}
                  </div>
                  <div className="grid grid-cols-4 gap-2">
                    {QUARTERS.map((q) => {
                      const qk = quarterKey(year, q.key);
                      return (
                        <QuarterCell
                          key={qk}
                          qKey={qk}
                          label={q.label}
                          dim={q.dim}
                          courseIds={plannedCourses[qk] ?? []}
                          courseInfoMap={courseInfoMap}
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </main>
      </div>

      {/* Drag overlay */}
      <DragOverlay dropAnimation={null}>
        {activeData && (
          <div className="rounded-md border border-blue-500/60 bg-[#1e2a3a] px-2 py-1.5 text-xs shadow-xl opacity-90 pointer-events-none">
            <CourseChip
              courseId={activeData.courseId}
              title={courseInfoMap[activeData.courseId]?.title}
            />
          </div>
        )}
      </DragOverlay>
    </DndContext>
  );
}
