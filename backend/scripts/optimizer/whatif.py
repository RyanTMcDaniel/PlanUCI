"""
What-if planner: validate locked-course assignments and rebalance a plan
around them.

validate_locks(locked_courses) → (bool, list[str])
    Quick check that the locked quarter assignments don't conflict with
    each other prereq-wise.  Fires before the optimizer runs so the UI can
    give instant feedback when a student locks a course.

optimize_around_locks(plan, locked_course_ids) → {"status": ..., ...}
    The core editor operation: locked courses never move; only the unlocked
    courses already in the plan are repositioned to improve the soft score.
"""

import os
import random
import re
from copy import deepcopy

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import (
    CoursePlan,
    CheckResult,
    UNITS_PER_COURSE,
    CODE_PREREQ_ORDER,
    _eval_tree,
    _norm,
    _pretty_quarter,
    _qkey,
    _course_units_by_norm,
    coreq_split_pairs,
    quarter_units,
    unit_cap_tiers,
    units_valid,
)
from .optimizer import _check_prereqs, _prereq_trees, _soft_score
from .plan_generator import (
    _fetch_course_terms,
    _fetch_course_units,
    _fetch_prereq_trees,
    _resolve_ap_credits,
    _resolve_implicit_prereqs,
)
from .soft_constraints import _load_course_meta, _load_difficulty_scores

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)


# ── Lock validation ───────────────────────────────────────────────────────────

def _eval_locked_item(
    item: dict, avail: set[str], locked_norm: set[str], completed_norm: set[str],
    same_q_norm: set[str] | None = None,
) -> bool | None:
    """Eval one prereq item. Returns True/False for locked/completed courses; None for unknowns.

    - locked course in avail (prior quarter) → True (satisfied)
    - locked course is a coreq AND in same_q_norm (same quarter) → True (coreq satisfied)
    - locked course NOT in avail and not a valid coreq → False (definitely wrong)
    - completed course → True (satisfied)
    - unlocked/non-completed course or exam → None (unknown; caller decides)
    """
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        if cid in locked_norm:
            if cid in avail:
                return True
            # Corequisite: same-quarter placement is acceptable
            if item.get("coreq") and same_q_norm is not None and cid in same_q_norm:
                return True
            return False
        if cid in completed_norm:
            return True
        return None  # not locked, not completed — unknown
    if t == "exam":
        # If _resolve_ap_credits encoded a satisfaction token, treat as satisfied.
        exam_name = item.get("examName", "")
        try:
            min_grade = int(item.get("minGrade", "3"))
        except (ValueError, TypeError):
            min_grade = 3
        token = f"EXAMOK:{_norm(exam_name)}:{min_grade}"
        if token in completed_norm:
            return True
        return None  # exam not in completed_norm — can't verify
    # nested AND/OR/NOT subtree — always returns bool
    return _eval_locked_tree(item, avail, locked_norm, completed_norm, same_q_norm)


def _eval_locked_tree(
    node: dict, avail: set[str], locked_norm: set[str], completed_norm: set[str],
    same_q_norm: set[str] | None = None,
) -> bool:
    """AND/OR/NOT prereq tree eval — only flags conflicts caused by locked courses.

    Three-valued logic (True / False / None=unknown):
      AND — fails only if any item is definitely False; unknown items pass.
      OR  — passes if any item is True; fails only if some item is False and none are True;
            if all items are unknown, passes (can't judge without full history).
      NOT — fails only if an anti-coreq is explicitly locked to an earlier slot.
    """
    for key in ("AND", "OR", "NOT"):
        if key not in node:
            continue
        items = node[key]
        if key == "AND":
            results = [_eval_locked_item(i, avail, locked_norm, completed_norm, same_q_norm) for i in items]
            return not any(r is False for r in results)
        if key == "OR":
            results = [_eval_locked_item(i, avail, locked_norm, completed_norm, same_q_norm) for i in items]
            if any(r is True for r in results):
                return True
            if any(r is False for r in results):
                # A locked alternative is in the wrong position AND nothing else satisfies it
                return False
            return True  # all unknown → no data to flag a conflict
        if key == "NOT":
            return not any(
                i.get("prereqType") == "course"
                and _norm(i.get("courseId", "")) in locked_norm
                and _norm(i.get("courseId", "")) in avail
                for i in items
            )
    return True


def _offending_locked_norms(
    node: dict, avail: set[str], locked_norm: set[str], completed_norm: set[str],
    same_q_norm: set[str] | None = None,
) -> set[str]:
    """Normalized ids of the locked courses that actually cause a tree to fail.

    Mirrors _eval_locked_tree but collects the specific False leaves instead of a
    bare bool, so a conflict message can name the offending prereq(s) rather than
    dumping every locked course.
    """
    offenders: set[str] = set()
    for key in ("AND", "OR", "NOT"):
        if key not in node:
            continue
        items = node[key]
        if key == "AND":
            for i in items:
                if _eval_locked_item(i, avail, locked_norm, completed_norm, same_q_norm) is False:
                    offenders |= _offending_item_norms(i, avail, locked_norm, completed_norm, same_q_norm)
            return offenders
        if key == "OR":
            results = [_eval_locked_item(i, avail, locked_norm, completed_norm, same_q_norm) for i in items]
            if any(r is True for r in results):
                return set()  # an alternative is satisfied — no conflict
            for i, r in zip(items, results):
                if r is False:
                    offenders |= _offending_item_norms(i, avail, locked_norm, completed_norm, same_q_norm)
            return offenders
        if key == "NOT":
            for i in items:
                if (
                    i.get("prereqType") == "course"
                    and _norm(i.get("courseId", "")) in locked_norm
                    and _norm(i.get("courseId", "")) in avail
                ):
                    offenders.add(_norm(i.get("courseId", "")))
            return offenders
    return offenders


def _offending_item_norms(
    item: dict, avail: set[str], locked_norm: set[str], completed_norm: set[str],
    same_q_norm: set[str] | None = None,
) -> set[str]:
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        is_false = _eval_locked_item(item, avail, locked_norm, completed_norm, same_q_norm) is False
        return {cid} if is_false else set()
    if t == "exam":
        return set()
    return _offending_locked_norms(item, avail, locked_norm, completed_norm, same_q_norm)


def _missing_prereq_norms(
    tree: dict, locked_norm: set[str], completed_norm: set[str]
) -> set[str]:
    """Return normalized course IDs that are AND-required by tree but absent from the plan.

    Uses conservative logic: only flags courses that are definitively missing.
    - AND: all children must be satisfied; collect missing from each child.
    - OR: if any alternative is in locked_norm or completed_norm, nothing missing.
          Otherwise collect missing from the first course alternative.
    - NOT/exam: ignored.
    """
    all_norm = locked_norm | completed_norm
    return _missing_item_norms_set(tree, all_norm)


def _missing_item_norms_set(node: dict, all_norm: set[str]) -> set[str]:
    for key in ("AND", "OR", "NOT"):
        if key not in node:
            continue
        items = node[key]
        if key == "AND":
            result: set[str] = set()
            for item in items:
                result |= _missing_leaf_norms(item, all_norm)
            return result
        if key == "OR":
            for item in items:
                if item.get("prereqType") == "course" and _norm(item.get("courseId", "")) in all_norm:
                    return set()
                if item.get("prereqType") not in ("course", "exam", None):
                    if not _missing_item_norms_set(item, all_norm):
                        return set()
            for item in items:
                if item.get("prereqType") == "course":
                    cid = _norm(item.get("courseId", ""))
                    if cid not in all_norm:
                        return {cid}
            return set()
        if key == "NOT":
            return set()
    return set()


def _missing_leaf_norms(item: dict, all_norm: set[str]) -> set[str]:
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        return set() if cid in all_norm else {cid}
    if t == "exam":
        return set()
    return _missing_item_norms_set(item, all_norm)


def validate_locks(
    locked_courses: dict[str, str],
    completed_courses: list[str] | None = None,
    ap_scores: dict[str, int] | None = None,
) -> tuple[bool, list[str]]:
    """Check locked quarter assignments for prereq ordering conflicts and missing prereqs.

    Two types of conflicts are reported:
    1. Ordering: a locked course A requires another locked course B in an earlier
       quarter, but B is pinned to the same or later quarter.
    2. Missing: a course in the plan requires a prereq that is not in the plan at all.

    completed_courses and AP-credited courses are treated as already satisfied.
    """
    if not locked_courses:
        return True, []

    completed_norm = {_norm(c) for c in (completed_courses or [])}

    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    # Resolve AP credits — adds equivalencies to completed_norm in-place
    if ap_scores:
        _resolve_ap_credits(client, ap_scores, completed_norm)
    course_ids  = list(locked_courses.keys())
    trees       = _prereq_trees(client, course_ids)
    locked_norm = {_norm(c) for c in locked_courses}

    norm_to_quarter = {_norm(c): q for c, q in locked_courses.items()}
    norm_to_raw     = {_norm(c): c for c in locked_courses}

    conflicts: list[str] = []
    for course_id, quarter in sorted(locked_courses.items(), key=lambda x: _qkey(x[1])):
        tree = trees.get(_norm(course_id))
        if not tree:
            continue

        # 1. Ordering conflict: prereq is in plan but placed in wrong quarter
        # (only meaningful when there are at least 2 courses to compare)
        if len(locked_courses) >= 2:
            avail = {
                n for n, q in norm_to_quarter.items()
                if _qkey(q) < _qkey(quarter)
            }
            same_q_norm = {
                n for n, q in norm_to_quarter.items()
                if _qkey(q) == _qkey(quarter)
            }

            if not _eval_locked_tree(tree, avail, locked_norm, completed_norm, same_q_norm):
                # Name the SPECIFIC offending prereq(s), not the full lock list.
                offenders = _offending_locked_norms(
                    tree, avail, locked_norm, completed_norm, same_q_norm
                )
                blocking = [
                    f"{norm_to_raw.get(n, n)} (locked to {norm_to_quarter[n]})"
                    for n in sorted(offenders)
                    if n in norm_to_quarter
                ]
                blocker_str = ", ".join(blocking) if blocking else "a locked prerequisite"
                conflicts.append(
                    f"{course_id} locked to {quarter}: needs {blocker_str} "
                    f"in an earlier quarter"
                )

        # 2. Missing prereq: required course is not in the plan at all
        missing = _missing_prereq_norms(tree, locked_norm, completed_norm)
        for missing_norm in sorted(missing):
            conflicts.append(
                f"{course_id} missing prereq: {missing_norm}"
            )

    return len(conflicts) == 0, conflicts


# ── What-if scheduling helpers ────────────────────────────────────────────────

def _perturb_unlocked(
    plan:        CoursePlan,
    locked_norm: set[str],
    rng:         random.Random,
    n_swaps:     int,
    trees:       dict[str, dict] | None = None,
) -> CoursePlan:
    """Swap n_swaps random pairs of UNLOCKED courses between quarters.

    When `trees` is provided, a swap that introduces a NEW prereq violation or
    splits a corequisite pair (relative to the pre-swap state) is undone, so the
    optimizer never starts from a plan whose prereq ordering it has scrambled.
    """
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
        before_viols = set(_check_prereqs(p, trees)) if trees else set()
        before_split = coreq_split_pairs(p, trees) if trees else set()
        p.planned_courses[q1].remove(c1)
        p.planned_courses[q2].remove(c2)
        p.planned_courses[q1].append(c2)
        p.planned_courses[q2].append(c1)
        if trees and (
            (set(_check_prereqs(p, trees)) - before_viols)
            or (coreq_split_pairs(p, trees) - before_split)
        ):
            p.planned_courses[q1].remove(c2)
            p.planned_courses[q2].remove(c1)
            p.planned_courses[q1].append(c1)
            p.planned_courses[q2].append(c2)
    return p


def _whatif_optimize(
    plan:        CoursePlan,
    locked_norm: set[str],
    trees:       dict[str, dict],
    diff_scores: dict[str, float],
    meta:        dict[str, dict],
    max_iter:    int = 150,
    seed:        int | None = None,
    extra_available: set[str] | None = None,
    units_by_course: dict[str, int] | None = None,
) -> tuple[CoursePlan, float, dict[str, float]]:
    """Hill-climb on unlocked courses only; locked courses never move.

    The per-quarter unit cap (plan.units_per_quarter) is enforced against REAL
    course units when units_by_course is supplied (else the count proxy).
    """
    rng       = random.Random(seed)
    best      = deepcopy(plan)
    best_score, best_bd = _soft_score(plan, diff_scores, meta)
    quarters  = sorted(plan.planned_courses.keys(), key=_qkey)
    cap       = plan.units_per_quarter

    # Violations that are already present before we start (don't penalise new moves for them)
    base_viols = set(_check_prereqs(plan, trees, extra_available))
    # Coreq pairs already split in the input — moves may not add new ones.
    base_coreq_split = coreq_split_pairs(plan, trees)

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

        # Unit cap (real units) — locked courses count against the cap in q_to
        prospective = best.planned_courses.get(q_to, []) + [course]
        if quarter_units(prospective, units_by_course) > cap:
            continue

        candidate = deepcopy(best)
        candidate.planned_courses[q_from].remove(course)
        candidate.planned_courses[q_to] = candidate.planned_courses.get(q_to, []) + [course]

        # Only reject if the move ADDS new violations
        cand_viols = set(_check_prereqs(candidate, trees, extra_available))
        if cand_viols - base_viols:
            continue

        # Coreqs must share a quarter — reject moves that newly split a pair
        if coreq_split_pairs(candidate, trees) - base_coreq_split:
            continue

        cand_score, cand_bd = _soft_score(candidate, diff_scores, meta)
        if cand_score < best_score:
            best, best_score, best_bd = candidate, cand_score, cand_bd

    return best, best_score, best_bd


# ── Primary API: optimize around locked courses ───────────────────────────────

def _prettify_quarters(msg: str) -> str:
    """Rewrite '2027_fall' → 'Fall 2027' inside a conflict message."""
    return re.sub(
        r"(\d{4})_(winter|spring|summer|fall)",
        lambda m: f"{m.group(2).capitalize()} {m.group(1)}",
        msg,
    )


def optimize_around_locks(
    plan: CoursePlan,
    locked_course_ids: list[str],
    top_n: int = 3,
    seed_configs: list[tuple[int, int]] | None = None,
    ap_scores: dict[str, int] | None = None,
) -> dict:
    """Reposition only the UNLOCKED courses of `plan`; locked courses never move.

    This is the core editor operation: the course set is fixed input (everything
    already in plan.planned_courses).  Courses whose ids are in locked_course_ids
    stay exactly where the plan places them; every other course may be moved to
    improve the soft score, subject to all hard constraints (using the structured
    checks from hard_constraints).

    Returns one of:
        { "status": "ok", "plans": [ {planned_courses, locked_courses,
              soft_score, soft_breakdown, valid, violations}, ... ] }
        { "status": "infeasible", "conflicts": [ {reason, code}, ... ] }

    A locked set is infeasible when the pinned courses can't be satisfied — e.g.
    two pinned courses violate prerequisite order, or a pinned course's prereq
    can't be placed before it.  In that case the specific conflict reasons from
    the hard-constraint layer are returned instead of any plan.
    """
    locked_norm = {_norm(c) for c in locked_course_ids}

    # Map every course in the plan to its current quarter; collect ids.
    course_quarter: dict[str, str] = {}
    all_ids: list[str] = []
    for q, courses in plan.planned_courses.items():
        for c in courses:
            course_quarter[_norm(c)] = q
            all_ids.append(c)

    unlocked_ids = [c for c in all_ids if _norm(c) not in locked_norm]

    # locked_map: {raw_course_id: current_quarter} for locked courses present in plan.
    locked_map: dict[str, str] = {}
    for c in locked_course_ids:
        q = course_quarter.get(_norm(c))
        if q is not None:
            locked_map[c] = q

    client      = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()
    trees       = _prereq_trees(client, all_ids)
    meta        = _load_course_meta(client, all_ids)
    units_by_course = _course_units_by_norm(client, all_ids)

    # Resolve AP / completed credit into a normalized baseline of satisfied
    # tokens (course norms + "EXAMOK:" exam tokens).  These count as satisfied
    # prereqs everywhere below, so AP-credited courses that are NOT placed in
    # any quarter still read as satisfied (the reported bug).
    completed_norm = {_norm(c) for c in plan.completed_courses}
    if ap_scores:
        _resolve_ap_credits(client, ap_scores, completed_norm)

    # ── Stage A: is the locked set itself feasible? ───────────────────────────
    # Treat completed + unlocked courses as available so validate_locks flags
    # ONLY locked-vs-locked ordering conflicts (and prereqs absent from the whole
    # plan), not prereqs that happen to be unlocked-but-present.
    if len(locked_map) >= 2:
        extra_available = list(plan.completed_courses) + unlocked_ids
        lock_ok, lock_conflicts = validate_locks(
            locked_map, completed_courses=extra_available, ap_scores=ap_scores
        )
        if not lock_ok:
            return {
                "status": "infeasible",
                "conflicts": [
                    {"reason": _prettify_quarters(c), "code": CODE_PREREQ_ORDER}
                    for c in lock_conflicts
                ],
            }

    # ── Stage B: reposition unlocked courses, keep only hard-valid variants ────
    configs = seed_configs or [(42, 0), (13, 4), (7, 8), (99, 12), (17, 16)]

    candidates: list[tuple[CoursePlan, float, dict]] = []
    best_attempt: tuple[float, list[str], list] | None = None  # (score, prereq_viols, unit_checks)

    # Unit-cap escalation: try the requested cap, then 20, then 24.  A looser cap
    # is used only when no hard-valid arrangement exists at the tighter one, so a
    # plan that fits at 16 is never needlessly loosened.
    for cap in unit_cap_tiers(plan.units_per_quarter):
        base = deepcopy(plan)
        base.units_per_quarter = cap

        for s, n_swaps in configs:
            rng   = random.Random(s)
            start = _perturb_unlocked(base, locked_norm, rng, n_swaps, trees) if n_swaps else base
            opt, score, bd = _whatif_optimize(
                start, locked_norm, trees, diff_scores, meta, seed=s,
                extra_available=completed_norm, units_by_course=units_by_course,
            )

            # Hard-constraint gate using the structured checks (real units).
            prereq_viols = _check_prereqs(opt, trees, completed_norm)
            units_ok, unit_checks = units_valid(opt, units_by_course=units_by_course)

            if not prereq_viols and units_ok:
                candidates.append((opt, score, bd))
            if best_attempt is None or score < best_attempt[0]:
                best_attempt = (score, prereq_viols, unit_checks)

        if candidates:
            break  # got hard-valid plans at this cap — don't loosen further

    if not candidates:
        # No valid arrangement exists even at 24 units/quarter → infeasible.
        # Surface the specific conflicts from the closest attempt.
        _, prereq_viols, unit_checks = best_attempt
        conflicts: list[dict] = []
        seen: set[str] = set()
        for v in prereq_viols:
            r = _prettify_quarters(v)
            if r not in seen:
                seen.add(r)
                conflicts.append({"reason": r, "code": CODE_PREREQ_ORDER})
        for chk in unit_checks:
            if chk.reason not in seen:
                seen.add(chk.reason)
                conflicts.append({"reason": chk.reason, "code": chk.code})
        return {"status": "infeasible", "conflicts": conflicts}

    candidates.sort(key=lambda t: t[1])
    plans = [
        {
            "planned_courses": dict(opt.planned_courses),
            "locked_courses":  locked_map,
            "soft_score":      score,
            "soft_breakdown":  bd,
            "valid":           True,
            "violations":      [],
        }
        for opt, score, bd in candidates[:top_n]
    ]
    return {"status": "ok", "plans": plans}



# ── Propose-and-autoplace prerequisites when a course is added ────────────────

def _season_of(quarter: str) -> str:
    """'2026_fall' → 'Fall'."""
    return quarter.split("_", 1)[1].capitalize()


def _offered_in(course: str, quarter: str, terms_by_course: dict[str, list[str]]) -> bool:
    """True if `course` is historically offered in this quarter's season (or unknown)."""
    terms = terms_by_course.get(course) or []
    if not terms:
        return True
    qs = _season_of(quarter)
    return any(
        len(t.split(" ", 1)) == 2 and t.split(" ", 1)[1].strip().capitalize() == qs
        for t in terms
    )


def _asap_place_missing(
    missing:           list[str],
    trees:             dict[str, dict],
    window:            list[str],
    units_per_quarter: int,
    completed_norm:    set[str],
    locked_by_quarter: dict[str, list[str]],
    terms_by_course:   dict[str, list[str]],
    units_by_course:   dict[str, int],
) -> tuple[dict[str, str], list[str]]:
    """ASAP-place `missing` courses into `window`, treating existing courses as locked.

    Mirrors the ASAP-around-locks pattern, but additionally (a) enforces term availability
    and (b) requires each course's prerequisites to sit in a STRICTLY earlier
    quarter (so chains like ICS31→32→33 span sequential quarters).  Returns
    (placements {course_id: quarter}, overflow list).
    """
    def _units(c: str) -> int:
        return units_by_course.get(c) or units_by_course.get(_norm(c)) or UNITS_PER_COURSE

    window = sorted(window, key=_qkey)
    placements: dict[str, str] = {}
    remaining = list(missing)

    for q in window:
        # Availability = completed + everything (locked or already-proposed) in
        # STRICTLY earlier quarters.  Same-quarter placements don't satisfy prereqs.
        available: set[str] = set(completed_norm)
        for qq in window:
            if _qkey(qq) < _qkey(q):
                available.update(_norm(c) for c in locked_by_quarter.get(qq, []))
                available.update(_norm(c) for c, cq in placements.items() if cq == qq)

        used = sum(_units(c) for c in locked_by_quarter.get(q, []))
        used += sum(_units(c) for c, cq in placements.items() if cq == q)

        for c in list(remaining):
            tree = trees.get(_norm(c))
            if tree and not _eval_tree(tree, available):
                continue
            if not _offered_in(c, q, terms_by_course):
                continue
            u = _units(c)
            if used + u > units_per_quarter:
                continue
            placements[c] = q
            used += u
            remaining.remove(c)

    return placements, remaining


def propose_prereq_chain(plan: CoursePlan, course_id: str) -> dict:
    """Detect a course's missing prerequisite chain and PROPOSE where to place it.

    Returns (does NOT mutate the plan):
        {
          "missing": [course_ids in the prereq chain not already in plan/completed],
          "proposed_placements": [ {course_id, quarter, reason}, ... ],
          "status": "ok" | "infeasible",
          "conflicts": [ {reason, code}, ... ]   # only when infeasible
        }

    The chain is found with _resolve_implicit_prereqs.  Missing courses are
    ASAP-placed into quarters strictly BEFORE the dependent course, treating the
    existing plan as locked (the locked-placement machinery), respecting prereq
    order, unit caps and term availability.  If the chain can't be placed validly,
    status is "infeasible" with structured CheckResult reasons.
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    upq = plan.units_per_quarter

    completed_norm = {_norm(c) for c in plan.completed_courses}
    placed_by_quarter = {q: list(cs) for q, cs in plan.planned_courses.items()}
    planned_norm = {_norm(c) for cs in placed_by_quarter.values() for c in cs}
    existing_norm = planned_norm | completed_norm

    # 1. Find the prerequisite chain for course_id (reuse _resolve_implicit_prereqs).
    trees = _fetch_prereq_trees(client, [course_id])
    expanded, trees = _resolve_implicit_prereqs(
        [course_id], trees, existing_norm, client
    )
    missing = [
        c for c in expanded
        if _norm(c) != _norm(course_id) and _norm(c) not in existing_norm
    ]

    # 2. Prereqs already satisfied → nothing to propose.
    if not missing:
        return {"missing": [], "proposed_placements": [], "status": "ok", "conflicts": []}

    # 3. Deadline = the quarter course_id occupies (prereqs must land strictly before).
    deadline_q = next(
        (q for q, cs in placed_by_quarter.items()
         if any(_norm(c) == _norm(course_id) for c in cs)),
        None,
    )
    quarters = sorted(placed_by_quarter.keys(), key=_qkey)
    window = [
        q for q in quarters
        if deadline_q is None or _qkey(q) < _qkey(deadline_q)
    ]

    # 4. ASAP-place missing courses around the locked existing plan.
    terms_by_course = _fetch_course_terms(client, missing)
    units_by_course = _fetch_course_units(client, missing)
    placements, overflow = _asap_place_missing(
        missing, trees, window, upq, completed_norm,
        placed_by_quarter, terms_by_course, units_by_course,
    )

    dep_where = _pretty_quarter(deadline_q) if deadline_q else "its target quarter"

    # 5. Infeasible: some prereq couldn't be placed before the dependent course.
    if overflow:
        conflicts: list[dict] = []
        if not window:
            reason_tail = (
                f"there is no quarter before {course_id} ({dep_where}) to place it"
            )
        else:
            reason_tail = (
                f"no quarter before {course_id} ({dep_where}) has room / term "
                f"availability for it after its own prerequisites"
            )
        for m in overflow:
            conflicts.append(CheckResult(
                valid=False,
                reason=f"Can't place prerequisite {m} — {reason_tail}.",
                code=CODE_PREREQ_ORDER,
            ).as_dict())
        return {
            "missing": missing,
            "proposed_placements": [],
            "status": "infeasible",
            "conflicts": conflicts,
        }

    # 6. Safety net: validate the combined plan with the structured checks.
    combined = apply_prereq_chain(
        plan, [{"course_id": m, "quarter": q} for m, q in placements.items()]
    )
    ptrees = _prereq_trees(client, [
        c for cs in combined.planned_courses.values() for c in cs
    ])
    prereq_viols = _check_prereqs(combined, ptrees)
    units_ok, unit_checks = units_valid(combined)
    if prereq_viols or not units_ok:
        conflicts = [
            {"reason": _prettify_quarters(v), "code": CODE_PREREQ_ORDER}
            for v in prereq_viols
        ] + [{"reason": c.reason, "code": c.code} for c in unit_checks]
        return {
            "missing": missing,
            "proposed_placements": [],
            "status": "infeasible",
            "conflicts": conflicts,
        }

    proposed_placements = [
        {
            "course_id": m,
            "quarter":   q,
            "reason":    (f"Prerequisite of {course_id}; earliest valid quarter "
                          f"before {dep_where} after its own prerequisites."),
        }
        for m, q in sorted(placements.items(), key=lambda kv: _qkey(kv[1]))
    ]
    return {
        "missing": missing,
        "proposed_placements": proposed_placements,
        "status": "ok",
        "conflicts": [],
    }


def apply_prereq_chain(plan: CoursePlan, proposed_placements: list[dict]) -> CoursePlan:
    """Apply an accepted proposal, returning a NEW CoursePlan (original untouched).

    proposed_placements: [ {course_id, quarter, ...}, ... ] from propose_prereq_chain.
    Kept separate from propose_prereq_chain so detection and execution don't mix:
    the frontend shows the proposal, the user confirms, THEN this runs.
    """
    new_planned = {q: list(cs) for q, cs in plan.planned_courses.items()}
    for p in proposed_placements:
        q = p["quarter"]
        new_planned.setdefault(q, []).append(p["course_id"])
    return CoursePlan(
        major_id          = plan.major_id,
        completed_courses = list(plan.completed_courses),
        planned_courses   = new_planned,
        graduation_year   = plan.graduation_year,
        units_per_quarter = plan.units_per_quarter,
    )
