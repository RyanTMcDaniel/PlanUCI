"""Regenerate the COLD-scenario major pool.

Probes candidate major_ids against a RUNNING backend (start it with
LOAD_TEST_MODE=1 so no writes leak) and prints the ones that return HTTP 200 with a
non-empty `variants` array — i.e. the optimizer does real work.  Paste the printed
list into major_pool.py.

Run from backend/:  venv/bin/python loadtest/validate_pool.py [--n 8] [--url ...]
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

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8001")
ap.add_argument("--n", type=int, default=8, help="how many good majors to keep")
args = ap.parse_args()

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
ids = sorted({r["major_id"] for r in
              sb.table("major_requirements").select("major_id").execute().data
              if r.get("major_id")})
# A spread across the catalogue; always include the known-good ICS major first.
candidates = ["BS-201G"] + ids[::6]
seen, ordered = set(), []
for c in candidates:
    if c not in seen:
        seen.add(c)
        ordered.append(c)

EP = f"{args.url.rstrip('/')}/optimizer/generate"


def body(mid, i):
    return {
        "major_id": mid, "completed_courses": [], "graduation_quarter": "2029_spring",
        "units_per_quarter": 16, "waived_ges": [], "ap_scores": {f"_probe_{i}": 0},
        "start_quarter": "2026_fall", "seed_courses": [], "seed_only": False,
    }


good = []
for i, mid in enumerate(ordered):
    try:
        r = requests.post(EP, json=body(mid, i), timeout=120)
        nv = len(r.json().get("variants", [])) if r.status_code == 200 else 0
        ok = r.status_code == 200 and nv > 0
        print(f"  {mid:10s} -> {r.status_code}  variants={nv}  {'KEEP' if ok else 'skip'}")
        if ok:
            good.append(mid)
    except Exception as exc:
        print(f"  {mid:10s} -> EXC {exc}")
    if len(good) >= args.n:
        break

print("\nMAJOR_POOL =", json.dumps(good, indent=4))
