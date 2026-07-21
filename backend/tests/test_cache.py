"""
Tests for the optimizer L1 in-memory cache layer.

Every test here is pure-Python and needs no Supabase credentials, except
test_optimize_around_locks_is_deterministic, which reads real course data and is
skipped automatically when SUPABASE_URL / SUPABASE_SERVICE_KEY are absent.

Run from backend/:
    pytest tests/test_cache.py -v -s
"""

import subprocess
import sys
import os
import time
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from scripts.optimizer import cache as optimizer_cache

# ── helpers ───────────────────────────────────────────────────────────────────

_FAKE_RESULT = {
    "variants":           [],
    "tight_timeline":     False,
    "quarters_available": 8,
    "quarters_needed":    7,
    "overflow_count":     0,
    "overflow_courses":   [],
    "extended_by":        0,
    "group_map":          {},
    "ap_credited_courses": [],
    "ap_units_awarded":   0,
    "choice_groups":      [],
    "cached":             False,
}

_SIMULATE_LATENCY = 0.15   # 150 ms — mimics a real optimizer run


def _make_key(major="BS-TEST", courses=None, grad="2029_spring"):
    return optimizer_cache.make_key(
        major_id           = major,
        completed_courses  = courses or [],
        graduation_quarter = grad,
        units_per_quarter  = 16,
    )


# Baseline whatif inputs.  Kept as plain builtins so it can be repr()'d into a
# subprocess by the PYTHONHASHSEED test below.
_WHATIF_BASE = {
    "planned_courses": {
        "2025_fall":   ["I&CSCI31", "MATH2A"],
        "2025_winter": ["I&CSCI32"],
    },
    "completed_courses": ["WRIT39A"],
    "graduation_year":   2028,
    "units_per_quarter": 16,
    "locked_course_ids": ["I&CSCI31"],
    "ap_scores":         {"CALCULUS AB": 4},
}


def _whatif_key(**overrides):
    """Baseline whatif key with selective field overrides."""
    return optimizer_cache.make_whatif_key(**{**_WHATIF_BASE, **overrides})


# ── Test 1: cached request is faster ─────────────────────────────────────────

def test_cache_hit_is_faster():
    """L1 hit returns the same result significantly faster than the optimizer path."""
    key = _make_key(major="BS-SPEED-TEST")
    optimizer_cache.invalidate_l1(key)

    generate_call_count = 0

    def slow_generate():
        nonlocal generate_call_count
        generate_call_count += 1
        time.sleep(_SIMULATE_LATENCY)
        return dict(_FAKE_RESULT)

    # ── Uncached path (simulates a real optimizer run) ────────────────────────
    t0 = time.perf_counter()
    hit = optimizer_cache.get_l1(key)
    if hit is None:
        result = slow_generate()
        optimizer_cache.set_l1(key, result)
    t_uncached = time.perf_counter() - t0

    # ── Cached path ───────────────────────────────────────────────────────────
    t1 = time.perf_counter()
    hit = optimizer_cache.get_l1(key)
    if hit is None:
        slow_generate()   # must NOT be reached
    t_cached = time.perf_counter() - t1

    optimizer_cache.invalidate_l1(key)

    print(f"\n  [latency] uncached={t_uncached*1000:.1f} ms  |  "
          f"cached={t_cached*1000:.3f} ms  |  "
          f"speedup={t_uncached/t_cached:.0f}×")

    assert hit is not None, "L1 cache returned None on second call"
    assert generate_call_count == 1, (
        f"generate() called {generate_call_count}× — expected exactly 1"
    )
    assert t_cached < t_uncached, "cache hit should be faster than the optimizer path"
    assert t_cached < 0.002, f"cache hit took {t_cached*1000:.3f} ms — expected < 2 ms"
    assert hit["tight_timeline"] == _FAKE_RESULT["tight_timeline"]
    assert hit["quarters_available"] == _FAKE_RESULT["quarters_available"]


# ── Test 2: different input bypasses the cache ────────────────────────────────

def test_different_input_bypasses_cache():
    """Two requests with different student profiles must not share a cache entry."""
    key_a = _make_key(major="BS-PROFILE-A", courses=["I&CSCI31"])
    key_b = _make_key(major="BS-PROFILE-B", courses=["I&CSCI31", "I&CSCI32"])
    optimizer_cache.invalidate_l1(key_a)
    optimizer_cache.invalidate_l1(key_b)

    result_a = {**_FAKE_RESULT, "quarters_available": 8}
    result_b = {**_FAKE_RESULT, "quarters_available": 6}

    optimizer_cache.set_l1(key_a, result_a)

    # key_a: cache hit — result_a returned
    hit_a = optimizer_cache.get_l1(key_a)
    # key_b: cache miss — different courses / major → new key
    hit_b = optimizer_cache.get_l1(key_b)

    optimizer_cache.set_l1(key_b, result_b)
    hit_b_after = optimizer_cache.get_l1(key_b)

    optimizer_cache.invalidate_l1(key_a)
    optimizer_cache.invalidate_l1(key_b)

    print(f"\n  [bypass] key_a hit={hit_a is not None}  |  "
          f"key_b initial miss={hit_b is None}  |  "
          f"key_b after set={hit_b_after is not None}")

    assert hit_a is not None, "key_a should hit the cache"
    assert hit_a["quarters_available"] == 8

    assert hit_b is None, (
        "key_b should miss the cache — different profile must not collide with key_a"
    )
    assert key_a != key_b, "SHA-256 keys for different inputs must differ"

    assert hit_b_after is not None
    assert hit_b_after["quarters_available"] == 6


# ── Test 3: stale results are not returned after invalidation ─────────────────

def test_invalidation_prevents_stale_results():
    """After course data changes, invalidating the L1 cache forces a fresh optimizer run."""
    key = _make_key(major="BS-STALE-TEST")
    optimizer_cache.invalidate_l1(key)

    stale_result  = {**_FAKE_RESULT, "quarters_available": 8, "cached": False}
    fresh_result  = {**_FAKE_RESULT, "quarters_available": 7, "cached": False}

    # Populate cache with the "pre-change" result
    optimizer_cache.set_l1(key, stale_result)
    assert optimizer_cache.get_l1(key)["quarters_available"] == 8, (
        "stale result should be in cache before invalidation"
    )

    # ── Simulate course data change ───────────────────────────────────────────
    # In production this would be triggered by a webhook or admin endpoint
    # whenever the courses / major_requirements tables are updated.
    optimizer_cache.invalidate_l1(key)

    # Cache must miss — no stale data served
    miss = optimizer_cache.get_l1(key)
    assert miss is None, (
        f"Expected cache miss after invalidation, got: {miss}"
    )

    # Re-run optimizer (simulated) and store fresh result
    optimizer_cache.set_l1(key, fresh_result)
    hit = optimizer_cache.get_l1(key)

    optimizer_cache.invalidate_l1(key)

    print(f"\n  [stale] stale evicted=True  |  "
          f"fresh result quarters_available={hit['quarters_available']}")

    assert hit is not None
    assert hit["quarters_available"] == 7, (
        "fresh result (7 quarters) should replace stale result (8 quarters)"
    )
    assert hit["quarters_available"] != stale_result["quarters_available"], (
        "post-invalidation result must differ from the stale snapshot"
    )


# ── Test 4: whatif key is invariant to input ordering ────────────────────────

def test_whatif_key_is_order_normalized():
    """Semantically identical grids must hash identically regardless of ordering.

    optimize_around_locks rebuilds its lists via remove/append, so its output
    ordering never matches what the client sent.  Without normalization the
    repeat-click case (re-submitting the returned plan) could never hit.
    """
    base = _whatif_key()

    reordered_quarters = _whatif_key(planned_courses={
        "2025_winter": ["I&CSCI32"],
        "2025_fall":   ["I&CSCI31", "MATH2A"],
    })
    reordered_courses = _whatif_key(planned_courses={
        "2025_fall":   ["MATH2A", "I&CSCI31"],
        "2025_winter": ["I&CSCI32"],
    })
    reordered_completed = _whatif_key(completed_courses=["WRIT39A"])
    # Locked ids absent from the grid are dropped by optimize_around_locks, so
    # they must not fragment the key space.
    ghost_lock = _whatif_key(locked_course_ids=["I&CSCI31", "GHOST999"])
    duplicate_lock = _whatif_key(locked_course_ids=["I&CSCI31", "I&CSCI31"])

    print(f"\n  [normalize] base={base[:12]}…")

    assert reordered_quarters == base, "quarter key order must not affect the key"
    assert reordered_courses  == base, "within-quarter course order must not affect the key"
    assert reordered_completed == base, "completed_courses order must not affect the key"
    assert ghost_lock == base, "a locked id absent from the grid is a no-op"
    assert duplicate_lock == base, "duplicate locked ids must collapse"


# ── Test 5: whatif key covers every input that reaches the computation ───────

def test_whatif_key_covers_every_computed_input():
    """Any change that can alter the plan must change the key.

    These are the collisions that would let one user's schedule be served to
    another — and because the whatif response is written into the grid by
    setPlannedCourses(), a wrong plan here becomes the user's saved schedule.
    """
    base = _whatif_key()

    variants = {
        "course moved to another quarter": _whatif_key(planned_courses={
            "2025_fall":   ["I&CSCI31"],
            "2025_winter": ["I&CSCI32", "MATH2A"],
        }),
        "course added": _whatif_key(planned_courses={
            "2025_fall":   ["I&CSCI31", "MATH2A", "I&CSCI6B"],
            "2025_winter": ["I&CSCI32"],
        }),
        "course removed": _whatif_key(planned_courses={
            "2025_fall":   ["I&CSCI31"],
            "2025_winter": ["I&CSCI32"],
        }),
        "completed_courses differs": _whatif_key(completed_courses=[]),
        "graduation_year differs":   _whatif_key(graduation_year=2027),
        "units_per_quarter differs": _whatif_key(units_per_quarter=20),
        "locked set differs":        _whatif_key(locked_course_ids=["I&CSCI32"]),
        "locks removed":             _whatif_key(locked_course_ids=[]),
        "ap score differs":          _whatif_key(ap_scores={"CALCULUS AB": 5}),
        "ap scores absent":          _whatif_key(ap_scores=None),
    }

    collisions = [label for label, key in variants.items() if key == base]
    print(f"\n  [coverage] {len(variants)} variants checked, "
          f"{len(collisions)} collisions")

    assert not collisions, (
        f"these inputs change the plan but not the key: {collisions}"
    )

    # Namespace: a whatif key can never equal a generate key.
    assert _whatif_key() != _make_key(), (
        "whatif and generate keys must live in separate namespaces"
    )


# ── Test 6: whatif L1 cannot evict generate entries ──────────────────────────

def test_whatif_l1_never_evicts_generate_entries():
    """The two L1 pools are independent.

    whatif keys embed the full course grid, so entries are largely single-use.
    Sharing one pool would let them push out generate entries — which do see
    cross-user reuse — by LRU.  This is the regression that motivates the split.
    """
    optimizer_cache.invalidate_l1()

    gen_key = _make_key(major="BS-EVICT-GUARD")
    optimizer_cache.set_l1(gen_key, {**_FAKE_RESULT, "quarters_available": 8})

    # Flood the whatif pool with twice its capacity.
    flood = optimizer_cache._L1_WHATIF_MAX * 2
    for i in range(flood):
        optimizer_cache.set_l1_whatif(f"whatif-flood-{i}", {"i": i})

    gen_hit    = optimizer_cache.get_l1(gen_key)
    whatif_len = len(optimizer_cache._l1_whatif)

    print(f"\n  [isolation] flooded {flood} whatif entries  |  "
          f"whatif pool={whatif_len}/{optimizer_cache._L1_WHATIF_MAX}  |  "
          f"generate entry survived={gen_hit is not None}")

    assert gen_hit is not None, (
        f"{flood} whatif writes evicted the generate entry — pools are not isolated"
    )
    assert gen_hit["quarters_available"] == 8
    assert whatif_len == optimizer_cache._L1_WHATIF_MAX, (
        "whatif pool must stay bounded by its own maxsize"
    )
    # Namespaces don't leak across pools.
    assert optimizer_cache.get_l1_whatif(gen_key) is None

    optimizer_cache.invalidate_l1()


# ── Test 7: invalidate_l1() clears both pools ────────────────────────────────

def test_invalidate_l1_clears_both_pools():
    """A data refresh invalidates every namespace — clearing one pool is a bug.

    Leaving the whatif pool warm after a courses/prereq update would keep serving
    plans built against superseded data.
    """
    gen_key = _make_key(major="BS-FLUSH-TEST")
    whatif_key = _whatif_key()

    optimizer_cache.set_l1(gen_key, dict(_FAKE_RESULT))
    optimizer_cache.set_l1_whatif(whatif_key, {"status": "ok", "plans": []})
    assert optimizer_cache.get_l1(gen_key) is not None
    assert optimizer_cache.get_l1_whatif(whatif_key) is not None

    optimizer_cache.invalidate_l1()   # None → clear everything

    gen_after    = optimizer_cache.get_l1(gen_key)
    whatif_after = optimizer_cache.get_l1_whatif(whatif_key)

    print(f"\n  [flush] generate cleared={gen_after is None}  |  "
          f"whatif cleared={whatif_after is None}")

    assert gen_after is None, "invalidate_l1(None) must clear the generate pool"
    assert whatif_after is None, (
        "invalidate_l1(None) must clear the whatif pool too — a warm whatif pool "
        "after a data refresh serves plans built against stale course data"
    )


# ── Test 8: keys are stable across processes (no hash randomization) ─────────

def _key_in_subprocess(hashseed: str) -> str:
    """Compute the baseline whatif key in a fresh interpreter with a given seed."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {_BACKEND!r})\n"
        "from scripts.optimizer import cache\n"
        f"print(cache.make_whatif_key(**{_WHATIF_BASE!r}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONHASHSEED": hashseed},
        check=True,
    )
    return proc.stdout.strip()


def test_whatif_key_stable_across_hash_seeds():
    """The key must not depend on PYTHONHASHSEED.

    L2 entries are shared across processes and restarts, so a key that varied
    with hash randomization would silently never hit.
    """
    k0 = _key_in_subprocess("0")
    k1 = _key_in_subprocess("1")

    print(f"\n  [hashseed] seed0={k0[:12]}…  seed1={k1[:12]}…")

    assert k0 and k1, "subprocess produced no key"
    assert k0 == k1, (
        "whatif key varies with PYTHONHASHSEED — L2 entries would never be reused"
    )
    assert k0 == _whatif_key(), "subprocess key differs from in-process key"


# ── Test 9: optimize_around_locks is deterministic (needs course data) ───────

_HAS_SUPABASE = bool(
    os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")
)


@pytest.mark.skipif(not _HAS_SUPABASE, reason="needs backend/.env for live Supabase")
def test_optimize_around_locks_is_deterministic():
    """Identical input must produce identical output — the premise of caching.

    All RNG in the whatif path is explicitly seeded (fixed seed_configs, plus
    random.Random(seed) in _perturb_unlocked and _whatif_optimize), and no scorer
    aggregates floats over set iteration, so results do not vary run to run.
    """
    from scripts.optimizer.hard_constraints import CoursePlan
    from scripts.optimizer.whatif import optimize_around_locks

    def _plan():
        return CoursePlan(
            major_id          = "BS-DETERMINISM-TEST",
            completed_courses = [],
            planned_courses   = {
                "2025_fall":   ["I&CSCI31", "MATH2A"],
                "2025_winter": ["I&CSCI32", "MATH2B"],
                "2025_spring": ["I&CSCI33"],
            },
            graduation_year   = 2028,
            units_per_quarter = 16,
        )

    first  = optimize_around_locks(_plan(), ["I&CSCI31"])
    second = optimize_around_locks(_plan(), ["I&CSCI31"])

    print(f"\n  [determinism] status={first.get('status')}  |  "
          f"identical={first == second}")

    assert first == second, (
        "optimize_around_locks returned different output for identical input — "
        "results are not cacheable"
    )
