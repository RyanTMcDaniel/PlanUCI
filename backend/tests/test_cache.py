"""
Tests for the optimizer L1 in-memory cache layer.

All three tests are pure-Python — no Supabase credentials required.

Run from backend/:
    pytest tests/test_cache.py -v -s
"""

import sys
import os
import time
from unittest.mock import MagicMock, patch

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
