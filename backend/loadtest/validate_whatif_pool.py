"""Regenerate the whatif-scenario course pool.

Builds a realistic course set per major DIRECTLY FROM SUPABASE — major
requirements plus the transitive prerequisite closure — which is what
buildAndOptimizePool assembles client-side before posting to /api/whatif.

Deliberately does NOT touch /optimizer/generate.  That endpoint has no frontend
caller and may be deleted; the whatif load test must not depend on it.  The only
endpoint probed here is /optimizer/whatif itself, purely to confirm each pool
actually produces a usable response.

Each kept major returns HTTP 200 from POST /optimizer/whatif with either
status="ok" (plans returned) or status="infeasible" (conflicts returned) — both
are legitimate results that exercise the full solve.

Run from backend/:
    venv/bin/python loadtest/validate_whatif_pool.py [--n 4] [--url ...]

Paste the printed WHATIF_POOL into whatif_pool.py.

WHATIF_POOL maps major_id -> {quarter: [course_id]} — a prereq-valid grid, not a
flat course list, because correct placement needs the prerequisite trees this
script has and the load-test payload builder does not.
"""

import argparse
import json
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND, ".env"))

import requests
from supabase import create_client

from scripts.optimizer.hard_constraints import _eval_tree, _norm
from scripts.optimizer.plan_generator import (
    _fetch_prereq_trees,
    _resolve_implicit_prereqs,
    collect_requirements,
)

# Import the payload builders from whatif_payload, NOT locustfile: importing
# locust here would monkey-patch ssl after supabase has already loaded it, which
# raises RecursionError.
try:
    from loadtest.whatif_payload import _quarter_list, _whatif_body
except ModuleNotFoundError:  # invoked from inside backend/loadtest/
    from whatif_payload import _quarter_list, _whatif_body

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8001")
ap.add_argument("--n", type=int, default=4, help="how many good majors to keep")
ap.add_argument("--max-courses", type=int, default=48,
                help="cap per major so one outlier can't dominate the grid")
ap.add_argument("--min-courses", type=int, default=24,
                help="skip trivially small majors — a real student grid is ~45 "
                     "courses, and a 4-course pool measures nothing")
args = ap.parse_args()

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
ids = sorted({r["major_id"] for r in
              sb.table("major_requirements").select("major_id").execute().data
              if r.get("major_id")})
# Same spread as validate_pool.py; always try the known-good ICS major first.
candidates = ["BS-201G"] + ids
seen, ordered = set(), []
for c in candidates:
    if c not in seen:
        seen.add(c)
        ordered.append(c)

EP = f"{args.url.rstrip('/')}/optimizer/whatif"


def _course_leaves(node):
    """Every courseId appearing anywhere in a prereq tree."""
    out = set()
    if not isinstance(node, dict):
        return out
    for key in ("AND", "OR", "NOT"):
        for item in node.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("prereqType") == "course":
                out.add(_norm(item.get("courseId", "")))
            else:
                out |= _course_leaves(item)
    return out


def _topo_order(courses, trees):
    """Order courses so a course never precedes an in-pool prerequisite.

    buildAndOptimizePool does a topological pass before round-robin placement
    (PlannerClient.tsx step (d)); without it, naive round-robin scatters prereq
    chains and the solve comes back infeasible for anything non-trivial.  Cycles
    and unresolvable leftovers are appended in their original order.
    """
    in_pool = {_norm(c) for c in courses}
    placed, out = set(), []
    remaining = list(courses)
    while remaining:
        progressed = False
        for c in list(remaining):
            needed = _course_leaves(trees.get(_norm(c)) or {}) & in_pool
            if needed <= placed:
                out.append(c)
                placed.add(_norm(c))
                remaining.remove(c)
                progressed = True
        if not progressed:            # cycle / unsatisfiable — emit the rest as-is
            out.extend(remaining)
            break
    return out


def asap_grid(courses, trees, quarters, cap=16, units_per_course=4):
    """Place courses into the earliest quarter whose prereqs are already satisfied.

    Prerequisites must sit in a STRICTLY earlier quarter, so neither round-robin
    nor sequential fill works: both put a course and its prereq in the same
    quarter.  This mirrors the frontend's topological pass (and the backend's own
    _asap_place_missing): walk quarters in order, and in each one place any course
    whose in-pool prereqs are all already placed earlier, up to the unit cap.

    Returns (grid, leftover).  Leftover courses are dropped rather than forced —
    an over-stuffed grid is what makes the seed infeasible.
    """
    per_quarter = max(1, cap // units_per_course)
    grid = {q: [] for q in quarters}
    remaining = list(courses)
    placed_norm = set()
    for q in quarters:
        for c in list(remaining):
            if len(grid[q]) >= per_quarter:
                break
            tree = trees.get(_norm(c))
            if tree and not _eval_tree(tree, placed_norm):
                continue
            grid[q].append(c)
            remaining.remove(c)
        # Only courses in STRICTLY earlier quarters count for the next quarter.
        placed_norm |= {_norm(c) for c in grid[q]}
    return grid, remaining


def course_set(major_id):
    """Required courses + transitive prereq closure — the buildAndOptimizePool pool."""
    required, _choice_groups, _ap_credited, _ap_units = collect_requirements(
        sb, major_id, completed_norm=set()
    )
    if not required:
        return [], {}
    trees = _fetch_prereq_trees(sb, required)
    expanded, trees = _resolve_implicit_prereqs(required, trees, set(), sb)
    # De-dupe on normalized id while preserving order.
    out, seen_norm = [], set()
    for c in expanded:
        n = _norm(c)
        if n not in seen_norm:
            seen_norm.add(n)
            out.append(c)
    return _topo_order(out[: args.max_courses], trees), trees


pool = {}
for mid in ordered:
    try:
        courses, trees = course_set(mid)
        if len(courses) < args.min_courses:
            continue
        grid, leftover = asap_grid(courses, trees, _quarter_list())
        placed = sum(len(v) for v in grid.values())
        # Filter on PLACED count, not pool size.  Courses whose prereqs are not
        # themselves in the pool (satisfied by choice-group picks a real student
        # makes) can never be scheduled, and any plan containing one is rejected
        # by optimize_around_locks — so a pool of 40 that places 3 yields a
        # 3-course grid, which measures nothing.
        if placed < args.min_courses:
            continue
        body = _whatif_body(mid, grid, locked=None, ap_scores={"_probe": 0})
        r = requests.post(EP, json=body, timeout=180)
        payload = r.json() if r.status_code == 200 else {}
        status = payload.get("status")
        n_plans = len(payload.get("plans", []))
        n_conf = len(payload.get("conflicts", []))
        ok = r.status_code == 200 and (
            (status == "ok" and n_plans > 0) or status == "infeasible"
        )
        print(f"  {mid:10s} -> {r.status_code}  placed={placed:3d}/{len(courses):3d}  "
              f"status={status!s:12s} plans={n_plans} conflicts={n_conf}  "
              f"{'KEEP' if ok else 'skip'}")
        if ok:
            pool[mid] = grid
    except Exception as exc:
        print(f"  {mid:10s} -> EXC {type(exc).__name__}: {exc}")
    if len(pool) >= args.n:
        break

print("\nWHATIF_POOL =", json.dumps(pool, indent=4))
