"""
Optimizer API endpoints.

POST /optimizer/generate      — plan_generator.generate()
POST /optimizer/whatif        — whatif.run_whatif()
POST /optimizer/swap          — swap_suggester.suggest_swaps()
POST /optimizer/move          — swap_suggester.suggest_move()
POST /optimizer/validate_locks — whatif.validate_locks()
"""

import sys
import os

# Ensure backend/ is on sys.path so scripts.optimizer.* is importable
_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import os

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

from scripts.optimizer.hard_constraints import CoursePlan
from scripts.optimizer.plan_generator import generate, FeasibilityError
from scripts.optimizer.swap_suggester import suggest_swaps, suggest_move
from scripts.optimizer.whatif import validate_locks, run_whatif
from scripts.optimizer import cache as optimizer_cache

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

def _supabase_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

router = APIRouter()


# ── Shared Pydantic models ────────────────────────────────────────────────────

class CoursePlanIn(BaseModel):
    major_id:          str
    completed_courses: list[str] = []
    planned_courses:   dict[str, list[str]] = {}
    graduation_year:   int = 2028
    units_per_quarter: int = 16

    def to_domain(self) -> CoursePlan:
        return CoursePlan(
            major_id          = self.major_id,
            completed_courses = self.completed_courses,
            planned_courses   = self.planned_courses,
            graduation_year   = self.graduation_year,
            units_per_quarter = self.units_per_quarter,
        )


def _variant_dict(v) -> dict:
    """Serialise an OptimizationResult to a JSON-safe dict."""
    return {
        "planned_courses": v.plan.planned_courses,
        "soft_score":      round(v.soft_score, 6),
        "soft_breakdown":  {k: round(val, 6) for k, val in v.soft_breakdown.items()},
        "violations":      v.violations,
    }


def _whatif_variant_dict(v) -> dict:
    return {
        "planned_courses": v.planned_courses,
        "locked_courses":  v.locked_courses,
        "soft_score":      round(v.soft_score, 6),
        "soft_breakdown":  {k: round(val, 6) for k, val in v.soft_breakdown.items()},
        "violations":      v.violations,
    }


def _suggestion_dict(s) -> dict:
    return {
        "course_id":        s.course_id,
        "current_quarter":  s.current_quarter,
        "proposed_quarter": s.proposed_quarter,
        "score_before":     round(s.score_before, 6),
        "score_after":      round(s.score_after, 6),
        "score_delta":      round(s.score_delta, 6),
        "reason":           s.reason,
    }


# ── /optimizer/generate ───────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    major_id:           str
    completed_courses:  list[str] = []
    graduation_quarter: str
    units_per_quarter:  int = 16
    waived_ges:         list[str] = []


@router.post("/generate")
def optimizer_generate(req: GenerateRequest):
    cache_key = optimizer_cache.make_key(
        major_id           = req.major_id,
        completed_courses  = req.completed_courses,
        graduation_quarter = req.graduation_quarter,
        units_per_quarter  = req.units_per_quarter,
        waived_ges         = req.waived_ges,
    )
    client = _supabase_client()
    cached = optimizer_cache.get(client, cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        result = generate(
            major_id           = req.major_id,
            completed_courses  = req.completed_courses,
            graduation_quarter = req.graduation_quarter,
            units_per_quarter  = req.units_per_quarter,
            waived_ges         = req.waived_ges,
        )
    except FeasibilityError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "error":              "infeasible",
                "message":            str(e),
                "quarters_available": e.quarters_available,
                "quarters_needed":    e.quarters_needed,
                "courses_to_complete": e.courses_to_complete,
                "years_to_extend":    e.years_to_extend,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    response = {
        "variants":           [_variant_dict(v) for v in result.variants],
        "tight_timeline":     result.tight_timeline,
        "quarters_available": result.quarters_available,
        "quarters_needed":    result.quarters_needed,
        "overflow_count":     result.overflow_count,
        "group_map":          result.group_map,
        "cached":             False,
    }
    optimizer_cache.set(client, cache_key, response)
    return response


# ── /optimizer/whatif ─────────────────────────────────────────────────────────

class WhatIfRequest(BaseModel):
    plan:               CoursePlanIn
    locked_courses:     dict[str, str]       # {course_id: quarter}
    major_id:           str
    graduation_quarter: str
    units_per_quarter:  int = 16
    waived_ges:         list[str] = []


@router.post("/whatif")
def optimizer_whatif(req: WhatIfRequest):
    try:
        result = run_whatif(
            plan               = req.plan.to_domain(),
            locked_courses     = req.locked_courses,
            major_id           = req.major_id,
            graduation_quarter = req.graduation_quarter,
            units_per_quarter  = req.units_per_quarter,
            waived_ges         = req.waived_ges,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if result.lock_conflicts:
        raise HTTPException(
            status_code=422,
            detail={
                "error":    "lock_conflict",
                "conflicts": result.lock_conflicts,
            },
        )

    return {
        "variants":           [_whatif_variant_dict(v) for v in result.variants],
        "lock_conflicts":     result.lock_conflicts,
        "quarters_available": result.quarters_available,
        "quarters_needed":    result.quarters_needed,
        "tight_timeline":     result.tight_timeline,
        "overflow_count":     result.overflow_count,
    }


# ── /optimizer/swap ───────────────────────────────────────────────────────────

class SwapRequest(BaseModel):
    plan:      CoursePlanIn
    course_id: str
    major_id:  str


@router.post("/swap")
def optimizer_swap(req: SwapRequest):
    try:
        suggestions = suggest_swaps(
            plan      = req.plan.to_domain(),
            course_id = req.course_id,
            major_id  = req.major_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"suggestions": [_suggestion_dict(s) for s in suggestions]}


# ── /optimizer/move ───────────────────────────────────────────────────────────

class MoveRequest(BaseModel):
    plan:      CoursePlanIn
    course_id: str


@router.post("/move")
def optimizer_move(req: MoveRequest):
    try:
        suggestions = suggest_move(
            plan      = req.plan.to_domain(),
            course_id = req.course_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"suggestions": [_suggestion_dict(s) for s in suggestions]}


# ── /optimizer/validate_locks ─────────────────────────────────────────────────

class ValidateLocksRequest(BaseModel):
    locked_courses:    dict[str, str]   # {course_id: quarter}
    completed_courses: list[str] = []


@router.post("/validate_locks")
def optimizer_validate_locks(req: ValidateLocksRequest):
    try:
        valid, conflicts = validate_locks(req.locked_courses, req.completed_courses)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"valid": valid, "conflicts": conflicts}
