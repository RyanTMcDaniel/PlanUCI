"""
Optimizer API endpoints.

POST /optimizer/generate      — plan_generator.generate()
POST /optimizer/whatif        — whatif.optimize_around_locks()  (rebalance around locks)
POST /optimizer/validate_locks — whatif.validate_locks()
POST /optimizer/requirements_state — plan_generator.get_requirements_state()
"""

import sys
import os
import threading

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
from scripts.optimizer.plan_generator import generate, FeasibilityError, get_requirements_state
from scripts.optimizer.whatif import (
    validate_locks,
    optimize_around_locks,
    propose_prereq_chain,
    apply_prereq_chain,
)
from scripts.optimizer import cache as optimizer_cache

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

def _supabase_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def _load_test_mode() -> bool:
    """True when LOAD_TEST_MODE is set — disables analytics/cache WRITES so a load
    test never pollutes app_stats or the optimizer_cache table. Read at call time
    (not import) so it can be toggled per-process without code changes."""
    return os.getenv("LOAD_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on")


if _load_test_mode():
    print("⚠️  LOAD_TEST_MODE active — schedules_saved counter and optimizer_cache "
          "L2 writes are DISABLED for this process.")


def _increment_stat(stat_key: str) -> None:
    """Fire-and-forget app_stats counter bump — runs off-thread so it can never
    block or raise into the request path."""
    if _load_test_mode():
        return  # load test: don't touch the real schedules_saved counter
    def _run():
        try:
            _supabase_client().rpc("increment_stat", {"stat_key": stat_key}).execute()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


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


# ── /optimizer/generate ───────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    major_id:           str
    completed_courses:  list[str] = []
    graduation_quarter: str
    units_per_quarter:  int = 19   # UCI standard max (12–19); Heavy=20, Overload=22
    waived_ges:         list[str] = []
    ap_scores:          dict[str, int] = {}  # {"AP Calculus AB": 4, "AP Statistics": 5}
    start_quarter:      str = ""   # grid's first quarter (e.g. "2026_fall"); pins the window
    seed_courses:       list[str] = []  # current schedule + GE/Minor picks to keep in the plan
    seed_only:          bool = False    # GE/Minor autofill: don't auto-add major requireds


@router.post("/generate")
def optimizer_generate(req: GenerateRequest):
    # UNREACHABLE FROM THE UI.  The frontend's last caller of this endpoint was
    # removed in 1d996b5 ("autofill: unified buildAndOptimizePool via /api/whatif;
    # remove dead code"), which deleted frontend/app/api/optimizer/route.ts and
    # rewired the planner onto /optimizer/whatif.  Nothing in frontend/ has
    # referenced it since.  Kept for the historical load-test baseline and as a
    # standalone API; do not treat it as the hot path.
    # Seeded (additive autofill) requests are schedule-specific — bypass the
    # shared cache so a seeded result never collides with a from-scratch one.
    use_cache = not req.seed_courses and not req.seed_only
    cache_key = optimizer_cache.make_key(
        major_id           = req.major_id,
        completed_courses  = req.completed_courses,
        graduation_quarter = req.graduation_quarter,
        units_per_quarter  = req.units_per_quarter,
        waived_ges         = req.waived_ges,
        ap_scores          = req.ap_scores,
        start_quarter      = req.start_quarter or None,
    )

    if use_cache:
        # L1: in-process memory — zero network cost for repeated identical requests.
        l1_hit = optimizer_cache.get_l1(cache_key)
        if l1_hit is not None:
            return {**l1_hit, "cached": True}

        # L2: Supabase — survives restarts, shared across instances.
        client = _supabase_client()
        l2_hit = optimizer_cache.get(client, cache_key)
        if l2_hit is not None:
            optimizer_cache.set_l1(cache_key, l2_hit)   # backfill L1
            return {**l2_hit, "cached": True}
    else:
        client = _supabase_client()

    try:
        result = generate(
            major_id           = req.major_id,
            completed_courses  = req.completed_courses,
            graduation_quarter = req.graduation_quarter,
            units_per_quarter  = req.units_per_quarter,
            waived_ges         = req.waived_ges,
            ap_scores          = req.ap_scores,
            start_quarter      = req.start_quarter or None,
            seed_courses       = req.seed_courses,
            seed_only          = req.seed_only,
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
        "variants":             [_variant_dict(v) for v in result.variants],
        "tight_timeline":       result.tight_timeline,
        "quarters_available":   result.quarters_available,
        "quarters_needed":      result.quarters_needed,
        "overflow_count":       result.overflow_count,
        "overflow_courses":     result.overflow_courses,
        "extended_by":          result.extended_by,
        "group_map":            result.group_map,
        "ap_credited_courses":  result.ap_credited_courses,
        "ap_units_awarded":     result.ap_units_awarded,
        "choice_groups":        result.choice_groups,
        "cached":               False,
    }
    if use_cache:
        optimizer_cache.set_l1(cache_key, response)   # L1: in-process
        optimizer_cache.set(client, cache_key, response)  # L2: Supabase
    _increment_stat("schedules_saved")  # fire-and-forget; never blocks the response
    return response


# ── /optimizer/whatif — rebalance the existing plan around locked courses ─────

class WhatIfRequest(BaseModel):
    plan:               CoursePlanIn
    locked_courses:     dict[str, str]       # {course_id: quarter} — locked in the grid
    major_id:           str = ""             # accepted for backward compat; unused
    graduation_quarter: str = ""             # accepted for backward compat; unused
    units_per_quarter:  int = 16
    waived_ges:         list[str] = []
    ap_scores:          dict[str, int] = {}


@router.post("/whatif")
def optimizer_whatif(req: WhatIfRequest):
    """Rebalance the plan around locked courses (optimize_around_locks).

    Locked courses never move; only unlocked courses already in the plan are
    repositioned.  The course set is the plan itself (the editor model) — this no
    longer regenerates from major requirements.  Returns the optimize_around_locks
    shape: {"status":"ok","plans":[...]} or {"status":"infeasible","conflicts":[...]}.

    Caching is admitted selectively, because the key embeds the whole grid and so
    most entries would be single-use:

      zero locks (the autofill/buildAndOptimizePool shape) → L1 + L2
          Built deterministically from major requirements, so two users with the
          same major/picks/years/cap send a byte-identical payload.  This is the
          only genuinely cross-user-shareable whatif traffic.

      locks + "infeasible" → L1 only
          The most expensive path in the function: it returns only after
          exhausting every unit_cap_tier x all 5 seed configs (up to 15 full
          hill-climbs, vs 5 for a success that breaks at the first tier).  It is
          also the most likely to be re-clicked unedited, since an infeasible
          response leaves the grid untouched — the frontend only shows a toast.
          Kept out of L2: a prereq fix in the DB should not stay masked by a
          shared "can't optimize" row.

      locks + "ok" → not cached
          Each click's input is the previous click's output (setPlannedCourses),
          so reuse is near zero while the payload is the largest of the three.
          Not worth the eviction pressure.
    """
    plan       = req.plan.to_domain()
    locked_ids = list(req.locked_courses.keys())
    zero_lock  = not locked_ids

    cache_key = optimizer_cache.make_whatif_key(
        planned_courses   = plan.planned_courses,
        completed_courses = plan.completed_courses,
        graduation_year   = plan.graduation_year,
        units_per_quarter = plan.units_per_quarter,
        locked_course_ids = locked_ids,
        ap_scores         = req.ap_scores,
    )

    # L1: in-process, whatif-only pool — never evicts generate entries.
    l1_hit = optimizer_cache.get_l1_whatif(cache_key)
    if l1_hit is not None:
        return {**l1_hit, "cached": True}

    # L2 is consulted only for the shape that can actually be shared.
    client = None
    if zero_lock:
        client = _supabase_client()
        l2_hit = optimizer_cache.get(client, cache_key, ttl=optimizer_cache.WHATIF_TTL)
        if l2_hit is not None:
            optimizer_cache.set_l1_whatif(cache_key, l2_hit)   # backfill L1
            return {**l2_hit, "cached": True}

    try:
        result = optimize_around_locks(
            plan,
            locked_ids,
            ap_scores=req.ap_scores or None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    response = {**result, "cached": False}

    if zero_lock:
        optimizer_cache.set_l1_whatif(cache_key, response)   # L1: in-process
        optimizer_cache.set(client, cache_key, response)     # L2: Supabase
    elif result.get("status") == "infeasible":
        optimizer_cache.set_l1_whatif(cache_key, response)   # L1 only

    return response


# ── /optimizer/propose_prereqs — detect a course's missing prereq chain ───────

class ProposePrereqsRequest(BaseModel):
    plan:      CoursePlanIn
    course_id: str


@router.post("/propose_prereqs")
def optimizer_propose_prereqs(req: ProposePrereqsRequest):
    """Propose (not apply) placements for a course's missing prerequisite chain.

    Returns {missing, proposed_placements, status, conflicts} — the plan is never
    mutated here; the frontend shows the proposal and the user confirms.
    """
    try:
        return propose_prereq_chain(req.plan.to_domain(), req.course_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /optimizer/apply_prereqs — commit an accepted prereq proposal ─────────────

class ApplyPrereqsRequest(BaseModel):
    plan:                CoursePlanIn
    proposed_placements: list[dict]   # [{course_id, quarter, ...}, ...]


@router.post("/apply_prereqs")
def optimizer_apply_prereqs(req: ApplyPrereqsRequest):
    """Apply an accepted proposal, returning the updated planned_courses."""
    try:
        updated = apply_prereq_chain(req.plan.to_domain(), req.proposed_placements)
        return {"planned_courses": updated.planned_courses}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /optimizer/validate_locks ─────────────────────────────────────────────────

class ValidateLocksRequest(BaseModel):
    locked_courses:    dict[str, str]        # {course_id: quarter}
    completed_courses: list[str] = []
    ap_scores:         dict[str, int] = {}   # {"AP Calculus BC": 4, ...}


# ── /optimizer/requirements_state ─────────────────────────────────────────────

class RequirementsStateRequest(BaseModel):
    plan:       CoursePlanIn
    waived_ges: list[str] = []
    ap_scores:  dict[str, int] = {}


@router.post("/requirements_state")
def optimizer_requirements_state(req: RequirementsStateRequest):
    """Return required_placed + unresolved choice_groups (with remaining) for a plan."""
    try:
        state = get_requirements_state(
            req.plan.to_domain(),
            waived_ges=req.waived_ges,
            ap_scores=req.ap_scores or None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return state


@router.post("/validate_locks")
def optimizer_validate_locks(req: ValidateLocksRequest):
    try:
        valid, conflicts = validate_locks(
            req.locked_courses,
            req.completed_courses,
            req.ap_scores or None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"valid": valid, "conflicts": conflicts}
