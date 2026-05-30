"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { listPlans, type SavedPlanMeta } from "@/lib/api/plans";

export default function PlansPage() {
  const router = useRouter();
  const [plans, setPlans] = useState<SavedPlanMeta[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listPlans().then(setPlans).catch(() => {}).finally(() => setLoading(false));
  }, []);

  return (
    <div className="max-w-2xl mx-auto py-12 px-4">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold text-white">My Plans</h1>
        <p className="text-sm text-zinc-500 mt-1">Your saved degree plans</p>
      </div>

      {loading ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : plans.length === 0 ? (
        <div className="rounded-lg border border-white/[0.08] bg-[#1e1e1e] px-6 py-10 text-center">
          <p className="text-sm text-zinc-400">No saved plans yet.</p>
          <p className="text-xs text-zinc-600 mt-1">
            Start planning and your progress saves automatically.
          </p>
          <button
            onClick={() => router.push("/planner")}
            className="mt-4 rounded-md bg-blue-600 hover:bg-blue-500 px-4 py-2 text-sm font-medium text-white transition-colors"
          >
            Go to Planner
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {plans.map((plan) => {
            const courseCount = Object.values(plan.plan_data.plannedCourses ?? {}).flat().length;
            const major =
              plan.plan_data.selectedDisplayName ||
              plan.plan_data.selectedMajorId ||
              "No major selected";
            const grad = plan.plan_data.gradQuarter?.replace("_", " ") ?? "—";
            const saved = plan.updated_at
              ? new Date(plan.updated_at).toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })
              : null;

            return (
              <div
                key={plan.id}
                className="flex items-center justify-between rounded-lg border border-white/[0.08] bg-[#1e1e1e] px-4 py-4"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-white truncate">{plan.name}</p>
                  <p className="text-xs text-zinc-500 mt-0.5">
                    {major} &middot; Grad {grad} &middot; {courseCount} course
                    {courseCount !== 1 ? "s" : ""}
                    {saved && <> &middot; Saved {saved}</>}
                  </p>
                </div>
                <button
                  onClick={() => router.push("/planner")}
                  className="ml-4 shrink-0 rounded-md bg-blue-600 hover:bg-blue-500 px-3 py-1.5 text-xs font-medium text-white transition-colors"
                >
                  Load
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-8">
        <Link
          href="/planner"
          className="text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
        >
          ← Back to Planner
        </Link>
      </div>
    </div>
  );
}
