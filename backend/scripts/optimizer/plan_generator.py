"""
Course plan generator combining hard and soft constraints.

generate(major_id, completed_courses, graduation_quarter, units_per_quarter=16,
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
import re  # FIX 1
from copy import deepcopy
from dataclasses import dataclass, field

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import (
    CoursePlan,
    UNITS_PER_COURSE,
    _eval_tree,
    _load_aliases,  # FIX 6
    _norm,
    _qkey,
)
from .optimizer import OptimizationResult, optimize, _check_prereqs  # _check_prereqs for prereq-safe _perturb
from .offering_patterns import is_likely_offered
from .soft_constraints import _load_difficulty_scores  # FIX 4

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

# Maximum quarters the dynamic scheduler is allowed to extend beyond the
# user's requested graduation_quarter.  16 = 4 years of UCI quarters.
MAX_QUARTERS = 16


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
    variants:           list[OptimizationResult]
    tight_timeline:     bool = False
    overflow_count:     int  = 0
    quarters_available: int  = 0
    quarters_needed:    int  = 0
    # requirement_group → list of course IDs selected from that group
    group_map:          dict[str, list[str]] = field(default_factory=dict)
    # Courses that could not be scheduled even after dynamic window extension.
    # Frontend should display these as "could not be scheduled within 4 years."
    overflow_courses:   list[str] = field(default_factory=list)
    # How many extra quarters were appended beyond graduation_quarter (0 = none needed).
    extended_by:        int = 0


# ── Quarter helpers ───────────────────────────────────────────────────────────

def _current_quarter() -> str:
    """Return the active UCI quarter as 'YYYY_quarter' (winter/spring/fall)."""
    today = datetime.date.today()
    m, y = today.month, today.year
    if m <= 3:
        return f"{y}_winter"
    if m <= 6:
        return f"{y}_spring"
    return f"{y}_fall"


def _generate_quarters(graduation_quarter: str) -> list[str]:
    """Quarters from the current quarter through graduation_quarter inclusive.

    Uses the standard UCI sequence (winter, spring, fall) and skips summer.
    graduation_quarter must be in 'YYYY_quarter' format, e.g. '2029_spring'.
    """
    seq = ["winter", "spring", "fall"]
    current    = _current_quarter()
    start_year = int(current.split("_")[0])
    start_q    = current.split("_")[1]
    grad_q     = graduation_quarter.split("_")[1]

    if grad_q not in seq:
        raise ValueError(
            f"graduation_quarter must end in winter/spring/fall, got {graduation_quarter!r}"
        )

    try:
        idx = seq.index(start_q)
    except ValueError:
        idx = 2

    quarters: list[str] = []
    year = start_year
    grad_year = int(graduation_quarter.split("_")[0])

    while True:
        q    = seq[idx]
        qstr = f"{year}_{q}"
        quarters.append(qstr)
        if qstr == graduation_quarter:
            break
        idx = (idx + 1) % len(seq)
        if idx == 0:
            year += 1
        if year > grad_year + 1:   # safety: stop if we've overshot
            break

    return quarters


def _next_quarter(q: str) -> str:
    """Return the UCI quarter immediately after q.

    Sequence: winter → spring → fall → (winter of next year).
    """
    seq = ["winter", "spring", "fall"]
    year, season = q.rsplit("_", 1)
    idx = seq.index(season)
    next_idx = (idx + 1) % len(seq)
    next_year = int(year) + (1 if next_idx == 0 else 0)
    return f"{next_year}_{seq[next_idx]}"


# ── Course unit helper ────────────────────────────────────────────────────────

def _fetch_course_units(client, course_ids: list[str]) -> dict[str, int]:
    """Return {course_id: min_units} from the courses table.

    Falls back to UNITS_PER_COURSE for any course whose min_units is NULL.
    Used by _asap_schedule so that real unit totals (not assumed 4 per course)
    are checked against the per-quarter unit cap.
    """
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,min_units")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {
        r["id"]: max(1, int(r["min_units"]))
        for r in rows
        if r.get("min_units") is not None
    }


# ── Course term helper (FIX 5) ───────────────────────────────────────────────

def _fetch_course_terms(client, course_ids: list[str]) -> dict[str, list[str]]:
    """Return {course_id: [term_strings]} for each course, e.g. ['2026 Fall', '2025 Winter']."""
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,terms")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {r["id"]: (r.get("terms") or []) for r in rows}


# ── Prereq depth helper (FIX 4) ──────────────────────────────────────────────

def _get_prereq_depths(client, norm_ids: list[str]) -> dict[str, int]:
    """Count direct prereq edges per course as a proxy for scheduling complexity."""
    if not norm_ids:
        return {}
    rows = (
        client.table("prereq_edges")
        .select("course_id")
        .in_("course_id", norm_ids)
        .execute()
        .data
    )
    counts: dict[str, int] = {}
    for row in rows:
        n = _norm(row["course_id"])
        counts[n] = counts.get(n, 0) + 1
    return counts


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
        .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", major_id)
        .execute()
        .data
    )

    # FIX 1: Specialization parent merge — if major_id ends with a letter suffix
    # (e.g. "BS-075A") also load the parent ("BS-075") and merge, deduplicating
    # by requirement_group + group_name so the spec's own rows take precedence.
    if re.search(r"[A-Z]$", major_id):
        parent_id = major_id[:-1]
        parent_rows = (
            client.table("major_requirements")
            .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
            .eq("major_id", parent_id)
            .execute()
            .data
        )
        existing_keys: set[str] = {
            (r.get("requirement_group") or "") + "|" + (r.get("group_name") or "")
            for r in major_rows
        }
        before_count = len(major_rows)
        for row in parent_rows:
            key = (row.get("requirement_group") or "") + "|" + (row.get("group_name") or "")
            if key not in existing_keys:
                major_rows.append(row)
                existing_keys.add(key)
        print(
            f"[FIX 1] Spec {major_id!r}: {before_count} spec rows + "
            f"{len(parent_rows)} parent ({parent_id!r}) rows → "
            f"{len(major_rows)} total after dedup"
        )
    else:
        print(f"[FIX 1] {major_id!r}: {len(major_rows)} rows (no parent merge needed)")

    ge_rows = (
        client.table("major_requirements")
        .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", "ALL_MAJORS")
        .execute()
        .data
    )

    selected: list[str] = []
    seen: set[str] = set()
    group_map: dict[str, list[str]] = {}

    # FIX 4: load difficulty scores once for elective sorting
    diff_scores = _load_difficulty_scores()

    def _pick(req: dict, use_plan_norm: set[str] | None = None) -> None:
        """Process one requirement row, appending chosen courses to selected/seen/group_map.

        use_plan_norm: when set, already_satisfied counts courses from this set
        (major-row snapshot) plus completed, not the live `seen` cursor.  This
        prevents GE rows from under-counting when alias mismatches hide coverage
        from major rows.  Candidate filtering still uses `seen` to avoid re-picks.
        """
        req_group = req.get("requirement_group") or ""
        waivable  = req.get("waivable", False)

        if waivable and req_group in waived_ges:
            return

        course_list: list[str] = req.get("courses") or []
        needed: int = req.get("courses_needed") or len(course_list)

        if use_plan_norm is not None:
            # FIX 2a: GE rows — count satisfaction using the live `seen` set
            # (which includes major rows AND previously processed GE rows), so that
            # a GE course selected by an earlier GE row isn't re-counted here.
            # `use_plan_norm` is kept for the 2b overlap detection print but not
            # used for the count because plan_courses_norm would miss GE-to-GE picks.
            ge_pool_norm = {_norm(c) for c in course_list}
            already_satisfied = len(ge_pool_norm & (completed_norm | seen))
            still_needed = max(0, needed - already_satisfied)
        else:
            already_have = sum(
                1 for c in course_list
                if _norm(c) in completed_norm or _norm(c) in seen
            )
            still_needed = max(0, needed - already_have)

        if still_needed == 0:
            return

        candidates = [
            c for c in course_list
            if _norm(c) not in completed_norm and _norm(c) not in seen
        ]

        req_type_str = (req.get("requirement_type") or "required").lower()
        if "elective" in req_type_str:
            # FIX 4: elective rows — sort by difficulty ASC, prereq_depth ASC, alpha.
            # Simpler courses (lower difficulty, fewer upstream prereqs) slot earlier.
            prereq_counts = _get_prereq_depths(client, [_norm(c) for c in candidates])
            candidates.sort(key=lambda c: (
                diff_scores.get(_norm(c), 5.0),
                prereq_counts.get(_norm(c), 0),
                c,
            ))
        else:
            # Prefer I&CSCI variants so prereq trees (which use "I&C SCI" notation)
            # resolve correctly via _norm.
            candidates.sort(
                key=lambda c: (0 if "I&CSCI" in c or "ICS" in _norm(c)[:3] else 1, c)
            )

        chosen = candidates[:still_needed]
        for course in chosen:
            selected.append(course)
            seen.add(_norm(course))
        if chosen:
            # FIX 2: accumulate instead of overwriting so multiple rows for the
            # same requirement_group all appear in the map.
            group_map.setdefault(req_group, []).extend(chosen)

    # Process major rows first (required + elective + major-specific GE)
    for req in major_rows:
        _pick(req)

    # FIX 2: snapshot after major rows so GE rows can see what is already covered
    plan_courses_norm = set(seen)

    # FIX 2b: identify required rows whose pool overlaps >50% with any GE pool
    all_ge_norms: set[str] = set()
    for ge_req in ge_rows:
        all_ge_norms.update(_norm(c) for c in (ge_req.get("courses") or []))

    for req in major_rows:
        if (req.get("requirement_type") or "").lower() == "required":
            pool = {_norm(c) for c in (req.get("courses") or [])}
            if pool:
                overlap = len(pool & all_ge_norms) / len(pool)
                if overlap > 0.5:
                    print(
                        f"[FIX 2b] Required row {req.get('group_name')!r} treated as "
                        f"GE-satisfying ({overlap:.0%} overlap with GE pools)"
                    )

    # Print GE course counts before GE processing (for test visibility)
    print(f"[FIX 2] Before GE rows: plan has {len(selected)} major courses")

    # Process GE rows using the major-row snapshot for already_satisfied
    for req in ge_rows:
        _pick(req, use_plan_norm=plan_courses_norm)

    # FIX 2: Print GE group counts after processing
    print(f"[FIX 2] GE group counts after processing ({major_id!r}):")
    for ge_req in ge_rows:
        rg = ge_req.get("requirement_group") or ""
        if rg in group_map:
            entries = group_map[rg]
            print(f"  {rg}: {len(entries)} course(s) → {entries}")

    return selected, group_map


# ── Feasibility check ─────────────────────────────────────────────────────────

def _check_feasibility(
    total_courses: int,
    quarters: list[str],
    units_per_quarter: int,
    graduation_quarter: str,
) -> tuple[bool, int, int]:
    """Return (tight_timeline, quarters_available, quarters_needed).

    Raises FeasibilityError only when even the 4-year (MAX_QUARTERS) extension
    cannot mathematically fit all courses.  Window-too-short cases where
    quarters_needed <= MAX_QUARTERS are handled by the dynamic extension loop in
    generate() — no error is raised for those.

    tight_timeline is True when fewer than 2 quarters of slack remain between
    the user's requested graduation_quarter and the pure-capacity minimum.
    """
    max_per_q          = units_per_quarter // UNITS_PER_COURSE
    quarters_available = len(quarters)
    quarters_needed    = math.ceil(total_courses / max_per_q) if max_per_q else 0

    if quarters_needed > MAX_QUARTERS:
        # Truly infeasible: even 4 years of quarters can't hold this many courses.
        excess_q            = quarters_needed - MAX_QUARTERS
        courses_to_complete = excess_q * max_per_q
        years_to_extend     = math.ceil(excess_q / 3)
        raise FeasibilityError(
            f"Cannot fit {total_courses} course(s) in even {MAX_QUARTERS} quarters "
            f"(4-year maximum). "
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


def _required_norms_from_tree(tree: dict, all_norm: set[str]) -> set[str]:
    """Return course norms that are AND-required by tree but absent from all_norm.

    AND: collect missing from every child.
    OR:  if any alternative is already in all_norm, nothing is required.
         Otherwise, collect missing from the first course alternative.
    NOT/exam: ignored.
    """
    for key in ("AND", "OR", "NOT"):
        if key not in tree:
            continue
        items = tree[key]
        if key == "AND":
            result: set[str] = set()
            for item in items:
                result |= _required_norms_from_item(item, all_norm)
            return result
        if key == "OR":
            for item in items:
                if item.get("prereqType") == "course" and _norm(item.get("courseId", "")) in all_norm:
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


def _required_norms_from_item(item: dict, all_norm: set[str]) -> set[str]:
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        return set() if cid in all_norm else {cid}
    if t == "exam":
        return set()
    return _required_norms_from_tree(item, all_norm)


def _resolve_implicit_prereqs(
    courses_to_plan: list[str],
    trees: dict[str, dict],
    completed_norm: set[str],
    client,
) -> tuple[list[str], dict[str, dict]]:
    """Inject prerequisite courses that are required but missing from courses_to_plan.

    Iterates until stable. For each course with a prereq tree, finds AND-required
    prerequisites that are not in the plan or completed, then fetches and adds them.
    This handles cases where a requirement group only picks 1 course from a sequence
    (e.g. ICS31) but downstream courses need the full chain (ICS32 → ICS33).
    """
    courses_set = list(courses_to_plan)
    all_norm = {_norm(c) for c in courses_set} | completed_norm

    changed = True
    while changed:
        changed = False
        missing_norms: set[str] = set()
        for norm_id, tree in trees.items():
            missing_norms |= _required_norms_from_tree(tree, all_norm)

        if not missing_norms:
            break

        # Convert norm IDs back to DB-style IDs to look up.
        # Prereq trees use "I&C SCI 33" → norm "ICS33"; DB stores "I&CSCI33".
        lookup_ids = []
        for n in missing_norms:
            lookup_ids.append(n)
            if n.startswith("ICS"):
                lookup_ids.append(n.replace("ICS", "I&CSCI", 1))

        rows = (
            client.table("courses")
            .select("id,prerequisite_tree")
            .in_("id", lookup_ids)
            .execute()
            .data
        )
        for row in rows:
            raw_id = row["id"]
            n = _norm(raw_id)
            if n not in all_norm:
                courses_set.append(raw_id)
                all_norm.add(n)
                if row.get("prerequisite_tree"):
                    trees[n] = row["prerequisite_tree"]
                changed = True

    return courses_set, trees


# ── ASAP scheduler ────────────────────────────────────────────────────────────

def _asap_schedule(
    courses: list[str],
    prereq_trees: dict[str, dict],
    quarters: list[str],
    units_per_quarter: int,
    completed_norm: set[str],
    terms_by_course: dict[str, list[str]] | None = None,
    units_by_course: dict[str, int] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Schedule each course in the earliest quarter its prerequisites allow.

    Returns (plan_dict, overflow) where overflow contains courses that could
    not be placed within the graduation window.

    Placement respects two constraints per quarter:
    - Term availability (FIX 5): course must be historically offered that season.
    - Unit cap: cumulative units of placed courses must not exceed units_per_quarter.
      Actual min_units from the courses table are used when units_by_course is
      provided; otherwise UNITS_PER_COURSE (4) is assumed per course.
    """
    available: set[str] = set(completed_norm)
    remaining: list[str] = list(courses)
    plan: dict[str, list[str]] = {q: [] for q in quarters}

    def _course_units(course: str) -> int:
        """Actual units for a course, falling back to the global assumed constant."""
        if units_by_course:
            return (units_by_course.get(course)
                    or units_by_course.get(_norm(course))
                    or UNITS_PER_COURSE)
        return UNITS_PER_COURSE

    def _offered_in_quarter(course: str, quarter: str) -> bool:
        """Return True if course is offered in this quarter's season type."""
        if not terms_by_course:
            return True
        terms = terms_by_course.get(course) or []
        if not terms:
            return True  # empty → offered all quarters
        q_season = quarter.split("_", 1)[1].capitalize()  # "2026_fall" → "Fall"
        return any(
            len(t.split(" ", 1)) == 2
            and t.split(" ", 1)[1].strip().capitalize() == q_season
            for t in terms
        )

    for quarter in quarters:
        while remaining:
            q_units = sum(_course_units(c) for c in plan[quarter])
            if q_units >= units_per_quarter:
                break  # quarter is full by unit budget

            # Find the first eligible course that fits in the remaining budget.
            # `available` holds only PRIOR quarters' courses so the schedule is
            # consistent with _check_prereqs validation (prereq must be in an
            # earlier quarter, not the same one).
            eligible_idx = next(
                (
                    i for i, c in enumerate(remaining)
                    if (
                        not prereq_trees.get(_norm(c))
                        or _eval_tree(prereq_trees[_norm(c)], available)
                    )
                    and _offered_in_quarter(c, quarter)
                    and q_units + _course_units(c) <= units_per_quarter
                ),
                None,
            )
            if eligible_idx is None:
                break  # nothing eligible or fits this quarter; advance to next

            course = remaining.pop(eligible_idx)
            plan[quarter].append(course)

        # Make this quarter's courses available to all subsequent quarters.
        available.update(_norm(c) for c in plan[quarter])

    return plan, remaining  # remaining = overflow


# ── Perturbation ──────────────────────────────────────────────────────────────

def _perturb(
    plan: CoursePlan,
    rng: random.Random,
    n_swaps: int,
    trees: dict[str, dict] | None = None,
) -> CoursePlan:
    """Swap n_swaps random course pairs between quarters.

    When `trees` is provided, each swap is validated against the prereq trees and
    immediately undone if it would introduce a prereq violation.  This prevents
    the optimizer from receiving a pre-broken starting plan that it would
    immediately return unchanged (the early-exit path in optimize()).
    """
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
        # Reject swap if it introduces any prereq violation
        if trees and _check_prereqs(p, trees):
            p.planned_courses[q1].remove(c2)
            p.planned_courses[q2].remove(c1)
            p.planned_courses[q1].append(c1)
            p.planned_courses[q2].append(c2)
    return p


# ── Elective overflow swap (Step 3) ─────────────────────────────────────────

def _try_swap_elective_overflow(
    client,
    major_id: str,
    overflow: list[str],
    courses_to_plan: list[str],
    trees: dict[str, dict],
    quarters: list[str],
    units_per_quarter: int,
    completed_norm: set[str],
    course_terms: dict[str, list[str]],
    diff_scores: dict[str, float],
    units_by_course: dict[str, int] | None = None,
) -> tuple[list[str], list[str]]:
    """For overflow courses that come from elective rows, try swapping them with
    simpler (lower difficulty + prereq depth) alternatives from the same pool.

    Returns (updated_courses_to_plan, remaining_overflow).
    Any required overflow courses are left untouched.
    """
    # Re-query major rows (including parent merge for specialisations)
    major_rows = (
        client.table("major_requirements")
        .select("requirement_group,group_name,requirement_type,courses,courses_needed")
        .eq("major_id", major_id)
        .execute()
        .data
    )
    if re.search(r"[A-Z]$", major_id):
        parent_id = major_id[:-1]
        parent_rows = (
            client.table("major_requirements")
            .select("requirement_group,group_name,requirement_type,courses,courses_needed")
            .eq("major_id", parent_id)
            .execute()
            .data
        )
        existing_keys = {
            (r.get("requirement_group") or "") + "|" + (r.get("group_name") or "")
            for r in major_rows
        }
        for row in parent_rows:
            key = (row.get("requirement_group") or "") + "|" + (row.get("group_name") or "")
            if key not in existing_keys:
                major_rows.append(row)

    # Build norm_id → (requirement_type, full_pool) for fast lookup
    req_info: dict[str, tuple[str, list[str]]] = {}
    for row in major_rows:
        rtype = (row.get("requirement_type") or "required").lower()
        for c in (row.get("courses") or []):
            req_info.setdefault(_norm(c), (rtype, row.get("courses") or []))

    current_plan_norms = {_norm(c) for c in courses_to_plan}
    updated = list(courses_to_plan)
    remaining = list(overflow)

    for oc in list(overflow):
        nc = _norm(oc)
        info = req_info.get(nc)
        if not info:
            continue
        rtype, pool = info
        if "elective" not in rtype:
            continue  # required overflow — can't swap

        # Find alternatives not yet in the plan
        alternatives = [
            c for c in pool
            if _norm(c) not in current_plan_norms
            and _norm(c) not in completed_norm
            and _norm(c) != nc
        ]
        if not alternatives:
            continue

        # Sort: lowest difficulty, fewest prereq edges, alpha tiebreak
        alt_norms = [_norm(a) for a in alternatives]
        alt_depths = _get_prereq_depths(client, alt_norms)
        alternatives.sort(key=lambda c: (
            diff_scores.get(_norm(c), 5.0),
            alt_depths.get(_norm(c), 0),
            c,
        ))

        # Try up to 3 candidates; accept the first that removes the overflow
        for alt in alternatives[:3]:
            trial = [c for c in updated if _norm(c) != nc]
            trial.append(alt)
            if alt not in course_terms:
                course_terms.update(_fetch_course_terms(client, [alt]))
            _, trial_overflow = _asap_schedule(
                trial, trees, quarters, units_per_quarter,
                completed_norm, terms_by_course=course_terms,
                units_by_course=units_by_course,
            )
            if oc not in trial_overflow and alt not in trial_overflow:
                print(f"  [Step 3] Swapped elective overflow {oc!r} → {alt!r}")
                updated = trial
                current_plan_norms.discard(nc)
                current_plan_norms.add(_norm(alt))
                remaining.remove(oc)
                break

    return updated, remaining


# ── Public API ────────────────────────────────────────────────────────────────

def generate(
    major_id: str,
    completed_courses: list[str],
    graduation_quarter: str,
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
    _load_aliases(client)  # FIX 6: build alias map before any _norm() calls
    completed_norm = {_norm(c) for c in completed_courses}

    # 1. Collect courses still needed (major + GE)
    courses_to_plan, group_map = _collect_courses(
        client, major_id, completed_norm, waived_ges
    )
    if not courses_to_plan:
        return GenerationResult(variants=[])

    # 2. Quarters available before graduation
    quarters = _generate_quarters(graduation_quarter)
    if not quarters:
        return GenerationResult(variants=[])

    # 3. Feasibility check — raises FeasibilityError if impossible
    tight, q_available, q_needed = _check_feasibility(
        len(courses_to_plan), quarters, units_per_quarter, graduation_quarter
    )

    # 4. Load prereq trees, inject implicit prereqs, re-check feasibility, then schedule
    trees = _fetch_prereq_trees(client, courses_to_plan)
    courses_to_plan, trees = _resolve_implicit_prereqs(
        courses_to_plan, trees, completed_norm, client
    )
    # Re-check feasibility after injection — may have added required prereq courses
    tight, q_available, q_needed = _check_feasibility(
        len(courses_to_plan), quarters, units_per_quarter, graduation_quarter
    )
    # Fetch term availability and actual unit counts before scheduling
    course_terms = _fetch_course_terms(client, courses_to_plan)
    course_units = _fetch_course_units(client, courses_to_plan)

    # Dynamic window extension — retry ASAP adding 1 quarter at a time if
    # overflow exists, up to MAX_QUARTERS total.  The stricter per-quarter prereq
    # enforcement means chains like ICS31→32→33 must span sequential quarters.
    working_quarters = list(quarters)
    plan_dict, overflow = _asap_schedule(
        courses_to_plan, trees, working_quarters, units_per_quarter,
        completed_norm, terms_by_course=course_terms, units_by_course=course_units,
    )
    extended_by = 0

    while overflow and len(working_quarters) < MAX_QUARTERS:
        working_quarters.append(_next_quarter(working_quarters[-1]))
        extended_by += 1
        plan_dict, overflow = _asap_schedule(
            courses_to_plan, trees, working_quarters, units_per_quarter,
            completed_norm, terms_by_course=course_terms, units_by_course=course_units,
        )

    if extended_by:
        print(
            f"  Extended window by {extended_by} quarter(s) beyond "
            f"{graduation_quarter} to fit all courses."
        )

    # For any remaining overflow, try swapping elective courses with simpler
    # alternatives from the same pool.
    if overflow:
        diff_scores = _load_difficulty_scores()
        courses_to_plan, overflow = _try_swap_elective_overflow(
            client, major_id, overflow, courses_to_plan, trees,
            working_quarters, units_per_quarter, completed_norm,
            course_terms, diff_scores, units_by_course=course_units,
        )
        if overflow:
            print(
                f"  Warning: {len(overflow)} course(s) could not be scheduled "
                f"within {MAX_QUARTERS} quarters — flagged as unschedulable: {overflow}"
            )

    # Final ASAP run with the (possibly swap-updated) courses_to_plan
    if overflow:
        plan_dict, overflow = _asap_schedule(
            courses_to_plan, trees, working_quarters, units_per_quarter,
            completed_norm, terms_by_course=course_terms, units_by_course=course_units,
        )

    # graduation_year: use the last quarter in the working window
    grad_year = int(working_quarters[-1].split("_")[0])
    base_plan = CoursePlan(
        major_id=major_id,
        completed_courses=completed_courses,
        planned_courses={q: cs for q, cs in plan_dict.items() if cs},
        graduation_year=grad_year,
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
        starting = _perturb(base_plan, rng, n_swaps, trees=trees) if n_swaps else base_plan
        result = optimize(starting, max_iter=150, seed=seed)
        variants.append(result)

    variants.sort(key=lambda r: r.soft_score)
    best_variants = variants[:3]

    # Annotate each variant with offering-pattern warnings (soft, not hard).
    # A course scheduled in a quarter it historically isn't offered gets a
    # warning appended to violations — plan is not invalidated.
    for variant in best_variants:
        for quarter, courses in variant.plan.planned_courses.items():
            q_name = quarter.split("_", 1)[1]  # "2026_fall" → "fall"
            for course in courses:
                likely, reason = is_likely_offered(course, q_name)
                if not likely:
                    variant.violations.append(
                        f"[offering] {course} in {quarter}: {reason}"
                    )

    return GenerationResult(
        variants=best_variants,
        tight_timeline=tight,
        overflow_count=len(overflow),
        quarters_available=len(working_quarters),
        quarters_needed=q_needed,
        group_map=group_map,
        overflow_courses=list(overflow),
        extended_by=extended_by,
    )

