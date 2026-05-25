"""
Analyze course offering patterns from historical terms data.

Main entry point:
    fetch_and_analyze() → dict[course_id, pattern]
        Pulls all courses from Supabase, computes offering patterns, and writes
        ml/data/offering_patterns.json.

Helper (importable by plan_generator):
    is_likely_offered(course_id, quarter) → (bool, reason_str)
        Loads from the JSON file lazily.  Returns (True, reason) when no data
        exists — we assume possible rather than block.

Pattern schema per course:
    offered_fall:         bool   (appeared in ≥ 2 distinct fall years since 2021)
    offered_winter:       bool   (appeared in ≥ 2 distinct winter years since 2021)
    offered_spring:       bool   (appeared in ≥ 2 distinct spring years since 2021)
    offering_confidence:  str    "high" (3+ active years) | "low" (1-2) | "unknown"
    last_offered:         str    most recent term string across all time
"""

import json
import os

from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

_HERE   = os.path.dirname(os.path.abspath(__file__))
_OUTPUT = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "ml", "data", "offering_patterns.json")
)

RECENT_CUTOFF = 2021  # earliest year counted toward "last 5 years"
MIN_OFFERINGS = 2     # distinct years needed to mark a quarter as "offered"

_QUARTER_SORT = {"Winter": 0, "Spring": 1, "Fall": 3}


# ── Term parsing ──────────────────────────────────────────────────────────────

def _parse_term(term: str) -> tuple[int, str] | None:
    """'2024 Fall' → (2024, 'Fall').  Returns None for malformed or unknown."""
    parts = term.split(" ", 1)
    if len(parts) != 2:
        return None
    try:
        year = int(parts[0])
    except ValueError:
        return None
    return year, parts[1]


def _is_summer(quarter_name: str) -> bool:
    return quarter_name.startswith("Summer")


def _term_sort_key(term: str) -> tuple[int, int]:
    parsed = _parse_term(term)
    if not parsed:
        return (0, 99)
    year, q = parsed
    return (year, _QUARTER_SORT.get(q, 2))  # unknown/summer sorts mid-year


# ── Per-course analysis ───────────────────────────────────────────────────────

def analyze(course_id: str, terms: list[str]) -> dict:
    """Return offering pattern dict for one course."""
    valid_terms = [t for t in terms if _parse_term(t)]
    last_offered = (
        max(valid_terms, key=_term_sort_key) if valid_terms else None
    )

    # Recent, non-summer (year, quarter_name) pairs
    recent: list[tuple[int, str]] = []
    for t in terms:
        parsed = _parse_term(t)
        if not parsed:
            continue
        year, q = parsed
        if year < RECENT_CUTOFF or _is_summer(q):
            continue
        if q not in ("Fall", "Winter", "Spring"):
            continue
        recent.append((year, q))

    fall_years   = {y for y, q in recent if q == "Fall"}
    winter_years = {y for y, q in recent if q == "Winter"}
    spring_years = {y for y, q in recent if q == "Spring"}
    active_years = fall_years | winter_years | spring_years

    offered_fall   = len(fall_years)   >= MIN_OFFERINGS
    offered_winter = len(winter_years) >= MIN_OFFERINGS
    offered_spring = len(spring_years) >= MIN_OFFERINGS

    n = len(active_years)
    confidence = "high" if n >= 3 else ("low" if n >= 1 else "unknown")

    return {
        "offered_fall":        offered_fall,
        "offered_winter":      offered_winter,
        "offered_spring":      offered_spring,
        "offering_confidence": confidence,
        "last_offered":        last_offered,
    }


# ── Supabase fetch + write ────────────────────────────────────────────────────

def fetch_and_analyze() -> dict[str, dict]:
    """Pull every course from Supabase, compute patterns, and write the JSON."""
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    patterns: dict[str, dict] = {}
    offset, PAGE = 0, 1000

    print("Fetching courses from Supabase...")
    while True:
        rows = (
            client.table("courses")
            .select("id,terms")
            .range(offset, offset + PAGE - 1)
            .execute()
            .data
        )
        for r in rows:
            patterns[r["id"]] = analyze(r["id"], r.get("terms") or [])
        if len(rows) < PAGE:
            break
        offset += PAGE

    print(f"  Analyzed {len(patterns)} courses")
    os.makedirs(os.path.dirname(_OUTPUT), exist_ok=True)
    with open(_OUTPUT, "w") as fh:
        json.dump(patterns, fh, indent=2, sort_keys=True)
    print(f"  Written → {_OUTPUT}")

    return patterns


# ── Lazy cache for is_likely_offered ─────────────────────────────────────────

_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            with open(_OUTPUT) as fh:
                _cache = json.load(fh)
        except FileNotFoundError:
            _cache = {}
    return _cache


def is_likely_offered(course_id: str, quarter: str) -> tuple[bool, str]:
    """Return (likely, reason) for whether course_id is offered in quarter.

    quarter: 'fall', 'winter', or 'spring' (case-insensitive).
    When confidence is 'unknown' the function returns (True, reason) —
    missing data is not treated as a block.
    """
    q = quarter.lower()
    key_map = {
        "fall":   "offered_fall",
        "winter": "offered_winter",
        "spring": "offered_spring",
    }
    if q not in key_map:
        return True, f"Unrecognized quarter {quarter!r}"

    entry = _load().get(course_id)
    if not entry or entry.get("offering_confidence") == "unknown":
        return True, "No recent offering data — not blocked"

    if entry.get(key_map[q]):
        return True, f"Historically offered in {q.capitalize()}"

    offered_in = [
        label
        for label, k in [("Fall", "offered_fall"), ("Winter", "offered_winter"), ("Spring", "offered_spring")]
        if entry.get(k)
    ]
    if offered_in:
        return False, f"Only offered {'/'.join(offered_in)} historically"
    return False, "No regular offering quarter found in recent data"


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    patterns = fetch_and_analyze()

    # ── Summary counts ────────────────────────────────────────────────────────
    total    = len(patterns)
    unknown  = sum(1 for p in patterns.values() if p["offering_confidence"] == "unknown")
    high_conf = sum(1 for p in patterns.values() if p["offering_confidence"] == "high")
    low_conf  = sum(1 for p in patterns.values() if p["offering_confidence"] == "low")

    fall_only   = [cid for cid, p in patterns.items()
                   if p["offered_fall"] and not p["offered_winter"] and not p["offered_spring"]]
    winter_only = [cid for cid, p in patterns.items()
                   if p["offered_winter"] and not p["offered_fall"] and not p["offered_spring"]]
    spring_only = [cid for cid, p in patterns.items()
                   if p["offered_spring"] and not p["offered_fall"] and not p["offered_winter"]]
    all_quarters = [cid for cid, p in patterns.items()
                    if p["offered_fall"] and p["offered_winter"] and p["offered_spring"]]

    print()
    print(f"Total courses analyzed:   {total}")
    print(f"  Confidence high:        {high_conf}")
    print(f"  Confidence low:         {low_conf}")
    print(f"  Unknown (no recent data): {unknown}")
    print()
    print(f"Offering pattern breakdown:")
    print(f"  Fall-only:              {len(fall_only)}")
    print(f"  Winter-only:            {len(winter_only)}")
    print(f"  Spring-only:            {len(spring_only)}")
    print(f"  All three quarters:     {len(all_quarters)}")
    print()

    # ── Spring-only validation ────────────────────────────────────────────────
    if len(spring_only) < 3:
        print(f"Only {len(spring_only)} spring-only courses found — skipping is_likely_offered test")
    else:
        sample = spring_only[:3]
        print(f"Spring-only courses (sample of 3): {sample}")
        print()
        all_pass = True
        for cid in sample:
            p = patterns[cid]
            print(f"  {cid}  (confidence={p['offering_confidence']}, "
                  f"last_offered={p['last_offered']!r})")
            for q in ("fall", "winter"):
                likely, reason = is_likely_offered(cid, q)
                status = "PASS" if not likely else "FAIL"
                print(f"    is_likely_offered({q!r}) → ({likely}, {reason!r})  [{status}]")
                if likely:
                    all_pass = False
            likely_sp, reason_sp = is_likely_offered(cid, "spring")
            status = "PASS" if likely_sp else "FAIL"
            print(f"    is_likely_offered('spring') → ({likely_sp}, {reason_sp!r})  [{status}]")
            if not likely_sp:
                all_pass = False
            print()

        print(f"Spring-only is_likely_offered checks: [{'PASS' if all_pass else 'FAIL'}]")
