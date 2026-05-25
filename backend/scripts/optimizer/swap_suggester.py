"""
Swap and move suggestions for course plans.

suggest_swaps(plan, course_id, major_id, top_n=3) → list[SwapSuggestion]
    Finds unscheduled required courses that could replace course_id in its
    current quarter, ranked by soft-score improvement.

suggest_move(plan, course_id, top_n=3) → list[SwapSuggestion]
    Finds valid quarter destinations for course_id within the existing plan,
    ranked by soft-score improvement.

Both use pre-loaded prerequisite trees and course metadata so each candidate
evaluation is in-memory — only the initial data fetches hit Supabase.
"""

import os
from copy import deepcopy
from dataclasses import dataclass

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import CoursePlan, UNITS_PER_COURSE, _norm, _qkey
from .optimizer import _check_prereqs, _prereq_trees, _soft_score
from .soft_constraints import _load_course_meta, _load_difficulty_scores
from .plan_generator import _collect_courses
from .offering_patterns import is_likely_offered

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class SwapSuggestion:
    course_id: str          # replacement course (swaps) or moved course (move)
    current_quarter: str    # quarter where the original course currently sits
    proposed_quarter: str   # destination (= current_quarter for swaps, different for moves)
    score_before: float
    score_after: float
    score_delta: float      # score_before − score_after; positive = improvement
    reason: str


# ── Shared helpers ────────────────────────────────────────────────────────────

def _find_quarter(plan: CoursePlan, course_id: str) -> str | None:
    """Return the quarter key containing course_id, or None if not found."""
    norm_target = _norm(course_id)
    for q, courses in plan.planned_courses.items():
        if any(_norm(c) == norm_target for c in courses):
            return q
    return None


def _build_reason(delta: float, off_reason: str, extra: str = "") -> str:
    if delta > 0.005:
        base = f"Improves plan by {delta:.3f}"
    elif delta < -0.005:
        base = f"Worsens plan by {-delta:.3f}"
    else:
        base = "Comparable soft score"
    parts = [base]
    if off_reason:
        parts.append(off_reason)
    if extra:
        parts.append(extra)
    return "; ".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────

def suggest_swaps(
    plan: CoursePlan,
    course_id: str,
    major_id: str,
    top_n: int = 3,
) -> list[SwapSuggestion]:
    """Find unscheduled required courses that could replace course_id.

    The replacement goes into course_id's current quarter.  Candidates are
    drawn from the set of major-required courses not yet placed in the plan,
    then filtered by:
      1. Historical offering pattern for the target quarter.
      2. No new prerequisite violations introduced by the swap (pre-existing
         violations in the baseline plan are not counted against candidates).
      3. Unit cap (always met for a 1-for-1 swap).
    Sorted by soft-score delta descending (best improvement first).
    """
    client      = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()

    current_quarter = _find_quarter(plan, course_id)
    if current_quarter is None:
        return []

    norm_target = _norm(course_id)
    q_name      = current_quarter.split("_", 1)[1]

    # "effectively completed" = everything already placed except course_id,
    # so _collect_courses returns course_id's slot as still needing to be filled.
    completed_norm = {_norm(c) for c in plan.completed_courses}
    scheduled_norm = {
        _norm(c)
        for cs in plan.planned_courses.values()
        for c in cs
        if _norm(c) != norm_target
    }
    effective_norm = completed_norm | scheduled_norm

    # Unscheduled required courses (major + GE, respecting group sizes)
    raw_candidates, _ = _collect_courses(client, major_id, effective_norm, [])
    # Drop course_id itself (it's still "needed" because we excluded it above)
    candidates = [c for c in raw_candidates if _norm(c) != norm_target]
    if not candidates:
        return []

    # Filter by historical offering — cheap, in-memory
    offering_ok: list[tuple[str, str]] = []
    for c in candidates:
        likely, reason = is_likely_offered(c, q_name)
        if likely:
            offering_ok.append((c, reason))
    if not offering_ok:
        return []

    # One round-trip: load trees + meta for planned courses + all offering-ok candidates
    all_planned_ids = [c for cs in plan.planned_courses.values() for c in cs]
    candidate_ids   = [c for c, _ in offering_ok]
    all_ids         = all_planned_ids + candidate_ids
    trees = _prereq_trees(client, all_ids)
    meta  = _load_course_meta(client, all_ids)

    # Baseline violations — pre-existing issues we don't penalise candidates for
    baseline_viols = set(_check_prereqs(plan, trees))
    baseline_score, _ = _soft_score(plan, diff_scores, meta)
    max_per_q = plan.units_per_quarter // UNITS_PER_COURSE
    suggestions: list[SwapSuggestion] = []

    for candidate, off_reason in offering_ok:
        modified = deepcopy(plan)
        modified.planned_courses[current_quarter] = [
            c for c in modified.planned_courses[current_quarter]
            if _norm(c) != norm_target
        ] + [candidate]

        if len(modified.planned_courses[current_quarter]) > max_per_q:
            continue

        # Reject only if the swap ADDS new violations beyond the baseline
        swap_viols = set(_check_prereqs(modified, trees))
        if swap_viols - baseline_viols:
            continue

        new_score, _ = _soft_score(modified, diff_scores, meta)
        delta         = baseline_score - new_score

        suggestions.append(SwapSuggestion(
            course_id       = candidate,
            current_quarter = current_quarter,
            proposed_quarter= current_quarter,
            score_before    = baseline_score,
            score_after     = new_score,
            score_delta     = delta,
            reason          = _build_reason(delta, off_reason),
        ))

    suggestions.sort(key=lambda s: s.score_delta, reverse=True)
    return suggestions[:top_n]


def suggest_move(
    plan: CoursePlan,
    course_id: str,
    top_n: int = 3,
) -> list[SwapSuggestion]:
    """Find valid quarter destinations for course_id within the existing plan.

    Moves course_id out of its current quarter and tries every other quarter
    in the plan, filtering by:
      1. Unit cap (target quarter must have room).
      2. No new prerequisite violations introduced by the move.  Pre-existing
         violations in the baseline plan are not counted against destinations.
    Offering-pattern warnings are noted in reason but do not filter out the
    destination — moves are flagged, not blocked.
    Sorted by soft-score delta descending.
    """
    client      = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()

    current_quarter = _find_quarter(plan, course_id)
    if current_quarter is None:
        return []

    norm_target = _norm(course_id)

    all_planned_ids = [c for cs in plan.planned_courses.values() for c in cs]
    trees = _prereq_trees(client, all_planned_ids)
    meta  = _load_course_meta(client, all_planned_ids)

    # Baseline — violations already present before any move
    baseline_viols = set(_check_prereqs(plan, trees))
    baseline_score, _ = _soft_score(plan, diff_scores, meta)
    max_per_q = plan.units_per_quarter // UNITS_PER_COURSE
    quarters  = sorted(plan.planned_courses.keys(), key=_qkey)
    suggestions: list[SwapSuggestion] = []

    for target_q in quarters:
        if target_q == current_quarter:
            continue
        if len(plan.planned_courses.get(target_q, [])) >= max_per_q:
            continue  # target quarter already full

        q_name        = target_q.split("_", 1)[1]
        likely, off_r = is_likely_offered(course_id, q_name)
        warning       = f"Warning: {off_r}" if not likely else ""

        modified = deepcopy(plan)
        modified.planned_courses[current_quarter] = [
            c for c in modified.planned_courses[current_quarter]
            if _norm(c) != norm_target
        ]
        modified.planned_courses[target_q] = (
            modified.planned_courses.get(target_q, []) + [course_id]
        )

        # Reject only if the move ADDS new violations beyond the baseline
        move_viols = set(_check_prereqs(modified, trees))
        if move_viols - baseline_viols:
            continue

        new_score, _ = _soft_score(modified, diff_scores, meta)
        delta         = baseline_score - new_score

        suggestions.append(SwapSuggestion(
            course_id       = course_id,
            current_quarter = current_quarter,
            proposed_quarter= target_q,
            score_before    = baseline_score,
            score_after     = new_score,
            score_delta     = delta,
            reason          = _build_reason(delta, off_r if likely else "", warning),
        ))

    suggestions.sort(key=lambda s: s.score_delta, reverse=True)
    return suggestions[:top_n]


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import statistics
    from .plan_generator import generate, FeasibilityError
    from .soft_constraints import _load_difficulty_scores as _ld

    MAJOR_ID   = "BS-201G"
    FIRST_GRAD = "2028_spring"

    # ── Step 1: generate plan (mirror plan_generator smoke logic) ──────────────
    print("Generating CS plan...")
    try:
        gen_result = generate(major_id=MAJOR_ID, completed_courses=[], graduation_quarter=FIRST_GRAD)
        GRAD_Q = FIRST_GRAD
    except FeasibilityError as e:
        base_year = int(FIRST_GRAD.split("_")[0])
        GRAD_Q    = f"{base_year + e.years_to_extend}_spring"
        gen_result = generate(major_id=MAJOR_ID, completed_courses=[], graduation_quarter=GRAD_Q)

    plan = gen_result.variants[0].plan
    print(f"  Plan generated: grad={GRAD_Q}, "
          f"{sum(len(cs) for cs in plan.planned_courses.values())} courses across "
          f"{len(plan.planned_courses)} quarters")

    # ── Step 2: find highest-difficulty quarter ────────────────────────────────
    diff_scores = _ld()
    quarter_avgs: dict[str, float] = {}
    for q, courses in plan.planned_courses.items():
        vals = [diff_scores.get(_norm(c), 5.0) for c in courses if courses]
        if vals:
            quarter_avgs[q] = statistics.mean(vals)

    hardest_q = max(quarter_avgs, key=lambda q: quarter_avgs[q])
    hardest_course = max(
        plan.planned_courses[hardest_q],
        key=lambda c: diff_scores.get(_norm(c), 5.0),
    )
    hardest_difficulty = diff_scores.get(_norm(hardest_course), 5.0)

    print()
    print(f"Hardest quarter: {hardest_q}  (avg difficulty: {quarter_avgs[hardest_q]:.2f})")
    print(f"  Courses: {plan.planned_courses[hardest_q]}")
    print(f"Hardest course:  {hardest_course}  (difficulty: {hardest_difficulty:.2f})")

    # ── Step 3: suggest_swaps ─────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"suggest_swaps({hardest_course!r}, major={MAJOR_ID})")
    print("=" * 60)
    swaps = suggest_swaps(plan, hardest_course, MAJOR_ID)
    if not swaps:
        print("  No valid swap candidates found.")
    for i, s in enumerate(swaps, 1):
        sign = "+" if s.score_delta >= 0 else ""
        print(f"  {i}. {s.course_id}")
        print(f"     Slot:  {s.current_quarter}")
        print(f"     Score: {s.score_before:.4f} → {s.score_after:.4f}  "
              f"(delta={sign}{s.score_delta:.4f})")
        print(f"     {s.reason}")

    # ── Step 4: suggest_move ──────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"suggest_move({hardest_course!r})")
    print("=" * 60)
    moves = suggest_move(plan, hardest_course)
    if not moves:
        print("  No valid move destinations found.")
    for i, s in enumerate(moves, 1):
        sign = "+" if s.score_delta >= 0 else ""
        print(f"  {i}. Move to {s.proposed_quarter}")
        print(f"     Score: {s.score_before:.4f} → {s.score_after:.4f}  "
              f"(delta={sign}{s.score_delta:.4f})")
        print(f"     {s.reason}")
