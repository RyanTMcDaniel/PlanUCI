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


def _norm(course_id: str) -> str:
    """Normalize course ID for comparison: remove spaces, uppercase, resolve UCI dept aliases."""
    s = course_id.replace(" ", "").upper()
    # prereq_edges stores raw catalog text like "I&C SCI 46"; courses table uses "ICS46"
    s = s.replace("I&CSCI", "ICS")
    return s


@dataclass
class CoursePlan:
    major_id: str
    completed_courses: list[str] = field(default_factory=list)
    planned_courses: dict[str, list[str]] = field(default_factory=dict)
    graduation_year: int = 2028
    units_per_quarter: int = 16


# ── Prerequisite tree evaluation ──────────────────────────────────────────────

def _eval_item(item: dict, available: set[str]) -> bool:
    prereq_type = item.get("prereqType")
    if prereq_type == "course":
        # _norm strips spaces so "MATH 2A" matches "MATH2A" in available
        return _norm(item.get("courseId", "")) in available
    if prereq_type == "exam":
        return False  # treat placement exams as not satisfied
    return _eval_tree(item, available)


def _eval_tree(node: dict, available: set[str]) -> bool:
    """Recursively evaluate an AND/OR/NOT prerequisite_tree node."""
    for logic_key in ("AND", "OR", "NOT"):
        if logic_key not in node:
            continue
        items = node[logic_key]
        if logic_key == "AND":
            return all(_eval_item(i, available) for i in items)
        if logic_key == "OR":
            return any(_eval_item(i, available) for i in items)
        if logic_key == "NOT":
            # Only fail if a course anti-coreq is explicitly in available.
            # Exams and nested subtrees inside NOT are ignored.
            return not any(
                i.get("prereqType") == "course"
                and _norm(i.get("courseId", "")) in available
                for i in items
            )
    return True  # empty node — no prereqs


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

def prereqs_satisfied(plan: CoursePlan, client) -> tuple[bool, list[str]]:
    """Every planned course must have its prerequisites completed or planned earlier.

    OR logic is handled by evaluating the prerequisite_tree JSON from the
    courses table.  If prereq_edges has a group_id / alternative_group column
    we use that instead (rows in the same group are treated as OR alternatives).
    """
    violations: list[str] = []

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
                        violations.append(
                            f"{course} in {quarter}: need one of {sample}{ellipsis} "
                            f"(OR group {gkey!r}) — none satisfied"
                        )
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
            for course in plan.planned_courses[quarter]:
                nc = _norm(course)
                tree = prereq_trees.get(nc)
                if tree and not _eval_tree(tree, available):
                    violations.append(
                        f"{course} in {quarter}: prerequisites not satisfied "
                        f"(evaluated prerequisite_tree for {course})"
                    )
            available.update(_norm(c) for c in plan.planned_courses[quarter])

    return len(violations) == 0, violations


def major_requirements_met(plan: CoursePlan, client) -> tuple[bool, list[str]]:
    """Required, elective, and GE requirement rows must be covered by the plan."""
    violations: list[str] = []

    rows = (
        client.table("major_requirements")
        .select("requirement_type,courses,courses_needed,group_name")
        .eq("major_id", plan.major_id)
        .execute()
        .data
    )

    if not rows:
        return True, [f"No requirements found for major {plan.major_id!r} — skipping"]

    all_courses = {_norm(c) for c in plan.completed_courses}
    for courses in plan.planned_courses.values():
        all_courses.update(_norm(c) for c in courses)

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
            violations.append(
                f"[{req_type}] {group_name!r}: need {needed} of "
                f"{short}{ellipsis}, have {covered}"
            )

    return len(violations) == 0, violations


def units_valid(plan: CoursePlan, client=None) -> tuple[bool, list[str]]:
    """No quarter may exceed units_per_quarter."""
    violations: list[str] = []

    for quarter, courses in plan.planned_courses.items():
        total = len(courses) * UNITS_PER_COURSE
        if total > plan.units_per_quarter:
            violations.append(
                f"{quarter}: {len(courses)} courses = {total} units "
                f"(cap is {plan.units_per_quarter})"
            )

    return len(violations) == 0, violations


def no_duplicate_courses(plan: CoursePlan, client=None) -> tuple[bool, list[str]]:
    """No course may appear in completed_courses and planned_courses, or twice in planned."""
    violations: list[str] = []
    completed_set = {_norm(c) for c in plan.completed_courses}
    seen: set[str] = set()

    for quarter, courses in plan.planned_courses.items():
        for course in courses:
            nc = _norm(course)
            if nc in completed_set:
                violations.append(f"{course} in {quarter} is already in completed_courses")
            if nc in seen:
                violations.append(f"{course} in {quarter} appears more than once in planned_courses")
            seen.add(nc)

    return len(violations) == 0, violations


# ── Combined validator ────────────────────────────────────────────────────────

def validate(plan: CoursePlan) -> tuple[bool, list[str]]:
    """Run all hard constraints. Returns (all_passed, all_violations)."""
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    checks = [
        ("prereqs_satisfied",      prereqs_satisfied(plan, client)),
        ("major_requirements_met", major_requirements_met(plan, client)),
        ("units_valid",            units_valid(plan)),
        ("no_duplicate_courses",   no_duplicate_courses(plan)),
    ]

    all_passed = True
    all_violations: list[str] = []

    for name, (passed, violations) in checks:
        if not passed:
            all_passed = False
        all_violations.extend(violations)

    return all_passed, all_violations

