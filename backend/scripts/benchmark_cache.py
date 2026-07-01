"""
Cache benchmark for POST /optimizer/generate.

Phases
------
  COLD   — L1 and L2 both empty; optimizer runs end-to-end (3-8 s typical).
  WARM-1 — Same payload, second request; hits the in-memory L1 cache.
  WARM-2 — Same payload, third request; hits L1 again.

Guaranteeing a cold start
-------------------------
  A timestamp-derived nonce is embedded in `ap_scores` (harmless — the server
  looks it up in the DB, finds nothing, and skips it).  This gives a unique
  SHA-256 cache key that cannot already exist in either L1 or L2.  The script
  also proactively deletes any matching L2 (Supabase) row before the first call.

Usage
-----
  # From backend/:
  python3 scripts/benchmark_cache.py [--url http://localhost:8001]
"""

import argparse
import json
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
args = parser.parse_args()
BASE = args.url.rstrip("/")
ENDPOINT = f"{BASE}/optimizer/generate"

# ── Payload ───────────────────────────────────────────────────────────────────
# Real ICS major (BS-201G = B.S. Informatics) with a handful of completed lower-div
# courses.  The nonce in ap_scores guarantees this exact combination has never been
# cached before, making the first call genuinely cold.

NONCE_KEY = f"_benchmark_nonce_{int(time.time())}"

PAYLOAD = {
    "major_id":           "BS-201G",
    "completed_courses":  ["I&CSCI31", "I&CSCI32", "I&CSCI33", "MATH2A"],
    "graduation_quarter": "2029_spring",
    "units_per_quarter":  16,
    "waived_ges":         [],
    "ap_scores":          {NONCE_KEY: 0},   # unique nonce → unique cache key
    "start_quarter":      "2026_fall",
    "seed_courses":       [],
    "seed_only":          False,
}

# ── Compute cache key and evict L2 proactively ────────────────────────────────

cache_key = optimizer_cache.make_key(
    major_id           = PAYLOAD["major_id"],
    completed_courses  = PAYLOAD["completed_courses"],
    graduation_quarter = PAYLOAD["graduation_quarter"],
    units_per_quarter  = PAYLOAD["units_per_quarter"],
    waived_ges         = PAYLOAD["waived_ges"],
    ap_scores          = PAYLOAD["ap_scores"],
    start_quarter      = PAYLOAD["start_quarter"],
)

print("=" * 64)
print("PlanUCI /optimizer/generate cache benchmark")
print("=" * 64)
print(f"  endpoint : {ENDPOINT}")
print(f"  major    : {PAYLOAD['major_id']}")
print(f"  grad     : {PAYLOAD['graduation_quarter']}")
print(f"  nonce    : {NONCE_KEY}")
print(f"  key      : {cache_key[:24]}…")
print()

# Evict from Supabase L2 (no-op if not present) so the cold call is truly cold.
try:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if supabase_url and supabase_key:
        sb = create_client(supabase_url, supabase_key)
        sb.table("optimizer_cache").delete().eq("cache_key", cache_key).execute()
        print("[setup] L2 Supabase entry evicted (or was absent)")
    else:
        print("[setup] Warning: SUPABASE_SERVICE_KEY not set — skipping L2 eviction")
except Exception as exc:
    print(f"[setup] Warning: could not clear L2 cache: {exc}")

# ── Helper ────────────────────────────────────────────────────────────────────

def call(label: str) -> float:
    t0 = time.perf_counter()
    resp = requests.post(ENDPOINT, json=PAYLOAD, timeout=120)
    elapsed = time.perf_counter() - t0

    if resp.status_code != 200:
        print(f"  [{label}] ERROR {resp.status_code}: {resp.text[:200]}")
        return float("nan")

    body = resp.json()
    cached_flag = body.get("cached", "?")
    n_variants  = len(body.get("variants", []))
    print(f"  [{label}]  {elapsed*1000:>9.1f} ms   cached={cached_flag}   variants={n_variants}")
    return elapsed


def stats(times: list[float], label: str):
    ms = [t * 1000 for t in times]
    print(f"  {label:8s}  min={min(ms):>8.1f} ms   max={max(ms):>8.1f} ms   avg={sum(ms)/len(ms):>8.1f} ms")


# ── Phase: COLD ───────────────────────────────────────────────────────────────

print()
print("Phase 1 — COLD (L1 miss + L2 miss → optimizer runs from scratch)")
print("-" * 64)
cold_times = [call("cold-1")]

# ── Phase: WARM (L1 hits) ─────────────────────────────────────────────────────

print()
print("Phase 2 — WARM (L1 in-memory cache hits)")
print("-" * 64)
warm_times = [
    call("warm-1"),
    call("warm-2"),
]

# ── Summary ───────────────────────────────────────────────────────────────────

print()
print("=" * 64)
print("Summary")
print("=" * 64)
stats(cold_times,  "COLD")
stats(warm_times,  "WARM")
print()
if cold_times[0] and warm_times:
    avg_warm = sum(warm_times) / len(warm_times)
    print(f"  Speedup (cold avg / warm avg): {cold_times[0] / avg_warm:,.0f}×")
print()
