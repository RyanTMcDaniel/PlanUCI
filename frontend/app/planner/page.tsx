export const metadata = { title: "Planner – PlanUCI" };

const YEARS = [1, 2, 3, 4];
const QUARTERS = ["Fall", "Winter", "Spring"];

export default function PlannerPage() {
  return (
    <div className="flex flex-1 overflow-hidden" style={{ height: "calc(100vh - 56px)" }}>
      {/* Sidebar */}
      <aside className="w-[280px] shrink-0 flex flex-col border-r border-white/[0.08] bg-[#141414]">
        <div className="px-4 py-3 border-b border-white/[0.08]">
          <span className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
            Courses
          </span>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <p className="text-sm text-zinc-600">No courses yet</p>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {/* Top bar */}
        <div className="h-12 shrink-0 flex items-center gap-4 px-5 border-b border-white/[0.08] bg-[#141414]">
          {/* Major selector */}
          <select
            disabled
            className="h-7 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-xs text-zinc-400 cursor-not-allowed"
          >
            <option>Select major…</option>
          </select>

          {/* Graduation quarter */}
          <select
            disabled
            className="h-7 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-xs text-zinc-400 cursor-not-allowed"
          >
            <option>Graduation quarter…</option>
          </select>

          <div className="ml-auto text-xs text-zinc-500 font-medium">
            0 units planned
          </div>
        </div>

        {/* 4-year grid */}
        <div className="flex-1 overflow-auto p-5">
          <h1 className="text-sm font-semibold text-zinc-400 mb-4 uppercase tracking-widest">
            Your Plan
          </h1>

          <div className="flex flex-col gap-6">
            {YEARS.map((year) => (
              <div key={year}>
                <div className="text-xs font-semibold text-zinc-600 uppercase tracking-widest mb-2">
                  Year {year}
                </div>
                <div className="grid grid-cols-3 gap-3">
                  {QUARTERS.map((quarter) => (
                    <div
                      key={quarter}
                      className="min-h-[140px] rounded-lg border border-white/[0.07] bg-[#1a1a1a] p-3 flex flex-col gap-2"
                    >
                      <span className="text-xs font-medium text-zinc-500">
                        {quarter}
                      </span>
                      <div className="flex-1 flex items-center justify-center">
                        <span className="text-xs text-zinc-700">Empty</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
