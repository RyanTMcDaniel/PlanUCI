"""Payload construction for the whatif load-test scenarios.

Kept OUT of locustfile.py so it can be imported by tools that also import
supabase/ssl: locust monkey-patches ssl via gevent at import time, and importing
locust after ssl is already loaded raises RecursionError.  validate_whatif_pool.py
needs these builders and the Supabase client, so they live here with no locust
dependency.
"""

# ── whatif payload construction ───────────────────────────────────────────────
# Mirrors what PlannerClient.tsx actually sends.  Both call sites post the same
# envelope; they differ only in completed_courses and whether locked_courses is
# populated:
#
#   "Optimize Schedule" button   (:4120)  completed_courses: []          locks: user's
#   buildAndOptimizePool         (:3319)  completed_courses: [...apCreditedSet]
#
#   {
#     "plan": {major_id, completed_courses, planned_courses,
#              graduation_year, units_per_quarter},
#     "locked_courses": {course_id: quarter},
#     "major_id": ..., "graduation_quarter": ...,   <- both inert, sent anyway
#     "units_per_quarter": ..., "waived_ges": [],   <- both inert, sent anyway
#     "ap_scores": {...}
#   }
#
# The four inert fields are reproduced faithfully so the wire payload matches the
# real client byte-for-byte in shape; the backend ignores them (see the
# DELIBERATELY EXCLUDED note in cache.make_whatif_key).

_START_YEAR    = 2026   # PlannerClient.tsx:271  START_YEAR
_DEFAULT_YEARS = 4      # PlannerClient.tsx:272  DEFAULT_YEARS
_DEFAULT_CAP   = 16     # PlannerClient.tsx:2012 useState(16)
_UNITS_PER_COURSE = 4   # frontend falls back to 4 when min_units is unknown


def _qkey(year: int, season: str) -> str:
    """Port of PlannerClient.tsx:280 qkey().

    Fall belongs to START_YEAR + year - 1; winter/spring/summer belong to the
    calendar year AFTER that fall.  So year 1 = 2026_fall, 2027_winter, ...
    """
    fall_year = _START_YEAR + year - 1
    cal_year  = fall_year if season == "fall" else fall_year + 1
    return f"{cal_year}_{season}"


def _quarter_list(num_years=_DEFAULT_YEARS, summer_years=()):
    """Port of the grid loop at PlannerClient.tsx:2913-2916 — the exact key order
    the client builds (fall, winter, spring, then summer when enabled)."""
    quarters = []
    for y in range(1, num_years + 1):
        for s in ("fall", "winter", "spring"):
            quarters.append(_qkey(y, s))
        if y in summer_years:
            quarters.append(_qkey(y, "summer"))
    return quarters


def _grad_quarter(num_years=_DEFAULT_YEARS) -> str:
    """PlannerClient.tsx:2093 — gradQuarter = qkey(numYears, "spring")."""
    return _qkey(num_years, "spring")


def _whatif_body(major, grid, locked=None, ap_scores=None,
                 num_years=_DEFAULT_YEARS, cap=_DEFAULT_CAP, completed=None):
    """Assemble the exact envelope PlannerClient posts to /api/whatif.

    `grid` is a {quarter: [course_id]} mapping taken straight from the fixture —
    a prereq-valid placement produced once by validate_whatif_pool.py, which has
    the DB access needed to do ASAP placement.  Packing is NOT done here: getting
    it right requires prerequisite trees, and a mis-packed seed makes every
    request come back infeasible, which would silently turn the whole load test
    into a worst-path benchmark.
    """
    grad_q = _grad_quarter(num_years)
    return {
        "plan": {
            "major_id":          major,
            "completed_courses": list(completed or []),
            "planned_courses":   grid,
            "graduation_year":   int(grad_q.split("_")[0]),
            "units_per_quarter": cap,
        },
        "locked_courses":     dict(locked or {}),
        "major_id":           major,             # inert (backward compat)
        "graduation_quarter": grad_q,            # inert (backward compat)
        "units_per_quarter":  cap,               # inert (plan's value is used)
        "waived_ges":         [],                # inert (never forwarded)
        "ap_scores":          dict(ap_scores or {}),
    }


def _lock_set(grid, n, offset=0):
    """Pick n courses from the packed grid and pin them to the quarter they sit in.

    Mirrors the frontend's lockedMap construction (:4113-4118 and :2861-2866):
    {course_id: quarter}.  Deterministic in (n, offset) so each simulated user
    gets a stable but distinct lock configuration.
    """
    flat = [(c, q) for q, cs in grid.items() for c in cs]
    if not flat:
        return {}
    return {
        flat[(offset + k * 3) % len(flat)][0]: flat[(offset + k * 3) % len(flat)][1]
        for k in range(min(n, len(flat)))
    }
