"""
Course plan generator combining hard and soft constraints.

generate(major_id, completed_courses, graduation_year, units_per_quarter=16)
→ list[OptimizationResult]   # top 3 variants, sorted by soft_score ascending

Steps inside generate():
  1. Load required + elective + GE course groups from major_requirements.
  2. Deduplicate and exclude already-completed courses.
  3. Build a topological ordering via prerequisite_tree evaluation (ASAP).
  4. Distribute courses across quarters respecting the unit cap.
  5. Generate 5 variants by perturbing the base plan with different seeds
     and running optimize() on each.  Return the 3 best.
"""

import datetime
import os
import random
from copy import deepcopy

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
) -> list[str]:
    """Return ordered list of courses still needed for the major.

    For each requirement row, takes `courses_needed` courses not already
    completed or selected.  Courses already counted toward an earlier group
    contribute to `already_have` so the total per-group quota is respected.
    """
    rows = (
        client.table("major_requirements")
        .select("requirement_type,courses,courses_needed")
        .eq("major_id", major_id)
        .execute()
        .data
    )

    selected: list[str] = []
    seen: set[str] = set()  # normalized IDs already selected

    for req in rows:
        course_list: list[str] = req.get("courses") or []
        needed: int = req.get("courses_needed") or len(course_list)

        # Courses in this group already satisfied (completed or selected earlier)
        already_have = sum(
            1 for c in course_list
            if _norm(c) in completed_norm or _norm(c) in seen
        )
        still_needed = max(0, needed - already_have)
        if still_needed == 0:
            continue

        # Candidates: not completed, not already selected
        candidates = [
            c for c in course_list
            if _norm(c) not in completed_norm and _norm(c) not in seen
        ]

        for course in candidates[:still_needed]:
            selected.append(course)
            seen.add(_norm(course))

    return selected


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
) -> list[OptimizationResult]:
    """Generate up to 3 optimized plan variants for a major.

    Returns variants sorted by soft_score ascending (best first).
    Courses that cannot fit within the graduation window are omitted.
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    completed_norm = {_norm(c) for c in completed_courses}

    # 1. Collect courses still needed
    courses_to_plan = _collect_courses(client, major_id, completed_norm)
    if not courses_to_plan:
        return []

    # 2. Load prereq trees for scheduling eligibility checks
    trees = _fetch_prereq_trees(client, courses_to_plan)

    # 3. Quarters available before graduation
    quarters = _generate_quarters(graduation_year)
    if not quarters:
        return []

    # 4. ASAP schedule — earliest possible placement for each course
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
    #    Seed 0 → no perturbation (optimizes base plan directly).
    #    Seeds 1-4 → increasing perturbation before optimizing.
    configs = [
        (42, 0),    # (seed, n_swaps)
        (13, 4),
        (7,  8),
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
    return variants[:3]


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    MAJOR_ID = "BS-201G"   # CS major at UCI
    GRAD_YEAR = 2028

    print(f"Generating plan: major={MAJOR_ID}, completed=[], graduation={GRAD_YEAR}\n")
    variants = generate(
        major_id=MAJOR_ID,
        completed_courses=[],
        graduation_year=GRAD_YEAR,
        units_per_quarter=16,
    )

    if not variants:
        print("No variants generated — check major_id or graduation_year.")
        raise SystemExit(1)

    # ── Variant 1 layout ──────────────────────────────────────────────────────
    best = variants[0].plan
    total_planned = sum(len(cs) for cs in best.planned_courses.values())
    print(f"Courses planned (variant 1): {total_planned}")
    print()
    print(f"{'Quarter':<16}  {'Courses':>7}  {'Units':>5}")
    print("-" * 34)
    for q in sorted(best.planned_courses.keys(), key=_qkey):
        cs = best.planned_courses[q]
        n = len(cs)
        print(f"  {q:<14}  {n:>7}  {n * UNITS_PER_COURSE:>5}")
    print()

    # ── Soft scores for all 3 variants ────────────────────────────────────────
    print(f"{'Variant':<10}  {'soft_score':>10}  breakdown")
    print("-" * 65)
    for i, v in enumerate(variants):
        bd = "  ".join(f"{k[:6]}={val:.3f}" for k, val in v.soft_breakdown.items())
        print(f"  {i + 1:<8}  {v.soft_score:>10.4f}  {bd}")
    print()

    passed = variants[0].soft_score <= variants[2].soft_score
    spread = variants[2].soft_score - variants[0].soft_score
    print(
        f"Variant 1 ≤ Variant 3 (spread={spread:.4f}): "
        f"[{'PASS' if passed else 'FAIL'}]"
    )
