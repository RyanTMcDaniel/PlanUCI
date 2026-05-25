"""
What-if planner: regenerate a course plan with selected courses pinned
to specific quarters.

validate_locks(locked_courses) → (bool, list[str])
    Quick check that the locked quarter assignments don't conflict with
    each other prereq-wise.  Fires before the full optimizer runs so the
    UI can give instant feedback when a student locks a course.

run_whatif(plan, locked_courses, major_id, graduation_quarter,
           units_per_quarter=16, waived_ges=[]) → WhatIfResult
    Builds a new plan with locked courses fixed, ASAP-schedules the
    remaining required courses around them, then hill-climbs on unlocked
    courses only.  Returns top 3 variants.
"""

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
from .optimizer import _check_prereqs, _prereq_trees, _soft_score
from .plan_generator import (
    _asap_schedule,
    _collect_courses,
    _fetch_prereq_trees,
    _generate_quarters,
)
from .soft_constraints import _load_course_meta, _load_difficulty_scores

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class WhatIfVariant:
    planned_courses:  dict[str, list[str]]
    locked_courses:   dict[str, str]        # {course_id: quarter}
    soft_score:       float
    soft_breakdown:   dict[str, float]
    violations:       list[str]


@dataclass
class WhatIfResult:
    variants:           list[WhatIfVariant]
    lock_conflicts:     list[str]
    quarters_available: int  = 0
    quarters_needed:    int  = 0
    tight_timeline:     bool = False
    overflow_count:     int  = 0


# ── Lock validation ───────────────────────────────────────────────────────────

def _eval_locked_item(item: dict, avail: set[str], locked_norm: set[str]) -> bool:
    """Eval one prereq item; non-locked courses are assumed satisfied."""
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        return (cid in avail) if (cid in locked_norm) else True
    if t == "exam":
        return True
    return _eval_locked_tree(item, avail, locked_norm)


def _eval_locked_tree(node: dict, avail: set[str], locked_norm: set[str]) -> bool:
    """AND/OR/NOT prereq tree eval; only locked courses matter for conflict detection."""
    for key in ("AND", "OR", "NOT"):
        if key not in node:
            continue
        items = node[key]
        if key == "AND":
            return all(_eval_locked_item(i, avail, locked_norm) for i in items)
        if key == "OR":
            return any(_eval_locked_item(i, avail, locked_norm) for i in items)
        if key == "NOT":
            return not any(_eval_locked_item(i, avail, locked_norm) for i in items)
    return True


def validate_locks(locked_courses: dict[str, str]) -> tuple[bool, list[str]]:
    """Check whether locked quarter assignments conflict with each other prereq-wise.

    Only intra-lock conflicts are reported: if a locked course A requires
    another locked course B to have appeared in an earlier quarter, but B
    is pinned to the same or a later quarter, that is a conflict.

    Non-locked courses (potentially completed or freely scheduled) are
    assumed satisfied — only the locking of B to the wrong position is flagged.
    """
    if len(locked_courses) < 2:
        return True, []

    client      = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    course_ids  = list(locked_courses.keys())
    trees       = _prereq_trees(client, course_ids)
    locked_norm = {_norm(c) for c in locked_courses}

    # Invert: norm_id → original quarter
    norm_to_quarter = {_norm(c): q for c, q in locked_courses.items()}

    conflicts: list[str] = []
    for course_id, quarter in sorted(locked_courses.items(), key=lambda x: _qkey(x[1])):
        tree = trees.get(_norm(course_id))
        if not tree:
            continue

        # available_locked = locked courses in strictly earlier quarters
        avail = {
            n for n, q in norm_to_quarter.items()
            if _qkey(q) < _qkey(quarter)
        }

        if not _eval_locked_tree(tree, avail, locked_norm):
            # Identify which locked course is causing the conflict
            blocking = [
                f"{c} (locked to {q})"
                for c, q in locked_courses.items()
                if c != course_id and _norm(c) in locked_norm
                and _qkey(locked_courses[c]) >= _qkey(quarter)
            ]
            blocker_str = ", ".join(blocking) if blocking else "a locked prerequisite"
            conflicts.append(
                f"{course_id} locked to {quarter}: {blocker_str} must be "
                f"placed in an earlier quarter"
            )

    return len(conflicts) == 0, conflicts


# ── What-if scheduling helpers ────────────────────────────────────────────────

def _asap_with_locks(
    remaining:       list[str],
    trees:           dict[str, dict],
    quarters:        list[str],
    units_per_quarter: int,
    completed_courses: list[str],
    locked_by_quarter: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[str]]:
    """ASAP scheduler that treats locked courses as pre-placed.

    Locked courses are added to `available` at the start of their quarter
    (same as the regular scheduler does within-quarter), giving unlocked
    courses access to them as prereqs in the same or later quarters.
    """
    max_per_q = units_per_quarter // UNITS_PER_COURSE
    available: set[str] = {_norm(c) for c in completed_courses}

    # Pre-fill plan with locked courses
    plan: dict[str, list[str]] = {
        q: list(locked_by_quarter.get(q, [])) for q in quarters
    }

    for quarter in quarters:
        # Make locked courses available at the start of their quarter
        for lc in plan[quarter]:
            available.add(_norm(lc))

        free = max_per_q - len(plan[quarter])
        placed = 0
        while placed < free and remaining:
            eligible = next(
                (
                    i for i, c in enumerate(remaining)
                    if not trees.get(_norm(c))
                    or _eval_tree(trees[_norm(c)], available)
                ),
                None,
            )
            if eligible is None:
                break
            course = remaining.pop(eligible)
            plan[quarter].append(course)
            available.add(_norm(course))
            placed += 1

    return plan, remaining  # remaining = overflow


def _perturb_unlocked(
    plan:        CoursePlan,
    locked_norm: set[str],
    rng:         random.Random,
    n_swaps:     int,
) -> CoursePlan:
    """Swap n_swaps random pairs of UNLOCKED courses between quarters."""
    p = deepcopy(plan)
    quarters = list(p.planned_courses.keys())
    for _ in range(n_swaps):
        unlocked_quarters = [
            q for q in quarters
            if any(_norm(c) not in locked_norm for c in p.planned_courses.get(q, []))
        ]
        if len(unlocked_quarters) < 2:
            break
        q1, q2 = rng.sample(unlocked_quarters, 2)
        free1 = [c for c in p.planned_courses[q1] if _norm(c) not in locked_norm]
        free2 = [c for c in p.planned_courses[q2] if _norm(c) not in locked_norm]
        if not free1 or not free2:
            continue
        c1, c2 = rng.choice(free1), rng.choice(free2)
        p.planned_courses[q1].remove(c1)
        p.planned_courses[q2].remove(c2)
        p.planned_courses[q1].append(c2)
        p.planned_courses[q2].append(c1)
    return p


def _whatif_optimize(
    plan:        CoursePlan,
    locked_norm: set[str],
    trees:       dict[str, dict],
    diff_scores: dict[str, float],
    meta:        dict[str, dict],
    max_iter:    int = 150,
    seed:        int | None = None,
) -> tuple[CoursePlan, float, dict[str, float]]:
    """Hill-climb on unlocked courses only; locked courses never move."""
    rng       = random.Random(seed)
    best      = deepcopy(plan)
    best_score, best_bd = _soft_score(plan, diff_scores, meta)
    quarters  = sorted(plan.planned_courses.keys(), key=_qkey)
    max_per_q = plan.units_per_quarter // UNITS_PER_COURSE

    # Violations that are already present before we start (don't penalise new moves for them)
    base_viols = set(_check_prereqs(plan, trees))

    for _ in range(max_iter):
        # Quarters with at least one unlocked course
        movable = [
            q for q in quarters
            if any(_norm(c) not in locked_norm for c in best.planned_courses.get(q, []))
        ]
        if not movable:
            break

        q_from   = rng.choice(movable)
        unlocked = [c for c in best.planned_courses[q_from] if _norm(c) not in locked_norm]
        if not unlocked:
            continue
        course   = rng.choice(unlocked)
        q_to     = rng.choice([q for q in quarters if q != q_from])

        # Unit cap — locked courses count against the cap in q_to
        if len(best.planned_courses.get(q_to, [])) >= max_per_q:
            continue

        candidate = deepcopy(best)
        candidate.planned_courses[q_from].remove(course)
        candidate.planned_courses[q_to] = candidate.planned_courses.get(q_to, []) + [course]

        # Only reject if the move ADDS new violations
        cand_viols = set(_check_prereqs(candidate, trees))
        if cand_viols - base_viols:
            continue

        cand_score, cand_bd = _soft_score(candidate, diff_scores, meta)
        if cand_score < best_score:
            best, best_score, best_bd = candidate, cand_score, cand_bd

    return best, best_score, best_bd


# ── Public API ────────────────────────────────────────────────────────────────

def run_whatif(
    plan:              CoursePlan,
    locked_courses:    dict[str, str],
    major_id:          str,
    graduation_quarter: str,
    units_per_quarter: int = 16,
    waived_ges:        list[str] | None = None,
) -> WhatIfResult:
    """Generate plan variants with locked_courses pinned to their quarters.

    Parameters
    ----------
    plan
        Existing CoursePlan.  completed_courses is used to seed prereq
        availability; planned_courses is ignored (regenerated from scratch).
    locked_courses
        {course_id: quarter_string} — courses the student has fixed.
    major_id, graduation_quarter, units_per_quarter, waived_ges
        Same semantics as plan_generator.generate().
    """
    if waived_ges is None:
        waived_ges = []

    # ── 1. Validate locks ─────────────────────────────────────────────────────
    lock_valid, lock_conflicts = validate_locks(locked_courses)
    if not lock_valid:
        return WhatIfResult(variants=[], lock_conflicts=lock_conflicts)

    client      = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()

    # ── 2. Quarter window ─────────────────────────────────────────────────────
    quarters = _generate_quarters(graduation_quarter)
    if not quarters:
        return WhatIfResult(variants=[], lock_conflicts=[])

    locked_norm    = {_norm(c) for c in locked_courses}
    completed_norm = {_norm(c) for c in plan.completed_courses}

    # ── 3. Collect remaining required courses (excluding locked + completed) ──
    effective_norm = completed_norm | locked_norm
    remaining, _   = _collect_courses(client, major_id, effective_norm, waived_ges)

    # ── 4. Feasibility ────────────────────────────────────────────────────────
    total      = len(locked_courses) + len(remaining)
    max_per_q  = units_per_quarter // UNITS_PER_COURSE
    q_avail    = len(quarters)
    q_needed   = math.ceil(total / max_per_q) if max_per_q else 0
    tight      = (q_avail - q_needed) <= 1

    # ── 5. Prereq trees + course meta (one round-trip each) ──────────────────
    all_ids = list(locked_courses.keys()) + remaining
    trees   = _fetch_prereq_trees(client, all_ids)
    meta    = _load_course_meta(client, all_ids)

    # ── 6. ASAP schedule unlocked courses around locked slots ─────────────────
    locked_by_quarter: dict[str, list[str]] = {}
    for c, q in locked_courses.items():
        locked_by_quarter.setdefault(q, []).append(c)

    plan_dict, overflow = _asap_with_locks(
        list(remaining), trees, quarters, units_per_quarter,
        plan.completed_courses, locked_by_quarter,
    )

    if overflow:
        print(f"  [whatif] Warning: {len(overflow)} course(s) could not fit "
              f"before {graduation_quarter} — omitted.")

    grad_year = int(graduation_quarter.split("_")[0])
    base = CoursePlan(
        major_id          = major_id,
        completed_courses = plan.completed_courses,
        planned_courses   = {q: cs for q, cs in plan_dict.items() if cs},
        graduation_year   = grad_year,
        units_per_quarter = units_per_quarter,
    )

    # ── 7. Generate 5 variants (perturb unlocked → hill-climb unlocked) ───────
    configs = [(42, 0), (13, 4), (7, 8), (99, 12), (17, 16)]
    raw: list[WhatIfVariant] = []
    base_viols = set(_check_prereqs(base, trees))

    for seed, n_swaps in configs:
        rng      = random.Random(seed)
        starting = _perturb_unlocked(base, locked_norm, rng, n_swaps) if n_swaps else base
        opt, score, bd = _whatif_optimize(
            starting, locked_norm, trees, diff_scores, meta, seed=seed
        )

        # All violations in the optimised plan (include pre-existing for transparency)
        opt_viols = list(set(_check_prereqs(opt, trees)))

        raw.append(WhatIfVariant(
            planned_courses = dict(opt.planned_courses),
            locked_courses  = locked_courses,
            soft_score      = score,
            soft_breakdown  = bd,
            violations      = opt_viols,
        ))

    raw.sort(key=lambda v: v.soft_score)
    return WhatIfResult(
        variants           = raw[:3],
        lock_conflicts     = [],
        quarters_available = q_avail,
        quarters_needed    = q_needed,
        tight_timeline     = tight,
        overflow_count     = len(overflow),
    )

