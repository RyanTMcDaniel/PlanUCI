"""
Course plan generator combining hard and soft constraints.

generate(major_id, completed_courses, graduation_year, units_per_quarter=16,
         waived_ges=[])
→ GenerationResult  (variants sorted best-first, feasibility metadata)

Steps inside generate():
  1. Load required + elective course groups for the major AND university-wide
     GE groups (major_id = "ALL_MAJORS"), merging and deduplicating.
  2. Run a feasibility check: raise FeasibilityError if there are not enough
     quarters to schedule all required courses.
  3. Build a topological ordering via prerequisite_tree evaluation (ASAP).
  4. Distribute courses across quarters respecting the unit cap.
  5. Generate 5 variants by perturbing the base plan with different seeds
     and running optimize() on each.  Return the 3 best.
"""

import datetime
import math
import os
import random
from copy import deepcopy
from dataclasses import dataclass, field

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import (
    CoursePlan,
    UNITS_PER_COURSE,
    _eval_tree,
    _norm,
    _qkey,
)
from .optimizer import OptimizationResult, optimize

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)


# ── Result / error types ──────────────────────────────────────────────────────

class FeasibilityError(Exception):
    """Raised when the graduation window cannot accommodate all required courses."""

    def __init__(
        self,
        message: str,
        quarters_available: int,
        quarters_needed: int,
        courses_to_complete: int,
        years_to_extend: int,
    ) -> None:
        super().__init__(message)
        self.quarters_available = quarters_available
        self.quarters_needed    = quarters_needed
        self.courses_to_complete = courses_to_complete
        self.years_to_extend    = years_to_extend


@dataclass
class GenerationResult:
    variants:          list[OptimizationResult]
    tight_timeline:    bool = False
    overflow_count:    int  = 0
    quarters_available: int = 0
    quarters_needed:   int  = 0
    # requirement_group → list of course IDs selected from that group
    group_map:         dict[str, list[str]] = field(default_factory=dict)


# ── Quarter helpers ───────────────────────────────────────────────────────────

def _next_quarter() -> tuple[int, str]:
    """Return (year, quarter_name) for the next UCI quarter after today."""
    today = datetime.date.today()
    m, y = today.month, today.year
    if m <= 3:
        return y, "spring"    # currently winter → next is spring
    if m <= 6:
        return y, "fall"      # currently spring → next is fall
    if m <= 8:
        return y, "fall"      # currently summer → next is fall
    return y + 1, "winter"   # currently fall  → next is winter


def _generate_quarters(graduation_year: int) -> list[str]:
    """Quarters from next quarter through spring of graduation_year.

    Uses the standard UCI sequence (winter, spring, fall) and skips summer.
    """
    start_year, start_q = _next_quarter()
    seq = ["winter", "spring", "fall"]

    try:
        idx = seq.index(start_q)
    except ValueError:
        idx = 2  # default to fall

    quarters: list[str] = []
    year = start_year

    while True:
        q = seq[idx]
        quarters.append(f"{year}_{q}")
        if year == graduation_year and q == "spring":
            break
        if year > graduation_year:
            break
        idx = (idx + 1) % len(seq)
        if idx == 0:
            year += 1

    return quarters


# ── Requirement loading ───────────────────────────────────────────────────────

def _collect_courses(
    client,
    major_id: str,
    completed_norm: set[str],
    waived_ges: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (flat_list, group_map) of courses still needed.

    Queries both the major's own requirements and the university-wide GE rows
    (major_id = "ALL_MAJORS").  GE groups whose requirement_group appears in
    waived_ges are skipped entirely.

    group_map maps requirement_group → list of course IDs selected from it,
    which callers can use to audit GE coverage.
    """
    major_rows = (
        client.table("major_requirements")
        .select("requirement_group,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", major_id)
        .execute()
        .data
    )
    ge_rows = (
        client.table("major_requirements")
        .select("requirement_group,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", "ALL_MAJORS")
        .execute()
        .data
    )

    selected: list[str] = []
    seen: set[str] = set()
    group_map: dict[str, list[str]] = {}

    def _process(rows: list[dict]) -> None:
        for req in rows:
            req_group = req.get("requirement_group") or ""
            waivable  = req.get("waivable", False)

            if waivable and req_group in waived_ges:
                continue

            course_list: list[str] = req.get("courses") or []
            needed: int = req.get("courses_needed") or len(course_list)

            already_have = sum(
                1 for c in course_list
                if _norm(c) in completed_norm or _norm(c) in seen
            )
            still_needed = max(0, needed - already_have)
            if still_needed == 0:
                continue

            candidates = [
                c for c in course_list
                if _norm(c) not in completed_norm and _norm(c) not in seen
            ]
            chosen = candidates[:still_needed]
            for course in chosen:
                selected.append(course)
                seen.add(_norm(course))
            if chosen:
                group_map[req_group] = chosen

    _process(major_rows)
    _process(ge_rows)

    return selected, group_map


# ── Feasibility check ─────────────────────────────────────────────────────────

def _check_feasibility(
    total_courses: int,
    quarters: list[str],
    units_per_quarter: int,
) -> tuple[bool, int, int]:
    """Return (tight_timeline, quarters_available, quarters_needed).

    Raises FeasibilityError if the graduation window is too short.
    tight_timeline is True when fewer than 2 quarters of slack remain.
    """
    max_per_q          = units_per_quarter // UNITS_PER_COURSE
    quarters_available = len(quarters)
    quarters_needed    = math.ceil(total_courses / max_per_q) if max_per_q else 0

    if quarters_needed > quarters_available:
        excess_q           = quarters_needed - quarters_available
        courses_to_complete = excess_q * max_per_q
        years_to_extend    = math.ceil(excess_q / 3)
        raise FeasibilityError(
            f"Cannot complete major in {quarters_available} quarters available. "
            f"Minimum {quarters_needed} quarters needed. "
            f"Complete {courses_to_complete} more courses first "
            f"or extend graduation by {years_to_extend} year(s).",
            quarters_available=quarters_available,
            quarters_needed=quarters_needed,
            courses_to_complete=courses_to_complete,
            years_to_extend=years_to_extend,
        )

    tight = (quarters_available - quarters_needed) <= 1
    return tight, quarters_available, quarters_needed


# ── Prereq data ───────────────────────────────────────────────────────────────

def _fetch_prereq_trees(client, course_ids: list[str]) -> dict[str, dict]:
    """Return {norm_id: prerequisite_tree} for each course that has one."""
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,prerequisite_tree")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {
        _norm(r["id"]): r["prerequisite_tree"]
        for r in rows
        if r.get("prerequisite_tree")
    }


# ── ASAP scheduler ────────────────────────────────────────────────────────────

def _asap_schedule(
    courses: list[str],
    prereq_trees: dict[str, dict],
    quarters: list[str],
    units_per_quarter: int,
    completed_norm: set[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Schedule each course in the earliest quarter its prerequisites allow.

    Returns (plan_dict, overflow) where overflow contains courses that could
    not be placed within the graduation window.
    """
    max_per_q = units_per_quarter // UNITS_PER_COURSE
    available: set[str] = set(completed_norm)
    remaining: list[str] = list(courses)
    plan: dict[str, list[str]] = {q: [] for q in quarters}

    for quarter in quarters:
        while len(plan[quarter]) < max_per_q and remaining:
            # Find the first course whose prereqs are satisfied
            eligible_idx = next(
                (
                    i for i, c in enumerate(remaining)
                    if not prereq_trees.get(_norm(c))
                    or _eval_tree(prereq_trees[_norm(c)], available)
                ),
                None,
            )
            if eligible_idx is None:
                break  # nothing unlocked this quarter

            course = remaining.pop(eligible_idx)
            plan[quarter].append(course)
            available.add(_norm(course))

    return plan, remaining  # remaining = overflow


# ── Perturbation ──────────────────────────────────────────────────────────────

def _perturb(plan: CoursePlan, rng: random.Random, n_swaps: int) -> CoursePlan:
    """Swap n_swaps random course pairs between quarters."""
    p = deepcopy(plan)
    quarters = list(p.planned_courses.keys())
    for _ in range(n_swaps):
        non_empty = [q for q in quarters if p.planned_courses.get(q)]
        if len(non_empty) < 2:
            break
        q1, q2 = rng.sample(non_empty, 2)
        c1 = rng.choice(p.planned_courses[q1])
        c2 = rng.choice(p.planned_courses[q2])
        p.planned_courses[q1].remove(c1)
        p.planned_courses[q2].remove(c2)
        p.planned_courses[q1].append(c2)
        p.planned_courses[q2].append(c1)
    return p


# ── Public API ────────────────────────────────────────────────────────────────

def generate(
    major_id: str,
    completed_courses: list[str],
    graduation_year: int,
    units_per_quarter: int = 16,
    waived_ges: list[str] | None = None,
) -> GenerationResult:
    """Generate up to 3 optimized plan variants for a major.

    Parameters
    ----------
    waived_ges : list of requirement_group codes (e.g. ["GE_VI"]) to skip.
                 Only groups marked waivable=True in the DB are skippable.

    Returns
    -------
    GenerationResult with variants sorted by soft_score ascending (best first)
    and feasibility metadata.  Raises FeasibilityError before returning if the
    graduation window is too short.
    """
    if waived_ges is None:
        waived_ges = []

    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    completed_norm = {_norm(c) for c in completed_courses}

    # 1. Collect courses still needed (major + GE)
    courses_to_plan, group_map = _collect_courses(
        client, major_id, completed_norm, waived_ges
    )
    if not courses_to_plan:
        return GenerationResult(variants=[])

    # 2. Quarters available before graduation
    quarters = _generate_quarters(graduation_year)
    if not quarters:
        return GenerationResult(variants=[])

    # 3. Feasibility check — raises FeasibilityError if impossible
    tight, q_available, q_needed = _check_feasibility(
        len(courses_to_plan), quarters, units_per_quarter
    )

    # 4. Load prereq trees and run ASAP schedule
    trees = _fetch_prereq_trees(client, courses_to_plan)
    plan_dict, overflow = _asap_schedule(
        courses_to_plan, trees, quarters, units_per_quarter, completed_norm
    )

    if overflow:
        print(
            f"  Warning: {len(overflow)} course(s) could not fit before "
            f"{graduation_year} — omitted from plan."
        )

    base_plan = CoursePlan(
        major_id=major_id,
        completed_courses=completed_courses,
        planned_courses={q: cs for q, cs in plan_dict.items() if cs},
        graduation_year=graduation_year,
        units_per_quarter=units_per_quarter,
    )

    # 5. Generate variants: perturb then optimize with different seeds
    configs = [
        (42,  0),   # (seed, n_swaps)
        (13,  4),
        (7,   8),
        (99, 12),
        (17, 16),
    ]

    variants: list[OptimizationResult] = []
    for seed, n_swaps in configs:
        rng = random.Random(seed)
        starting = _perturb(base_plan, rng, n_swaps) if n_swaps else base_plan
        result = optimize(starting, max_iter=150, seed=seed)
        variants.append(result)

    variants.sort(key=lambda r: r.soft_score)
    return GenerationResult(
        variants=variants[:3],
        tight_timeline=tight,
        overflow_count=len(overflow),
        quarters_available=q_available,
        quarters_needed=q_needed,
        group_map=group_map,
    )


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    MAJOR_ID = "BS-201G"   # CS major at UCI

    # ── Step 1: feasibility check with graduation=2028 ────────────────────────
    print("=" * 60)
    print(f"Feasibility check  major={MAJOR_ID}  completed=[]  grad=2028")
    print("=" * 60)
    try:
        generate(major_id=MAJOR_ID, completed_courses=[], graduation_year=2028)
        print("  Feasibility: PASS")
        GRAD_YEAR = 2028
    except FeasibilityError as e:
        print(f"  Feasibility: FAIL")
        print(f"  {e}")
        print(f"  quarters_available={e.quarters_available}  "
              f"quarters_needed={e.quarters_needed}")
        GRAD_YEAR = 2028 + e.years_to_extend
        print(f"\n  Extending to graduation_year={GRAD_YEAR} and retrying...\n")

    # ── Step 2: generate with extended (or same) graduation year ──────────────
    print("=" * 60)
    print(f"Generating plan  major={MAJOR_ID}  completed=[]  grad={GRAD_YEAR}")
    print("=" * 60)
    try:
        result = generate(
            major_id=MAJOR_ID,
            completed_courses=[],
            graduation_year=GRAD_YEAR,
            units_per_quarter=16,
        )
    except FeasibilityError as e:
        print(f"  Still infeasible: {e}")
        raise SystemExit(1)

    if not result.variants:
        print("No variants generated.")
        raise SystemExit(1)

    # ── Feasibility metadata ──────────────────────────────────────────────────
    print()
    tl = "YES — only 1 quarter of slack" if result.tight_timeline else "no"
    print(f"Tight timeline:      {tl}")
    print(f"Quarters available:  {result.quarters_available}")
    print(f"Quarters needed:     {result.quarters_needed}")
    print(f"Overflow (omitted):  {result.overflow_count}")
    print()

    # ── GE coverage ───────────────────────────────────────────────────────────
    ge_groups = {k: v for k, v in result.group_map.items() if k.startswith("GE_")}
    planned_norm = {
        _norm(c)
        for cs in result.variants[0].plan.planned_courses.values()
        for c in cs
    }

    print(f"GE courses in plan (variant 1):")
    any_ge_scheduled = False
    for grp, selected in sorted(ge_groups.items()):
        scheduled = [c for c in selected if _norm(c) in planned_norm]
        any_ge_scheduled = any_ge_scheduled or bool(scheduled)
        print(f"  {grp:<22}  selected={len(selected)}  "
              f"scheduled={len(scheduled)}  "
              f"courses={scheduled[:3]}{'…' if len(scheduled) > 3 else ''}")
    print(f"  Any GE courses in plan: {'YES' if any_ge_scheduled else 'NO'}")
    print()

    # ── Quarter layout (variant 1) ────────────────────────────────────────────
    best = result.variants[0].plan
    total_planned = sum(len(cs) for cs in best.planned_courses.values())
    print(f"Courses planned (variant 1): {total_planned}")
    print(f"{'Quarter':<16}  {'Courses':>7}  {'Units':>5}")
    print("-" * 34)
    for q in sorted(best.planned_courses.keys(), key=_qkey):
        cs = best.planned_courses[q]
        n  = len(cs)
        ge_n = sum(1 for c in cs if _norm(c) in {_norm(x) for grp in ge_groups.values() for x in grp})
        ge_tag = f"  ({ge_n} GE)" if ge_n else ""
        print(f"  {q:<14}  {n:>7}  {n * UNITS_PER_COURSE:>5}{ge_tag}")
    print()

    # ── Soft scores for all 3 variants ────────────────────────────────────────
    print(f"{'Variant':<10}  {'soft_score':>10}  breakdown")
    print("-" * 68)
    for i, v in enumerate(result.variants):
        bd = "  ".join(f"{k[:6]}={val:.3f}" for k, val in v.soft_breakdown.items())
        print(f"  {i + 1:<8}  {v.soft_score:>10.4f}  {bd}")
    print()

    passed = result.variants[0].soft_score <= result.variants[2].soft_score
    spread = result.variants[2].soft_score - result.variants[0].soft_score
    print(f"Variant 1 ≤ Variant 3 (spread={spread:.4f}): [{'PASS' if passed else 'FAIL'}]")
