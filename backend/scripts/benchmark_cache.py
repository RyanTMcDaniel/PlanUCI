"""
Cache benchmark for POST /optimizer/generate.

Phases
------
  COLD   — L1 and L2 both empty; optimizer runs end-to-end (3-8 s typical).
           Each cold rep uses a FRESH nonce so it is a genuine miss.
  WARM   — Same payload repeated; hits the in-memory L1 cache (cached=True).

Guaranteeing a cold start
-------------------------
  A timestamp+counter-derived nonce is embedded in `ap_scores` (harmless — the
  server looks it up in the DB, finds nothing, and skips it).  This gives a
  unique SHA-256 cache key that cannot already exist in either L1 or L2.  The
  script also proactively deletes any matching L2 (Supabase) row before each
  cold call, and (with --cleanup) deletes every nonce row it created afterward.

Statistics
----------
  Reports n, p50 (median), and p95 for both cold and warm — not a single
  sample — and the measured speedup = cold_p50 / warm_p50.

Usage
-----
  # From backend/:
  python3 scripts/benchmark_cache.py [--url http://localhost:8001] \
      [--reps 15] [--warm-reps 30] [--cleanup]
"""

import argparse
import os
import sys
import time

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND, ".env"))

import requests
from supabase import create_client
from scripts.optimizer import cache as optimizer_cache

# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8001",
                    help="Base URL of the running FastAPI server")
parser.add_argument("--reps", type=int, default=15,
                    help="Number of COLD reps (each a fresh nonce → real solve)")
parser.add_argument("--warm-reps", type=int, default=30,
                    help="Number of WARM reps (repeated identical → L1 hit)")
parser.add_argument("--cleanup", action="store_true",
                    help="Delete every nonce optimizer_cache row created by this run")
args = parser.parse_args()
BASE = args.url.rstrip("/")
ENDPOINT = f"{BASE}/optimizer/generate"

# ── Payload template ──────────────────────────────────────────────────────────
# Real ICS major (BS-201G = B.S. Informatics) with a handful of completed lower-div
# courses.  A unique nonce in ap_scores guarantees this exact combination has never
# been cached before, making each cold call genuinely cold.

_RUN_TAG = int(time.time())


def make_payload(nonce_key: str) -> dict:
    return {
        "major_id":           "BS-201G",
        "completed_courses":  ["I&CSCI31", "I&CSCI32", "I&CSCI33", "MATH2A"],
        "graduation_quarter": "2029_spring",
        "units_per_quarter":  16,
        "waived_ges":         [],
        "ap_scores":          {nonce_key: 0},   # unique nonce → unique cache key
        "start_quarter":      "2026_fall",
        "seed_courses":       [],
        "seed_only":          False,
    }


def key_for(payload: dict) -> str:
    return optimizer_cache.make_key(
        major_id           = payload["major_id"],
        completed_courses  = payload["completed_courses"],
        graduation_quarter = payload["graduation_quarter"],
        units_per_quarter  = payload["units_per_quarter"],
        waived_ges         = payload["waived_ges"],
        ap_scores          = payload["ap_scores"],
        start_quarter      = payload["start_quarter"],
    )


# ── Supabase (for L2 eviction + cleanup) ──────────────────────────────────────

_sb = None
_supabase_url = os.getenv("SUPABASE_URL")
_supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
if _supabase_url and _supabase_key:
    _sb = create_client(_supabase_url, _supabase_key)


def evict_l2(cache_key: str) -> None:
    if _sb is None:
        return
    try:
        _sb.table("optimizer_cache").delete().eq("cache_key", cache_key).execute()
    except Exception as exc:
        print(f"[setup] Warning: could not evict L2 {cache_key[:12]}…: {exc}")


# ── HTTP helper ───────────────────────────────────────────────────────────────

def call(payload: dict) -> tuple[float, bool, int]:
    """Return (elapsed_seconds, cached_flag, n_variants).  Raises on non-200."""
    t0 = time.perf_counter()
    resp = requests.post(ENDPOINT, json=payload, timeout=120)
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    return elapsed, bool(body.get("cached", False)), len(body.get("variants", []))


# ── Stats ─────────────────────────────────────────────────────────────────────

def percentile(sorted_ms: list[float], pct: float) -> float:
    """Nearest-rank percentile on an already-sorted list (ms)."""
    if not sorted_ms:
        return float("nan")
    k = max(0, min(len(sorted_ms) - 1, int(round((pct / 100.0) * len(sorted_ms) + 0.5)) - 1))
    return sorted_ms[k]


def report(label: str, times_s: list[float]) -> dict:
    ms = sorted(t * 1000 for t in times_s)
    stats = {
        "n":   len(ms),
        "p50": percentile(ms, 50),
        "p95": percentile(ms, 95),
        "min": ms[0] if ms else float("nan"),
        "max": ms[-1] if ms else float("nan"),
    }
    print(f"  {label:6s}  n={stats['n']:<3d}  "
          f"p50={stats['p50']:>9.1f} ms   p95={stats['p95']:>9.1f} ms   "
          f"min={stats['min']:>8.1f}   max={stats['max']:>8.1f}")
    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

print("=" * 72)
print("PlanUCI /optimizer/generate cache benchmark  (reps + p50/p95)")
print("=" * 72)
print(f"  endpoint  : {ENDPOINT}")
print(f"  cold reps : {args.reps}    warm reps : {args.warm_reps}")
print(f"  L2 evict  : {'enabled' if _sb else 'DISABLED (no SUPABASE_SERVICE_KEY)'}")
print()

created_keys: list[str] = []

# ── Phase COLD ────────────────────────────────────────────────────────────────
print("Phase 1 — COLD (fresh nonce each rep → L1 miss + L2 miss → real solve)")
print("-" * 72)
cold_times: list[float] = []
for i in range(args.reps):
    nonce = f"_bench_{_RUN_TAG}_{i}"
    payload = make_payload(nonce)
    ck = key_for(payload)
    created_keys.append(ck)
    evict_l2(ck)  # defensive: guarantee truly cold even if key somehow existed
    try:
        elapsed, cached, nv = call(payload)
    except Exception as exc:
        print(f"  cold-{i:<2d}  ERROR: {exc}")
        continue
    tag = "" if not cached else "  !! UNEXPECTED cached=True on cold"
    print(f"  cold-{i:<2d}  {elapsed*1000:>9.1f} ms   cached={cached}   variants={nv}{tag}")
    cold_times.append(elapsed)

# ── Phase WARM ────────────────────────────────────────────────────────────────
print()
print("Phase 2 — WARM (one payload, repeated → L1 in-memory cache hits)")
print("-" * 72)
warm_nonce = f"_bench_{_RUN_TAG}_warm"
warm_payload = make_payload(warm_nonce)
warm_key = key_for(warm_payload)
created_keys.append(warm_key)
evict_l2(warm_key)

# Prime: first call is cold (populates L1 + L2); not counted in warm stats.
try:
    prime_elapsed, prime_cached, _ = call(warm_payload)
    print(f"  prime   {prime_elapsed*1000:>9.1f} ms   cached={prime_cached}  (not counted)")
except Exception as exc:
    print(f"  prime   ERROR: {exc}")

warm_times: list[float] = []
for i in range(args.warm_reps):
    try:
        elapsed, cached, nv = call(warm_payload)
    except Exception as exc:
        print(f"  warm-{i:<2d}  ERROR: {exc}")
        continue
    if not cached:
        print(f"  warm-{i:<2d}  {elapsed*1000:>9.1f} ms   cached=False  !! expected a cache hit")
    warm_times.append(elapsed)
print(f"  (ran {len(warm_times)} warm reps; all cached=True unless flagged above)")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("Summary")
print("=" * 72)
cold_stats = report("COLD", cold_times)
warm_stats = report("WARM", warm_times)
print()
if cold_stats["n"] and warm_stats["n"] and warm_stats["p50"] > 0:
    speedup = cold_stats["p50"] / warm_stats["p50"]
    print(f"  Measured speedup (cold p50 / warm p50): {speedup:,.0f}×")
    print(f"  Claimed on resume:                      460×  (7.4 s → ~16 ms)")
print()
print("  What this measures: L1 cache-hit latency vs a cold optimizer solve on a")
print("  LOCAL server instance. NOT a Railway/Supabase throughput figure.")
print()

# ── Cleanup ───────────────────────────────────────────────────────────────────
if args.cleanup and _sb is not None:
    deleted = 0
    for ck in created_keys:
        try:
            _sb.table("optimizer_cache").delete().eq("cache_key", ck).execute()
            deleted += 1
        except Exception as exc:
            print(f"  [cleanup] Warning: could not delete {ck[:12]}…: {exc}")
    print(f"  [cleanup] Deleted {deleted}/{len(created_keys)} nonce optimizer_cache rows.")
elif args.cleanup:
    print("  [cleanup] Skipped — no Supabase client (SUPABASE_SERVICE_KEY unset).")
else:
    print(f"  [note] {len(created_keys)} nonce rows left in optimizer_cache "
          f"(re-run with --cleanup to remove).")
