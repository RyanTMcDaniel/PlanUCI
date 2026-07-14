"""
Course plan optimizer combining hard and soft constraints.

evaluate(plan) → OptimizationResult
    Scores a plan against all hard and soft constraints in a single pass.

optimize(plan) → OptimizationResult
    Improves the soft score via hill-climbing while keeping hard constraints
    satisfied.  Prereq trees and course metadata are pre-loaded once so all
    candidate checks run in-memory without extra Supabase round-trips.

Usage:
    result = evaluate(plan)
    result = optimize(plan, max_iter=200, seed=42)
"""

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
    _course_units_by_norm,
    coreq_split_pairs,
    major_requirements_met,
    no_duplicate_courses,
    quarter_units,
    units_valid,
    validate,
)
from .soft_constraints import (
    WEIGHTS,
    _load_course_meta,
    _load_difficulty_scores,
    adjacent_smoothing,
    difficulty_balance,
    ge_distribution,
    lower_div_earliness,
    major_clustering,
    min_units_load,
    over_cap_penalty,
    workload_progression,
)

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class OptimizationResult:
    plan: CoursePlan
    valid: bool
    violations: list[str]
    soft_score: float
    soft_breakdown: dict[str, float]
    iterations_run: int = 0
    improved: bool = False


# ── Shared helpers ────────────────────────────────────────────────────────────

def _prereq_trees(client, course_ids: list[str]) -> dict[str, dict]:
    """Fetch prerequisite_tree for each course id; return {norm_id: tree}."""
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


def _check_prereqs(
    plan: CoursePlan, trees: dict[str, dict], extra_available: set[str] | None = None
) -> list[str]:
    """Prereq check using pre-loaded trees — no Supabase calls.

    extra_available holds already-normalized satisfied tokens (AP-credited course
    norms and ``EXAMOK:`` exam tokens) that count as satisfied before any quarter.
    """
    violations: list[str] = []
    sorted_quarters = sorted(plan.planned_courses.keys(), key=_qkey)
    available: set[str] = {_norm(c) for c in plan.completed_courses}
    if extra_available:
        available |= extra_available

    for quarter in sorted_quarters:
        same_q = frozenset(_norm(c) for c in plan.planned_courses[quarter])
        for course in plan.planned_courses[quarter]:
            tree = trees.get(_norm(course))
            if tree and not _eval_tree(tree, available, same_q):
                violations.append(
                    f"{course} in {quarter}: prerequisites not satisfied"
                )
        available.update(_norm(c) for c in plan.planned_courses[quarter])

    return violations


def _soft_score(
    plan: CoursePlan,
    diff_scores: dict[str, float],
    meta: dict[str, dict],
    locked_norm: frozenset[str] = frozenset(),
) -> tuple[float, dict[str, float]]:
    """Soft score using pre-loaded data — no Supabase calls.

    locked_norm: normalized ids of locked courses, excluded from the earliness
    preferences (they can't move, so they'd only add a constant offset).
    """
    breakdown = {
        "difficulty_balance":   difficulty_balance(plan, diff_scores),
        "ge_distribution":      ge_distribution(plan, meta),
        "workload_progression": workload_progression(plan, diff_scores),
        "major_clustering":     major_clustering(plan, meta),
        "adjacent_smoothing":   adjacent_smoothing(plan, diff_scores),
        "min_units_load":       min_units_load(plan, meta),
        "over_cap_penalty":     over_cap_penalty(plan, plan.units_per_quarter, meta),
        "lower_div_earliness":  lower_div_earliness(plan, locked_norm),
    }
    return sum(WEIGHTS[k] * v for k, v in breakdown.items()), breakdown


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate(plan: CoursePlan) -> OptimizationResult:
    """Run all hard and soft constraints against plan and return a full report."""
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()
    all_ids = [c for courses in plan.planned_courses.values() for c in courses]
    meta = _load_course_meta(client, all_ids)

    valid, checks = validate(plan)
    # validate() now returns structured CheckResults; OptimizationResult.violations
    # is the JSON-facing string list, so surface each check's human-readable reason.
    violations = [c.reason for c in checks]
    soft_score, breakdown = _soft_score(plan, diff_scores, meta)

    return OptimizationResult(
        plan=plan,
        valid=valid,
        violations=violations,
        soft_score=soft_score,
        soft_breakdown=breakdown,
    )


def optimize(
    plan: CoursePlan,
    max_iter: int = 200,
    seed: int | None = None,
    ge_norms: set[str] | None = None,
    ge_allowed_quarters: set[str] | None = None,
) -> OptimizationResult:
    """Improve soft score via hill-climbing while keeping hard constraints satisfied.

    On each iteration a random course is moved to a random other quarter.  The
    move is kept only if it (1) stays within the unit cap, (2) leaves all
    prerequisite chains satisfied, (3) does not push a GE course past the Year-2
    deadline, and (4) lowers the soft penalty score.

    major_requirements_met and no_duplicate_courses are invariant under simple
    moves (the course set doesn't change) so they are checked once upfront.

    Hard GE earliness (optimizer layer): ``ge_norms`` is the set of
    normalized GE course ids; ``ge_allowed_quarters`` is the set of quarter strings
    (Year 1-2) in which a GE may legally sit.  A GE that is ALREADY past the
    deadline in the starting plan (prereq-blocked) is exempt — it can't be pulled
    earlier anyway — but a GE that starts within Y1-Y2 may never be moved out of
    the allowed window.  When ``ge_allowed_quarters`` is None the guard is off
    (preserves the old behaviour for callers that don't pass it).
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()
    all_ids = [c for courses in plan.planned_courses.values() for c in courses]
    meta = _load_course_meta(client, all_ids)
    trees = _prereq_trees(client, all_ids)
    units_by_course = _course_units_by_norm(client, all_ids)

    # Full validation for reporting purposes (structured → reason strings)
    hard_valid, checks = validate(plan)
    violations = [c.reason for c in checks]
    init_score, init_breakdown = _soft_score(plan, diff_scores, meta)

    # major_requirements_met and no_duplicate_courses are invariant under moves
    # (the set of courses never changes), so we only block optimization when
    # prereqs or the unit cap are actually violated.
    blocking = bool(_check_prereqs(plan, trees)) or not units_valid(plan, units_by_course=units_by_course)[0]
    if blocking:
        return OptimizationResult(
            plan=plan,
            valid=False,
            violations=violations,
            soft_score=init_score,
            soft_breakdown=init_breakdown,
        )

    rng = random.Random(seed)
    best = deepcopy(plan)
    best_score, best_breakdown = init_score, init_breakdown
    quarters = sorted(plan.planned_courses.keys(), key=_qkey)
    improved = False

    # Hard GE earliness guard.  Pre-compute the GE courses that are
    # already past the Year-2 deadline in the starting plan; those are exempt
    # (prereq-blocked).  Every other GE must stay inside ge_allowed_quarters.
    ge_norms = ge_norms or set()
    base_late_ge: set[str] = set()
    if ge_norms and ge_allowed_quarters is not None:
        for q, cs in plan.planned_courses.items():
            if q not in ge_allowed_quarters:
                for c in cs:
                    if _norm(c) in ge_norms:
                        base_late_ge.add(_norm(c))

    # Coreqs must stay in the same quarter.  Reject any move that introduces a
    # split not already present in the starting plan (the seed is coreq-valid, so
    # in practice this freezes coreq pairs together).  base-aware so a pre-split
    # input is never made worse.
    base_coreq_split = coreq_split_pairs(plan, trees)

    for _ in range(max_iter):
        non_empty = [q for q in quarters if best.planned_courses.get(q)]
        if not non_empty:
            break

        q_from = rng.choice(non_empty)
        course = rng.choice(best.planned_courses[q_from])
        q_to = rng.choice([q for q in quarters if q != q_from])

        # Hard GE earliness deadline: never move a (non-exempt) GE course out of
        # the Year-1-2 window.  Checked before building the candidate so the
        # hill-climber can't relocate a pure-elective GE into Year 3/4.
        if (
            ge_allowed_quarters is not None
            and _norm(course) in ge_norms
            and _norm(course) not in base_late_ge
            and q_to not in ge_allowed_quarters
        ):
            continue

        candidate = deepcopy(best)
        candidate.planned_courses[q_from].remove(course)
        candidate.planned_courses[q_to].append(course)

        # Unit cap (real per-course units)
        if quarter_units(candidate.planned_courses[q_to], units_by_course) > candidate.units_per_quarter:
            continue

        # Prereqs (in-memory, no Supabase)
        if _check_prereqs(candidate, trees):
            continue

        # Coreqs must share a quarter — reject moves that split a pair
        if coreq_split_pairs(candidate, trees) - base_coreq_split:
            continue

        # Soft score
        cand_score, cand_breakdown = _soft_score(candidate, diff_scores, meta)
        if cand_score < best_score:
            best, best_score, best_breakdown = candidate, cand_score, cand_breakdown
            improved = True

    return OptimizationResult(
        plan=best,
        valid=True,
        violations=[],
        soft_score=best_score,
        soft_breakdown=best_breakdown,
        iterations_run=max_iter,
        improved=improved,
    )

