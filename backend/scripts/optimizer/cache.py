"""
Optimizer result cache backed by Supabase.

Table (run once in Supabase SQL editor before using):

    CREATE TABLE IF NOT EXISTS optimizer_cache (
        cache_key   TEXT PRIMARY KEY,
        result_json JSONB NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        hit_count   INTEGER NOT NULL DEFAULT 0
    );

Cache key is a SHA-256 hash of the endpoint's inputs so identical requests
always hit the same row regardless of call order.  Two key namespaces share the
table: generate() inputs via make_key(), whatif inputs via make_whatif_key().

TTL: 7 days for generate; 6 hours for whatif (see WHATIF_TTL).  Stale entries
are deleted on read and regenerated on next call.

All public functions are intentionally non-fatal: a cache miss (including any
Supabase error) returns None so callers always fall back to computing the result.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from cachetools import TTLCache
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

_TABLE   = "optimizer_cache"
_TTL     = timedelta(days=7)

# whatif entries expire far sooner than generate entries.  Both go stale the same
# way — neither key covers the external data listed under "Invalidation surface"
# below — but the consequence differs sharply.  A stale generate result is a
# suggestion the user can discard; a stale whatif result is written straight into
# the user's grid by setPlannedCourses() in PlannerClient and then saved via
# /plans, so it becomes their persisted schedule.  Shorter TTL bounds how long a
# plan built against superseded course data can be committed that way.
WHATIF_TTL = timedelta(hours=6)   # public: passed by the whatif router to get()

# ── Invalidation surface (applies to BOTH namespaces) ─────────────────────────
# None of these are inputs to any cache key, so a change to any of them leaves
# both tiers stale until TTL expiry.  Flush after any data refresh that touches:
#   1. courses.prerequisite_tree  — _prereq_trees()          (optimizer.py)
#   2. courses.min_units          — _course_units_by_norm()  (hard_constraints.py)
#   3. courses.terms              — _fetch_course_terms()    (plan_generator.py)
#   4. courses.ge_list            — _load_course_meta()      (soft_constraints.py)
#   5. courses.department         — _load_course_meta()      (soft_constraints.py)
#   6. ap_credits table           — _resolve_ap_credits()    (plan_generator.py)
#   7. ml/data/course_features.csv — _load_difficulty_scores() (soft_constraints.py)
# Note (7) is additionally memoized process-wide at first read, so it is frozen
# for the process lifetime regardless of this cache.


# ── L1: in-process memory cache ───────────────────────────────────────────────
# Sits in front of the Supabase L2 cache. Same key space; shorter TTL (1 h)
# since the process may restart between requests.  Thread-safe via RLock.

_L1_TTL = 3600  # seconds — evict after 1 hour even if the process stays up
_L1_MAX = 256   # max entries before LRU eviction kicks in

_l1: TTLCache = TTLCache(maxsize=_L1_MAX, ttl=_L1_TTL)

# whatif gets its OWN pool rather than sharing _l1.  whatif keys embed the full
# course grid, so entries are largely single-use; sharing one 256-slot pool would
# let them evict generate entries — which DO see cross-user reuse — by LRU.
# Separate instances make that impossible regardless of traffic mix.
_L1_WHATIF_MAX = 128
_l1_whatif: TTLCache = TTLCache(maxsize=_L1_WHATIF_MAX, ttl=_L1_TTL)

_l1_lock = threading.RLock()  # guards both pools


def get_l1(cache_key: str) -> dict | None:
    """Return the in-memory cached generate result, or None on miss."""
    with _l1_lock:
        return _l1.get(cache_key)


def set_l1(cache_key: str, result: dict) -> None:
    """Store a generate result in the in-memory cache."""
    with _l1_lock:
        _l1[cache_key] = result


def get_l1_whatif(cache_key: str) -> dict | None:
    """Return the in-memory cached whatif result, or None on miss."""
    with _l1_lock:
        return _l1_whatif.get(cache_key)


def set_l1_whatif(cache_key: str, result: dict) -> None:
    """Store a whatif result in the whatif-only in-memory cache."""
    with _l1_lock:
        _l1_whatif[cache_key] = result


def invalidate_l1(cache_key: str | None = None) -> None:
    """Evict one entry (or everything) from the in-memory caches.

    Call this whenever the underlying course/requirement data changes so the
    next request re-runs the optimizer instead of returning a stale snapshot.
    Passing None clears BOTH L1 pools (generate and whatif) — a data refresh
    invalidates every namespace, so clearing only one would silently leave the
    other warm.  A specific key is popped from both; namespace prefixes mean a
    key can only ever live in one, so the extra pop is a harmless no-op.
    """
    with _l1_lock:
        if cache_key is None:
            _l1.clear()
            _l1_whatif.clear()
        else:
            _l1.pop(cache_key, None)
            _l1_whatif.pop(cache_key, None)

MIGRATION_SQL = """\
-- Run once in the Supabase SQL editor:
CREATE TABLE IF NOT EXISTS optimizer_cache (
    cache_key   TEXT PRIMARY KEY,
    result_json JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count   INTEGER NOT NULL DEFAULT 0
);
"""


# ── Key construction ──────────────────────────────────────────────────────────

def make_key(
    major_id:           str,
    completed_courses:  list[str],
    graduation_quarter: str,
    units_per_quarter:  int,
    waived_ges:         list[str] | None = None,
    ap_scores:          dict[str, int] | None = None,
    start_quarter:      str | None = None,
) -> str:
    """Return a deterministic SHA-256 hex key for the given generate() inputs."""
    payload = json.dumps(
        {
            "major_id":           major_id,
            "completed_courses":  sorted(completed_courses),
            "graduation_quarter": graduation_quarter,
            "units_per_quarter":  units_per_quarter,
            "waived_ges":         sorted(waived_ges or []),
            "ap_scores":          dict(sorted((ap_scores or {}).items())),
            "start_quarter":      start_quarter or "",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def make_whatif_key(
    planned_courses:   dict[str, list[str]],
    completed_courses: list[str],
    graduation_year:   int,
    units_per_quarter: int,
    locked_course_ids: list[str],
    ap_scores:         dict[str, int] | None = None,
) -> str:
    """Return a deterministic SHA-256 hex key for optimize_around_locks() inputs.

    Covers exactly the values that reach the computation, and nothing else.

    Order-normalized: quarter keys and the course list within each quarter are
    sorted before hashing, so two semantically identical grids share one entry.
    optimize_around_locks is NOT order-invariant (_perturb_unlocked reads
    planned_courses.keys() in dict order into rng.sample, and rng.choice depends
    on within-quarter list order), so a hit may return a plan computed from a
    differently-ordered input.  That plan still passed the full hard-constraint
    gate (_check_prereqs + units_valid) before becoming a candidate, so it is
    always valid.  Normalizing is also what makes the repeat-click case hit at
    all: the optimizer rebuilds its lists via remove/append, so its output order
    never matches the order the client sent.

    DELIBERATELY EXCLUDED — the whatif handler accepts these but never passes
    them to optimize_around_locks, so including them would only cause spurious
    misses:
        major_id            — request field AND plan.major_id; neither is read by
                              optimize_around_locks or _soft_score (major_clustering
                              uses `department` from course meta, not major_id)
        graduation_quarter  — marked "accepted for backward compat; unused"
        units_per_quarter   — the TOP-LEVEL request field only; it is shadowed by
                              plan.units_per_quarter, which IS keyed below
        waived_ges          — accepted by WhatIfRequest but never forwarded
    Also excluded: the VALUES of locked_courses.  Only .keys() is passed; lock
    quarters are re-derived from the grid itself.

    !! If any of the above is ever wired into optimizer_whatif, it MUST be added
    here first.  Otherwise two semantically different requests hash identically
    and one user is served the other's plan — and because the whatif response is
    written into the grid by setPlannedCourses(), that wrong plan becomes their
    saved schedule.
    """
    grid_norm = {q: sorted(planned_courses[q]) for q in sorted(planned_courses)}
    # Locked ids absent from the grid are dropped by optimize_around_locks
    # (locked_map only keeps ids it can find a quarter for), so they are provably
    # no-ops — intersecting keeps them from fragmenting the key space.
    # NB: this module defines a public set() function, which shadows the builtin
    # here — so build/intersect sets via literals and methods, never set(...).
    in_grid = {c for courses in planned_courses.values() for c in courses}
    payload = json.dumps(
        {
            "v":                 "whatif-1",   # namespace: never collides with make_key
            "planned":           grid_norm,
            "completed":         sorted(completed_courses),
            "graduation_year":   graduation_year,
            "units_per_quarter": units_per_quarter,
            "locked":            sorted(in_grid.intersection(locked_course_ids)),
            "ap_scores":         dict(sorted((ap_scores or {}).items())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Read ──────────────────────────────────────────────────────────────────────

def get(client, cache_key: str, ttl: timedelta | None = None) -> dict | None:
    """Return cached result dict or None on miss / error / TTL expiry.

    Stale entries (older than TTL) are deleted synchronously so the table
    stays clean without a separate cleanup job.

    ttl defaults to the module-wide _TTL (7 days, the generate value); whatif
    callers pass the shorter WHATIF_TTL.  Expiry is enforced here on read, so
    no schema change is needed to support per-namespace lifetimes.
    """
    ttl = _TTL if ttl is None else ttl
    try:
        rows = (
            client.table(_TABLE)
            .select("result_json,created_at,hit_count")
            .eq("cache_key", cache_key)
            .execute()
            .data
        )
        if not rows:
            return None

        entry      = rows[0]
        created_at = datetime.fromisoformat(entry["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) - created_at > ttl:
            # Stale — evict and treat as miss
            client.table(_TABLE).delete().eq("cache_key", cache_key).execute()
            return None

        # Increment hit counter (non-fatal if it fails)
        try:
            client.table(_TABLE).update(
                {"hit_count": entry["hit_count"] + 1}
            ).eq("cache_key", cache_key).execute()
        except Exception:
            pass

        return entry["result_json"]

    except Exception:
        return None


# ── Write ─────────────────────────────────────────────────────────────────────

def _load_test_mode() -> bool:
    """True when LOAD_TEST_MODE is set — suppresses the L2 (Supabase) write so a
    load test never pollutes the optimizer_cache table with thousands of rows.
    L1 (in-process) caching is left untouched so warm-path behavior still works."""
    return os.getenv("LOAD_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def set(client, cache_key: str, result: dict) -> None:
    """Upsert result into the cache.  Silently swallows errors."""
    if _load_test_mode():
        return  # load test: skip the L2 upsert (junk-row pollution guard)
    try:
        client.table(_TABLE).upsert(
            {
                "cache_key":   cache_key,
                "result_json": result,
                "created_at":  datetime.now(timezone.utc).isoformat(),
                "hit_count":   0,
            },
            on_conflict="cache_key",
        ).execute()
    except Exception:
        pass

