"""
pytest suite for the optimizer stack.

Run from backend/:
    pytest tests/test_optimizer.py -v
"""

import sys
import os

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(_BACKEND, ".env"))

from scripts.optimizer.hard_constraints import (
    CoursePlan,
    prereqs_satisfied,
    units_valid,
    no_duplicate_courses,
    major_requirements_met,
    _norm,
)
from scripts.optimizer.optimizer import _soft_score
from scripts.optimizer.soft_constraints import _load_difficulty_scores, _load_course_meta
from scripts.optimizer.plan_generator import generate, FeasibilityError
from scripts.optimizer.whatif import validate_locks, optimize_around_locks


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def _make_plan(**kwargs) -> CoursePlan:
    defaults = dict(
        major_id="BS-201G",
        completed_courses=[],
        planned_courses={},
        graduation_year=2029,
        units_per_quarter=16,
    )
    defaults.update(kwargs)
    return CoursePlan(**defaults)


# ── 1. prereqs_satisfied catches violation ────────────────────────────────────

def test_prereqs_not_satisfied_missing_prereq(client):
    """I&CSCI33 requires I&CSCI32; placing it with empty completed should fail."""
    plan = _make_plan(
        completed_courses=[],
        planned_courses={"2024_fall": ["I&CSCI33"]},
    )
    ok, violations = prereqs_satisfied(plan, client)
    assert not ok, "Expected prereq violation but got none"


# ── 2. prereqs_satisfied passes valid plan ────────────────────────────────────

def test_prereqs_satisfied_with_completed(client):
    """I&CSCI33 with I&CSCI32 already completed should pass."""
    plan = _make_plan(
        completed_courses=["I&CSCI32"],
        planned_courses={"2024_fall": ["I&CSCI33"]},
    )
    ok, violations = prereqs_satisfied(plan, client)
    assert ok, f"Expected no violations, got: {violations}"


# ── 3. units_valid catches over-cap ───────────────────────────────────────────

def test_units_valid_over_cap():
    """5 courses × 4 units = 20 > 16-unit cap."""
    plan = _make_plan(
        units_per_quarter=16,
        planned_courses={
            "2024_fall": ["I&CSCI31", "I&CSCI32", "I&CSCI33", "MATH2A", "MATH2B"]
        },
    )
    ok, violations = units_valid(plan)
    assert not ok, "Expected unit-cap violation but got none"


# ── 4. no_duplicate_courses catches duplicate ─────────────────────────────────

def test_no_duplicate_courses():
    """A course in both completed_courses and planned_courses is a duplicate."""
    plan = _make_plan(
        completed_courses=["I&CSCI31"],
        planned_courses={"2024_fall": ["I&CSCI31"]},
    )
    ok, violations = no_duplicate_courses(plan)
    assert not ok, "Expected duplicate violation but got none"


# ── 5. major_requirements_met catches missing courses ────────────────────────

def test_major_requirements_not_met_empty_plan(client):
    """An empty plan for BS-201G should not satisfy all requirements."""
    plan = _make_plan(
        major_id="BS-201G",
        completed_courses=[],
        planned_courses={},
    )
    ok, violations = major_requirements_met(plan, client)
    assert not ok, "Expected unmet requirements but got none"


# ── 6. Soft score: balanced < front-loaded ────────────────────────────────────

def test_soft_score_balanced_better_than_frontloaded(client):
    """A plan with evenly spread difficulty should score lower than a front-loaded one."""
    diff_scores = _load_difficulty_scores()

    front_loaded = _make_plan(planned_courses={
        "2026_fall":   ["COMPSCI161", "COMPSCI162", "COMPSCI163", "COMPSCI164"],
        "2027_winter": ["MATH2A"],
        "2027_spring": ["MATH2B"],
        "2027_fall":   ["WRITING39A"],
    })
    balanced = _make_plan(planned_courses={
        "2026_fall":   ["COMPSCI161", "MATH2A"],
        "2027_winter": ["COMPSCI162", "MATH2B"],
        "2027_spring": ["COMPSCI163", "WRITING39A"],
        "2027_fall":   ["COMPSCI164"],
    })

    all_courses = list({
        c
        for p in (front_loaded, balanced)
        for cs in p.planned_courses.values()
        for c in cs
    })
    meta = _load_course_meta(client, all_courses)

    score_front, _    = _soft_score(front_loaded, diff_scores, meta)
    score_balanced, _ = _soft_score(balanced,     diff_scores, meta)

    assert score_balanced < score_front, (
        f"Balanced ({score_balanced:.4f}) should be lower (better) than "
        f"front-loaded ({score_front:.4f})"
    )


# ── 7. generate returns 3 variants ───────────────────────────────────────────

def test_generate_returns_three_variants():
    result = generate(
        major_id="BS-201G",
        completed_courses=[],
        graduation_quarter="2029_spring",
    )
    assert len(result.variants) == 3, f"Expected 3 variants, got {len(result.variants)}"


# ── 8. variants sorted by soft score ─────────────────────────────────────────

def test_generate_variants_sorted_by_score():
    result = generate(
        major_id="BS-201G",
        completed_courses=[],
        graduation_quarter="2029_spring",
    )
    assert result.variants[0].soft_score <= result.variants[2].soft_score, (
        f"Variant 0 score ({result.variants[0].soft_score}) should be ≤ "
        f"variant 2 score ({result.variants[2].soft_score})"
    )


# ── 9. optimize_around_locks keeps locks fixed, repositions unlocked ─────────

def test_optimize_around_locks_respects_locks():
    """Locked ICS31/ICS33 stay put; unlocked courses may move; all plans hard-valid."""
    plan = _make_plan(
        completed_courses=[],
        planned_courses={
            "2025_fall":   ["I&CSCI31", "WRITING39A", "WRITING39B", "I&CSCI6D"],
            "2026_winter": ["I&CSCI32"],
            "2026_spring": ["I&CSCI33"],
        },
        units_per_quarter=16,
    )
    res = optimize_around_locks(plan, ["I&CSCI31", "I&CSCI33"], top_n=3)
    assert res["status"] == "ok", res
    for p in res["plans"]:
        pos = {_norm(c): q for q, cs in p["planned_courses"].items() for c in cs}
        assert pos[_norm("I&CSCI31")] == "2025_fall"
        assert pos[_norm("I&CSCI33")] == "2026_spring"


def test_optimize_around_locks_infeasible_conflicting_locks():
    """ICS32 pinned before its prereq ICS31 → infeasible with a conflict reason."""
    plan = _make_plan(
        completed_courses=[],
        planned_courses={"2025_fall": ["I&CSCI32"], "2026_fall": ["I&CSCI31"]},
        units_per_quarter=16,
    )
    res = optimize_around_locks(plan, ["I&CSCI32", "I&CSCI31"])
    assert res["status"] == "infeasible" and res["conflicts"], res


# ── 10. validate_locks catches intra-lock conflict ───────────────────────────

def test_validate_locks_detects_conflict():
    """I&CSCI31 is a prereq of I&CSCI32. Locking 32 earlier than 31 is a conflict."""
    valid, conflicts = validate_locks({
        "I&CSCI32": "2026_fall",
        "I&CSCI31": "2027_fall",
    })
    assert not valid, "Expected lock conflict but validate_locks returned valid=True"
    assert len(conflicts) > 0, "Expected at least one conflict message"


# ── 11. Corequisites must share a quarter ─────────────────────────────────────

def test_coreq_split_pairs_detects_and_clears():
    """coreq_split_pairs flags an AND coreq placed in a different quarter, and is
    empty once the pair shares a quarter.  Edge direction is irrelevant
    (MATH105A→105LA holds the edge; the helper treats it symmetrically)."""
    from scripts.optimizer.hard_constraints import coreq_split_pairs
    trees = {"MATH105A": {"AND": [{"prereqType": "course", "courseId": "MATH 105LA", "coreq": True}]}}

    split = _make_plan(
        planned_courses={"2026_fall": ["MATH105LA"], "2027_winter": ["MATH105A"]},
    )
    assert coreq_split_pairs(split, trees) == {frozenset(("MATH105A", "MATH105LA"))}

    together = _make_plan(planned_courses={"2026_fall": ["MATH105A", "MATH105LA"]})
    assert coreq_split_pairs(together, trees) == set()


def test_generate_keeps_coreqs_same_quarter():
    """BS-0K6A requires two coreq pairs (105A/105LA, 105B/105LB) with edges encoded
    in opposite directions.  Every generated variant must keep each pair together."""
    res = generate(
        major_id="BS-0K6A",
        completed_courses=[],
        graduation_quarter="2030_spring",
        units_per_quarter=16,
        start_quarter="2026_fall",
    )
    assert res.variants, "expected at least one variant"
    for v in res.variants:
        loc = {c: q for q, cs in v.plan.planned_courses.items() for c in cs}
        for a, b in [("MATH105A", "MATH105LA"), ("MATH105B", "MATH105LB")]:
            if a in loc and b in loc:
                assert loc[a] == loc[b], f"{a}@{loc[a]} split from {b}@{loc[b]}"


def test_whatif_does_not_split_coreqs():
    """optimize_around_locks must never introduce a coreq split; given a split
    input it may fix it, but the output must have no split pairs."""
    from scripts.optimizer.hard_constraints import coreq_split_pairs
    from scripts.optimizer.optimizer import _prereq_trees

    plan = _make_plan(
        completed_courses=["MATH2A", "MATH2B", "MATH9", "MATH3A"],
        planned_courses={
            "2026_fall":   ["MATH105LA"],
            "2027_winter": ["MATH105A"],
        },
        units_per_quarter=16,
    )
    res = optimize_around_locks(plan, [], top_n=3)
    assert res["status"] == "ok", res
    all_ids = [c for cs in plan.planned_courses.values() for c in cs]
    trees = _prereq_trees(create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")), all_ids)
    for p in res["plans"]:
        cp = CoursePlan(major_id="x", planned_courses=p["planned_courses"])
        assert coreq_split_pairs(cp, trees) == set(), p["planned_courses"]


# ── 12. Unit cap uses real units + escalation ladder ─────────────────────────

def test_unit_cap_tiers_ladder():
    from scripts.optimizer.hard_constraints import unit_cap_tiers
    assert unit_cap_tiers(16) == [16, 20, 24]
    assert unit_cap_tiers(20) == [20, 24]
    assert unit_cap_tiers(24) == [24]
    assert unit_cap_tiers(18) == [18, 20, 24]


def test_units_valid_uses_real_units(client):
    """A quarter of four 5-unit courses is 20 real units — must fail a 16 cap even
    though it is only four courses (the old count proxy passed it)."""
    from scripts.optimizer.hard_constraints import units_valid, quarter_units, _course_units_by_norm
    five_unit = ["FRENCH1C", "GERMAN1C", "ARABIC1B", "ARABIC1C"]  # all 5-unit courses
    ubc = _course_units_by_norm(client, five_unit)
    assert quarter_units(five_unit, ubc) == 20, ubc
    plan = _make_plan(planned_courses={"2026_fall": five_unit}, units_per_quarter=16)
    ok, viols = units_valid(plan, units_by_course=ubc)
    assert not ok and viols, "expected a real-unit cap violation"
    # Same plan passes once the cap is raised to the courses' real total.
    plan20 = _make_plan(planned_courses={"2026_fall": five_unit}, units_per_quarter=20)
    assert units_valid(plan20, units_by_course=ubc)[0]


def test_whatif_respects_real_unit_cap(client):
    """optimize_around_locks must not return a plan whose real units exceed the
    (possibly escalated) cap."""
    from scripts.optimizer.hard_constraints import quarter_units, _course_units_by_norm
    r = generate(major_id="BS-201D", completed_courses=[],
                 graduation_quarter="2030_spring", units_per_quarter=16,
                 start_quarter="2026_fall")
    plan = r.variants[0].plan.planned_courses
    all_ids = [c for cs in plan.values() for c in cs]
    ubc = _course_units_by_norm(client, all_ids)
    res = optimize_around_locks(
        CoursePlan(major_id="BS-201D", planned_courses=plan, units_per_quarter=16), [],
        top_n=3,
    )
    assert res["status"] == "ok", res
    for p in res["plans"]:
        for q, cs in p["planned_courses"].items():
            assert quarter_units(cs, ubc) <= 24, (q, cs, quarter_units(cs, ubc))
