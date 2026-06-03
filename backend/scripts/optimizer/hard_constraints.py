"""
Hard constraint validators for CoursePlan objects.

Usage:
    plan = CoursePlan(major_id="BS-201A", ...)
    passed, violations = validate(plan)
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

# Winter/Spring/Summer/Fall order within a calendar year
QUARTER_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
UNITS_PER_COURSE = 4  # assumed when per-course unit data is unavailable


def _qkey(qstr: str) -> tuple[int, int]:
    """'2024_fall' → (2024, 3) for chronological sorting."""
    year, quarter = qstr.rsplit("_", 1)
    return (int(year), QUARTER_ORDER.get(quarter.lower(), 99))


def _pretty_quarter(qstr: str) -> str:
    """'2026_fall' → 'Fall 2026' for human-readable reason strings."""
    try:
        year, quarter = qstr.rsplit("_", 1)
        return f"{quarter.capitalize()} {year}"
    except ValueError:
        return qstr


# ── Structured check result ───────────────────────────────────────────────────
# Constraint checks return one of these instead of a bare bool so the UI can tell
# the user *why* a placement is invalid.  `reason` is a human-readable,
# course-specific message; `code` is a short machine tag.  `reason` is None only
# when `valid` is True.

@dataclass
class CheckResult:
    valid: bool
    reason: str | None
    code: str

    def as_dict(self) -> dict:
        return {"valid": self.valid, "reason": self.reason, "code": self.code}


# Machine tags for each hard constraint.
CODE_PREREQ_ORDER  = "PREREQ_ORDER"
CODE_UNIT_CAP      = "UNIT_CAP"
CODE_DUPLICATE     = "DUPLICATE"
CODE_REQ_UNCOVERED = "REQ_UNCOVERED"


# ── CSE ↔ ICS alias map (FIX 6) ─────────────────────────────────────────────
# CSE was the old department prefix; current catalog uses I&CSCI / ICS after norm.
# Hardcoded known aliases; _load_aliases() extends this from the DB at runtime.
_ALIASES: dict[str, str] = {
    "CSE31":  "ICS31",
    "CSE43":  "ICS43",
    "CSE45C": "ICS45C",
    "CSE46":  "ICS46",
}
_ALIASES_INITIALIZED = False


def _load_aliases(client) -> None:
    """FIX 6: query major_requirements to discover additional CSE↔ICS aliases."""
    global _ALIASES, _ALIASES_INITIALIZED
    if _ALIASES_INITIALIZED:
        return
    try:
        rows = (
            client.table("major_requirements")
            .select("courses")
            .execute()
            .data
        )
    except Exception:
        _ALIASES_INITIALIZED = True
        return

    for row in rows:
        courses = row.get("courses") or []
        raw_norms = [c.replace(" ", "").upper().replace("I&CSCI", "ICS") for c in courses]
        cse_nums = {n[3:] for n in raw_norms if n.startswith("CSE")}
        ics_nums = {n[3:] for n in raw_norms if n.startswith("ICS")}
        for num in cse_nums & ics_nums:
            _ALIASES.setdefault("CSE" + num, "ICS" + num)

    _ALIASES_INITIALIZED = True
    print(f"[FIX 6] Alias map ({len(_ALIASES)} entries): {dict(sorted(_ALIASES.items()))}")


def _norm(course_id: str) -> str:
    """Normalize course ID for comparison: remove spaces, uppercase, resolve UCI dept aliases."""
    s = course_id.replace(" ", "").upper()
    # prereq_edges stores raw catalog text like "I&C SCI 46"; courses table uses "ICS46"
    s = s.replace("I&CSCI", "ICS")
    # FIX 6: resolve CSE→ICS aliases so CSE46 == ICS46 in all comparisons
    return _ALIASES.get(s, s)


@dataclass
class CoursePlan:
    major_id: str
    completed_courses: list[str] = field(default_factory=list)
    planned_courses: dict[str, list[str]] = field(default_factory=dict)
    graduation_year: int = 2028
    units_per_quarter: int = 16


# ── Prerequisite tree evaluation ──────────────────────────────────────────────

def _eval_item(
    item: dict,
    available: set[str],
    same_quarter: frozenset[str] = frozenset(),
) -> bool:
    prereq_type = item.get("prereqType")
    if prereq_type == "course":
        cid = _norm(item.get("courseId", ""))
        if item.get("coreq"):
            # A corequisite is satisfied if the course has been completed in any
            # prior quarter OR is being taken concurrently (same quarter).
            return cid in available or cid in same_quarter
        return cid in available
    if prereq_type == "exam":
        # Check whether _resolve_ap_credits encoded a satisfaction token in
        # available.  Token: "EXAMOK:<normed_exam_name>:<score_threshold>"
        exam_name = item.get("examName", "")
        try:
            min_grade = int(item.get("minGrade", "3"))
        except (ValueError, TypeError):
            min_grade = 3
        return f"EXAMOK:{_norm(exam_name)}:{min_grade}" in available
    return _eval_tree(item, available, same_quarter)


def _eval_tree(
    node: dict,
    available: set[str],
    same_quarter: frozenset[str] = frozenset(),
) -> bool:
    """Recursively evaluate an AND/OR/NOT prerequisite_tree node.

    same_quarter: normed IDs of courses placed in the current quarter.
    Coreq leaves are satisfied when their course is in same_quarter OR available.
    Callers that don't care about coreqs (e.g. optimizer inner loop) can omit
    same_quarter and rely on the default empty frozenset.
    """
    for logic_key in ("AND", "OR", "NOT"):
        if logic_key not in node:
            continue
        items = node[logic_key]
        if logic_key == "AND":
            return all(_eval_item(i, available, same_quarter) for i in items)
        if logic_key == "OR":
            return any(_eval_item(i, available, same_quarter) for i in items)
        if logic_key == "NOT":
            # Anti-prereqs: only fail if the forbidden course is in prior quarters.
            # Exams and nested subtrees inside NOT are ignored.
            return not any(
                i.get("prereqType") == "course"
                and _norm(i.get("courseId", "")) in available
                for i in items
            )
    return True  # empty node — no prereqs


# ── Corequisite same-quarter helper ───────────────────────────────────────────

def _collect_coreq_norms(tree: dict | None) -> set[str]:
    """Normed IDs of AND-linked corequisite course leaves in a prereq tree.

    Only AND-linked coreqs are returned — an OR branch means the coreq is one of
    several alternatives, so it is not forced.
    """
    if not tree:
        return set()
    result: set[str] = set()
    if "AND" in tree:
        for item in tree["AND"]:
            if item.get("prereqType") == "course" and item.get("coreq"):
                result.add(_norm(item.get("courseId", "")))
            elif "AND" in item or "OR" in item:
                result |= _collect_coreq_norms(item)
    return result


def coreq_split_pairs(
    plan: CoursePlan, trees: dict[str, dict]
) -> set[frozenset[str]]:
    """Return AND-coreq pairs whose two courses are placed in DIFFERENT quarters.

    Corequisites are bidirectional in reality but stored one-directionally in the
    data — sometimes the lecture row holds the coreq edge (MATH105A→105LA),
    sometimes the lab row does (MATH105LB→105B).  We treat the relationship
    symmetrically: a pair is "split" whenever both courses sit in the plan but in
    different quarters.  Pairs whose partner is absent from the plan (e.g. already
    satisfied by completed credit) are ignored — only co-scheduled coreqs are
    constrained to share a quarter.
    """
    quarter_of: dict[str, str] = {}
    for q, courses in plan.planned_courses.items():
        for c in courses:
            quarter_of[_norm(c)] = q

    split: set[frozenset[str]] = set()
    for c_norm, q in quarter_of.items():
        for partner in _collect_coreq_norms(trees.get(c_norm)):
            if partner in quarter_of and quarter_of[partner] != q:
                split.add(frozenset((c_norm, partner)))
    return split


def _representative_unmet(
    node: dict,
    available: set[str],
    same_quarter: frozenset[str] = frozenset(),
) -> str | None:
    """Return one normalized course id that is an unmet prerequisite of `node`.

    Best-effort, for building a human-readable reason only — the authoritative
    pass/fail decision is made by _eval_tree.  Walks AND children for the first
    unsatisfied leaf; for an unsatisfied OR, returns the first course alternative.
    """
    for logic_key in ("AND", "OR", "NOT"):
        if logic_key not in node:
            continue
        items = node[logic_key]
        if logic_key == "AND":
            for item in items:
                if not _eval_item(item, available, same_quarter):
                    leaf = _representative_unmet_leaf(item, available, same_quarter)
                    if leaf:
                        return leaf
            return None
        if logic_key == "OR":
            if any(_eval_item(i, available, same_quarter) for i in items):
                return None
            for item in items:
                leaf = _representative_unmet_leaf(item, available, same_quarter)
                if leaf:
                    return leaf
            return None
        if logic_key == "NOT":
            return None
    return None


def _representative_unmet_leaf(
    item: dict, available: set[str], same_quarter: frozenset[str]
) -> str | None:
    if item.get("prereqType") == "course":
        return _norm(item.get("courseId", ""))
    if item.get("prereqType") == "exam":
        return None
    return _representative_unmet(item, available, same_quarter)


def _where_scheduled(norm_id: str, plan: "CoursePlan") -> str | None:
    """Pretty-quarter where `norm_id` is planned, or None if absent from the plan."""
    for quarter, courses in plan.planned_courses.items():
        if any(_norm(c) == norm_id for c in courses):
            return _pretty_quarter(quarter)
    return None


def _probe_group_column(client) -> str | None:
    """Return the first grouping column found in prereq_edges, or None."""
    for col in ("group_id", "alternative_group"):
        try:
            client.table("prereq_edges").select(f"course_id,{col}").limit(1).execute()
            return col
        except Exception:
            pass
    return None


# ── Validators ───────────────────────────────────────────────────────────────

def prereqs_satisfied(plan: CoursePlan, client) -> tuple[bool, list[CheckResult]]:
    # FIX 6 — ensure alias map is populated before any _norm() comparisons
    _load_aliases(client)
    """Every planned course must have its prerequisites completed or planned earlier.

    OR logic is handled by evaluating the prerequisite_tree JSON from the
    courses table.  If prereq_edges has a group_id / alternative_group column
    we use that instead (rows in the same group are treated as OR alternatives).

    Returns (all_passed, violations) where each violation is a CheckResult with
    code PREREQ_ORDER and a course-specific reason.
    """
    violations: list[CheckResult] = []

    all_planned_ids = list({c for courses in plan.planned_courses.values() for c in courses})
    if not all_planned_ids:
        return True, []

    group_col = _probe_group_column(client)

    if group_col:
        # ── Group-column path: same group_col value → OR; different → AND ──
        rows = (
            client.table("prereq_edges")
            .select(f"course_id,prereq_course_id,{group_col}")
            .in_("course_id", all_planned_ids)
            .execute()
            .data
        )
        # prereq_groups: norm_course_id → {group_key → [norm_prereq_ids]}
        prereq_groups: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            cid = _norm(row["course_id"])
            pid = _norm(row["prereq_course_id"])
            gkey = row.get(group_col) or "default"
            prereq_groups.setdefault(cid, {}).setdefault(gkey, []).append(pid)

        sorted_quarters = sorted(plan.planned_courses.keys(), key=_qkey)
        available: set[str] = {_norm(c) for c in plan.completed_courses}

        for quarter in sorted_quarters:
            for course in plan.planned_courses[quarter]:
                nc = _norm(course)
                for gkey, prereqs in prereq_groups.get(nc, {}).items():
                    if not any(p in available for p in prereqs):
                        sample = prereqs[:3]
                        ellipsis = "…" if len(prereqs) > 3 else ""
                        violations.append(CheckResult(
                            valid=False,
                            reason=(
                                f"Can't place {course} in {_pretty_quarter(quarter)} — "
                                f"needs one of {sample}{ellipsis} (OR group {gkey!r}) "
                                f"scheduled earlier; none is."
                            ),
                            code=CODE_PREREQ_ORDER,
                        ))
            available.update(_norm(c) for c in plan.planned_courses[quarter])

    else:
        # ── prerequisite_tree fallback: full AND/OR/NOT evaluation ──
        course_rows = (
            client.table("courses")
            .select("id,prerequisite_tree")
            .in_("id", all_planned_ids)
            .execute()
            .data
        )
        prereq_trees: dict[str, dict] = {
            _norm(row["id"]): row.get("prerequisite_tree")
            for row in course_rows
            if row.get("prerequisite_tree")
        }

        sorted_quarters = sorted(plan.planned_courses.keys(), key=_qkey)
        available = {_norm(c) for c in plan.completed_courses}

        for quarter in sorted_quarters:
            # Pass same-quarter courses so coreq leaves are satisfied correctly.
            same_q = frozenset(_norm(c) for c in plan.planned_courses[quarter])
            for course in plan.planned_courses[quarter]:
                nc = _norm(course)
                tree = prereq_trees.get(nc)
                if tree and not _eval_tree(tree, available, same_q):
                    unmet = _representative_unmet(tree, available, same_q)
                    if unmet:
                        where = _where_scheduled(unmet, plan)
                        whenc = (f"isn't scheduled until {where}" if where
                                 else "isn't in your plan")
                        detail = f"prerequisite {unmet} {whenc}"
                    else:
                        detail = "its prerequisites aren't satisfied yet"
                    violations.append(CheckResult(
                        valid=False,
                        reason=(
                            f"Can't place {course} in {_pretty_quarter(quarter)} — "
                            f"{detail}."
                        ),
                        code=CODE_PREREQ_ORDER,
                    ))
            available.update(_norm(c) for c in plan.planned_courses[quarter])

    return len(violations) == 0, violations


def major_requirements_met(plan: CoursePlan, client) -> tuple[bool, list[CheckResult]]:
    """Required, elective, and GE requirement rows must be covered by the plan.

    Returns (all_passed, violations) where each violation is a CheckResult with
    code REQ_UNCOVERED.
    """
    _load_aliases(client)  # FIX 6
    violations: list[CheckResult] = []

    all_courses = {_norm(c) for c in plan.completed_courses}
    for courses_in_q in plan.planned_courses.values():
        all_courses.update(_norm(c) for c in courses_in_q)

    # Check major-specific requirements (API rows, then merged with catalogue rows)
    rows = (
        client.table("major_requirements")
        .select("requirement_type,courses,courses_needed,group_name")
        .eq("major_id", plan.major_id)
        .execute()
        .data
    )

    # Merge catalogue_requirements: fetch core rows (specialization_id IS NULL)
    # and let them override API rows with the same group_name.
    try:
        cat_rows = (
            client.table("catalogue_requirements")
            .select("requirement_type,courses,courses_needed,group_name")
            .eq("major_id", plan.major_id)
            .is_("specialization_id", "null")
            .execute()
            .data
        ) or []
        if cat_rows:
            cat_names = {r["group_name"] for r in cat_rows}
            rows = [r for r in rows if r.get("group_name") not in cat_names]
            rows.extend(cat_rows)
    except Exception:
        pass  # catalogue table missing or query failed — fall back to API rows only

    if not rows:
        print(f"[major_requirements_met] No requirements found for "
              f"major {plan.major_id!r} — skipping")
        return True, []

    for req in rows:
        course_list: list[str] = req.get("courses") or []
        needed: int = req.get("courses_needed") or 1
        req_type: str = req.get("requirement_type", "required")
        group_name: str = req.get("group_name") or "unnamed group"

        if not course_list:
            continue

        covered = sum(1 for c in course_list if _norm(c) in all_courses)
        if covered < needed:
            short = course_list[:4]
            ellipsis = "…" if len(course_list) > 4 else ""
            violations.append(CheckResult(
                valid=False,
                reason=(
                    f"Requirement {group_name!r} ({req_type}) needs {needed} of "
                    f"{short}{ellipsis} — only {covered} scheduled."
                ),
                code=CODE_REQ_UNCOVERED,
            ))

    # FIX 3: Also validate university-wide GE requirements (major_id = "ALL_MAJORS").
    # A plan fails if any GE group has fewer courses than courses_needed.
    ge_rows = (
        client.table("major_requirements")
        .select("requirement_type,courses,courses_needed,group_name")
        .eq("major_id", "ALL_MAJORS")
        .execute()
        .data
    )

    for req in ge_rows:
        course_list = req.get("courses") or []
        needed = req.get("courses_needed") or 1
        group_name = req.get("group_name") or "unnamed GE group"

        if not course_list:
            continue

        covered = sum(1 for c in course_list if _norm(c) in all_courses)
        if covered < needed:
            short = course_list[:4]
            ellipsis = "…" if len(course_list) > 4 else ""
            violations.append(CheckResult(
                valid=False,
                reason=(
                    f"GE requirement {group_name!r} needs {needed} of "
                    f"{short}{ellipsis} — only {covered} scheduled."
                ),
                code=CODE_REQ_UNCOVERED,
            ))

    return len(violations) == 0, violations


def units_valid(plan: CoursePlan, client=None) -> tuple[bool, list[CheckResult]]:
    """No quarter may exceed units_per_quarter.

    Returns (all_passed, violations) where each violation is a CheckResult with
    code UNIT_CAP.
    """
    violations: list[CheckResult] = []

    for quarter, courses in plan.planned_courses.items():
        total = len(courses) * UNITS_PER_COURSE
        if total > plan.units_per_quarter:
            violations.append(CheckResult(
                valid=False,
                reason=(
                    f"{_pretty_quarter(quarter)} would reach {total} units "
                    f"({len(courses)} courses) — cap is {plan.units_per_quarter}."
                ),
                code=CODE_UNIT_CAP,
            ))

    return len(violations) == 0, violations


def no_duplicate_courses(plan: CoursePlan, client=None) -> tuple[bool, list[CheckResult]]:
    """No course may appear in completed_courses and planned_courses, or twice in planned.

    Returns (all_passed, violations) where each violation is a CheckResult with
    code DUPLICATE.
    """
    violations: list[CheckResult] = []
    completed_set = {_norm(c) for c in plan.completed_courses}
    seen: set[str] = set()

    for quarter, courses in plan.planned_courses.items():
        for course in courses:
            nc = _norm(course)
            if nc in completed_set:
                violations.append(CheckResult(
                    valid=False,
                    reason=(f"{course} in {_pretty_quarter(quarter)} is already "
                            f"in your completed courses."),
                    code=CODE_DUPLICATE,
                ))
            if nc in seen:
                violations.append(CheckResult(
                    valid=False,
                    reason=(f"{course} appears more than once in the plan "
                            f"(again in {_pretty_quarter(quarter)})."),
                    code=CODE_DUPLICATE,
                ))
            seen.add(nc)

    return len(violations) == 0, violations


# ── Combined validator ────────────────────────────────────────────────────────

def validate(plan: CoursePlan) -> tuple[bool, list[CheckResult]]:
    """Run all hard constraints. Returns (all_passed, all_violations).

    all_violations is a list of CheckResult (each with reason + code); empty
    when the plan is fully valid.
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    _load_aliases(client)  # FIX 6

    checks = [
        ("prereqs_satisfied",      prereqs_satisfied(plan, client)),
        ("major_requirements_met", major_requirements_met(plan, client)),
        ("units_valid",            units_valid(plan)),
        ("no_duplicate_courses",   no_duplicate_courses(plan)),
    ]

    all_passed = True
    all_violations: list[CheckResult] = []

    for name, (passed, violations) in checks:
        if not passed:
            all_passed = False
        all_violations.extend(violations)

    return all_passed, all_violations


# ── Move-level checks + public entry point ────────────────────────────────────
# These check the placement of ONE course into ONE quarter and return a single
# CheckResult.  They are the primitives an interactive editor / API layer calls
# on every drag-and-drop action.

def _simulate_move(plan: CoursePlan, course: str, target_quarter: str) -> CoursePlan:
    """Return a deep-ish copy of `plan` with `course` placed in `target_quarter`.

    The course is removed from any quarter it currently occupies first, so this
    models a *move* (not an add) when the course is already scheduled.
    """
    nc = _norm(course)
    new_planned: dict[str, list[str]] = {}
    for q, courses in plan.planned_courses.items():
        new_planned[q] = [c for c in courses if _norm(c) != nc]
    new_planned.setdefault(target_quarter, [])
    new_planned[target_quarter].append(course)
    return CoursePlan(
        major_id          = plan.major_id,
        completed_courses = list(plan.completed_courses),
        planned_courses   = new_planned,
        graduation_year   = plan.graduation_year,
        units_per_quarter = plan.units_per_quarter,
    )


def check_duplicate(plan: CoursePlan, course: str, target_quarter: str) -> CheckResult:
    """Reject placing a course that's already completed or already in the plan."""
    nc = _norm(course)
    if nc in {_norm(c) for c in plan.completed_courses}:
        return CheckResult(
            valid=False,
            reason=f"{course} is already in your completed courses.",
            code=CODE_DUPLICATE,
        )
    # Already placed in a quarter other than the target → moving is fine; placing
    # a second copy is not.  _simulate_move removes prior copies, so a duplicate
    # only remains if the same id appears twice in the source plan.
    occurrences = sum(
        1 for courses in plan.planned_courses.values()
        for c in courses if _norm(c) == nc
    )
    if occurrences > 1:
        return CheckResult(
            valid=False,
            reason=f"{course} already appears more than once in the plan.",
            code=CODE_DUPLICATE,
        )
    return CheckResult(valid=True, reason=None, code=CODE_DUPLICATE)


def check_unit_cap(plan: CoursePlan, course: str, target_quarter: str) -> CheckResult:
    """Reject a placement that pushes the target quarter over its unit cap."""
    sim = _simulate_move(plan, course, target_quarter)
    total = len(sim.planned_courses.get(target_quarter, [])) * UNITS_PER_COURSE
    if total > sim.units_per_quarter:
        return CheckResult(
            valid=False,
            reason=(
                f"{_pretty_quarter(target_quarter)} would reach {total} units "
                f"with {course} — cap is {sim.units_per_quarter}."
            ),
            code=CODE_UNIT_CAP,
        )
    return CheckResult(valid=True, reason=None, code=CODE_UNIT_CAP)


def check_prereq_order(
    plan: CoursePlan,
    course: str,
    target_quarter: str,
    trees: dict[str, dict] | None = None,
    client=None,
) -> CheckResult:
    """Reject a placement that leaves any prerequisite chain unsatisfied.

    Evaluates the *resulting* plan (course moved to target_quarter) and reports
    the first prereq violation — which may be the moved course's own prereq being
    too late, or a course downstream that depended on the moved course.
    """
    sim = _simulate_move(plan, course, target_quarter)

    if trees is None:
        if client is None:
            client = create_client(
                os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
            )
        _load_aliases(client)
        all_ids = list({c for cs in sim.planned_courses.values() for c in cs})
        rows = (
            client.table("courses")
            .select("id,prerequisite_tree")
            .in_("id", all_ids)
            .execute()
            .data
        )
        trees = {
            _norm(r["id"]): r["prerequisite_tree"]
            for r in rows
            if r.get("prerequisite_tree")
        }

    sorted_quarters = sorted(sim.planned_courses.keys(), key=_qkey)
    available: set[str] = {_norm(c) for c in sim.completed_courses}

    for quarter in sorted_quarters:
        same_q = frozenset(_norm(c) for c in sim.planned_courses[quarter])
        for c in sim.planned_courses[quarter]:
            tree = trees.get(_norm(c))
            if tree and not _eval_tree(tree, available, same_q):
                unmet = _representative_unmet(tree, available, same_q)
                if unmet:
                    where = _where_scheduled(unmet, sim)
                    whenc = (f"isn't scheduled until {where}" if where
                             else "isn't in your plan")
                    detail = f"prerequisite {unmet} {whenc}"
                else:
                    detail = "its prerequisites aren't satisfied yet"
                return CheckResult(
                    valid=False,
                    reason=(f"Can't place {c} in {_pretty_quarter(quarter)} — "
                            f"{detail}."),
                    code=CODE_PREREQ_ORDER,
                )
        available.update(_norm(c) for c in sim.planned_courses[quarter])

    return CheckResult(valid=True, reason=None, code=CODE_PREREQ_ORDER)


def validate_move(
    plan: CoursePlan,
    course: str,
    target_quarter: str,
    trees: dict[str, dict] | None = None,
    client=None,
) -> CheckResult:
    """Single public entry point for an editor / API layer.

    Runs every move-level hard constraint for placing ONE course into ONE quarter
    and returns the FIRST violation, or a valid CheckResult if the move is legal.
    Check order: duplicate → unit cap → prerequisite order.

    Note: requirement coverage (REQ_UNCOVERED) is a whole-plan property — placing
    a single course can only help it — so it is validated via validate(), not here.
    """
    if client is None:
        client = create_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
        )

    dup = check_duplicate(plan, course, target_quarter)
    if not dup.valid:
        return dup

    cap = check_unit_cap(plan, course, target_quarter)
    if not cap.valid:
        return cap

    pre = check_prereq_order(plan, course, target_quarter, trees=trees, client=client)
    if not pre.valid:
        return pre

    return CheckResult(valid=True, reason=None, code="OK")

