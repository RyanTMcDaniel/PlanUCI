"""
Scrape RateMyProfessor and match records to UCI instructors — PRECISION FIRST.

WHY THIS WAS REWRITTEN
----------------------
The previous version fired an instructor's name at RMP's relevance search,
regex-scraped every legacyId off the results page, took the first three, and kept
whichever had the MOST RATINGS — with no comparison between the professor it
selected and the name it searched for.  RMP's search matches on any token,
including a bare middle initial, so "Michael A Carroll" returns the professor
literally named 'A' Pantano (Mathematics, 58 ratings), who outranks the real hits
on numRatings and wins.  Every UCI instructor with middle initial "A" was assigned
Pantano's ratings — 80 of them.  Overall 70.4% of instructors with an RMP rating
shared it with someone else (2,276 / 3,231), poisoning rmp_score for 68.7% of rows
in prof_course_features.csv.

THE RULES NOW
-------------
1. Full-name agreement, exact after normalization.  Both first AND last must match.
   Middle initials and suffixes are stripped from the UCI name; an RMP record whose
   firstName is a bare initial is REJECTED outright (unverifiable).
2. Department disambiguation.  When several RMP records pass the name check (there
   really are two different "Michael Green"s at UCI), the UCI instructor's
   department must pick exactly one.  If it cannot, the match is DROPPED.
3. Cardinality is enforced: one RMP record maps to AT MOST ONE ucinetid.  If two
   instructors both claim the same legacyId, BOTH are dropped — we do not guess.
4. Every drop is logged with a reason.  A missing rmp_score has a documented
   fallback (ml/data/build_features.py); a WRONG one silently poisons the blend.
   Precision over recall, deliberately.

The search results page embeds the full Teacher record (legacyId, firstName,
lastName, department, avgRating, avgDifficulty, numRatings, wouldTakeAgainPercent),
so one HTTP request per instructor does the matching.  The GraphQL API is only hit
for review comments, and only for a verified match.

Usage:
    python -m backend.scripts.fetch_rmp            # scrape → ml/data/rmp_matches.json
    python -m backend.scripts.fetch_rmp --load     # ...then write to Supabase
    python -m backend.scripts.fetch_rmp --limit 50 # smoke test
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

UCI_SCHOOL_ID = 1074
SEARCH_URL = "https://www.ratemyprofessors.com/search/professors/{sid}?q={q}"
GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
}
RATINGS_QUERY = {
    "query": (
        "query RatingsListQuery($count: Int! $id: ID! $courseFilter: String $cursor: String) "
        "{node(id: $id) {... on Teacher {ratings(first: $count, after: $cursor, "
        "courseFilter: $courseFilter) {edges {node {comment}}}}}}"
    )
}

OUT_JSON = Path(__file__).resolve().parents[2] / "ml" / "data" / "rmp_matches.json"
DROP_LOG = Path(__file__).parent / "rmp_dropped.log"

THROTTLE = 0.4

# Tokens that carry no identifying information in a UCI name.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "prof"}


# ── Name normalization ────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """lowercase, strip accents and punctuation, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z\s]", " ", s)      # drops periods, hyphens, apostrophes
    return re.sub(r"\s+", " ", s).strip()


def uci_name_parts(name: str) -> tuple[str, str] | None:
    """UCI 'Michael A Carroll' → ('michael', 'carroll').

    Middle initials (single-letter tokens) and suffixes are dropped — they are
    exactly what RMP's search matched on, and they identify nobody.
    """
    toks = [t for t in _norm(name).split() if t not in _SUFFIXES and len(t) > 1]
    if len(toks) < 2:
        return None
    return toks[0], toks[-1]


def search_query(name: str) -> str:
    """Query RMP with 'First Last', dropping middle names/initials but KEEPING
    punctuation (so "Kevin C O'leary" → "Kevin O'leary", not "kevin leary").

    Searching the raw name hurts recall badly: RMP's search does not find
    "Lisa Pearl" when asked for "Lisa Sue Pearl".
    """
    toks = [t for t in name.split()
            if _norm(t) not in _SUFFIXES and len(_norm(t)) > 1]
    if len(toks) < 2:
        return name
    return f"{toks[0]} {toks[-1]}"


def rmp_name_parts(first: str, last: str) -> tuple[str, str] | None:
    """RMP firstName/lastName → normalized pair, or None if unverifiable."""
    f, l = _norm(first), _norm(last)
    f = " ".join(t for t in f.split() if t not in _SUFFIXES)
    l = " ".join(t for t in l.split() if t not in _SUFFIXES)
    if not f or not l:
        return None
    if len(f.split()[0]) <= 1:
        return None          # RMP first name is a bare initial ('A' Pantano) — cannot verify
    return f.split()[0], l.split()[-1]


def dept_tokens(s: str) -> set[str]:
    stop = {"and", "of", "the", "not", "specified", "dept", "department", "sciences", "studies"}
    return {t for t in _norm(s).split() if t not in stop and len(t) > 2}


def dept_agrees(uci_dept: str, rmp_dept: str) -> bool:
    a, b = dept_tokens(uci_dept), dept_tokens(rmp_dept)
    return bool(a and b and (a & b))


# ── Scraping ──────────────────────────────────────────────────────────────────

def search_teachers(name: str) -> list[dict]:
    """Return every Teacher record embedded in the search results page."""
    url = SEARCH_URL.format(sid=UCI_SCHOOL_ID, q=requests.utils.quote(name))
    html = requests.get(url, headers=HEADERS, timeout=20).text
    out, seen = [], set()
    for m in re.finditer(r'"__typename":"Teacher"', html):
        start = html.rfind("{", 0, m.start())
        depth = 0
        for i in range(start, len(html)):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(html[start : i + 1])
                    except ValueError:
                        break
                    if "legacyId" in obj and "lastName" in obj and obj["legacyId"] not in seen:
                        seen.add(obj["legacyId"])
                        out.append(obj)
                    break
    return out


def get_reviews(prof_id: int, num_ratings: int) -> str:
    encoded = base64.b64encode(f"Teacher-{prof_id}".encode()).decode()
    h = {**HEADERS, "Referer": f"https://www.ratemyprofessors.com/ShowRatings.jsp?tid={prof_id}"}
    r = requests.post(
        GRAPHQL_URL,
        json={**RATINGS_QUERY, "variables": {"id": encoded, "count": min(max(num_ratings, 1), 100)}},
        headers=h,
        timeout=20,
    )
    edges = r.json()["data"]["node"]["ratings"]["edges"]
    comments = [e["node"]["comment"].strip() for e in edges if (e["node"].get("comment") or "").strip()]
    return " | ".join(comments)


# ── Matching ──────────────────────────────────────────────────────────────────

def match_one(instructor: dict, teachers: list[dict]) -> tuple[dict | None, str]:
    """Return (chosen_teacher, reason). chosen_teacher is None when we decline."""
    uci = uci_name_parts(instructor["name"])
    if uci is None:
        return None, "uci_name_unparseable"
    uci_first, uci_last = uci

    named = []
    for t in teachers:
        parts = rmp_name_parts(t.get("firstName", ""), t.get("lastName", ""))
        if parts is None:
            continue                      # bare-initial RMP record — the old bug's engine
        rf, rl = parts
        if rf == uci_first and rl == uci_last:
            named.append(t)

    if not named:
        return None, "no_full_name_match"

    if len(named) == 1:
        t = named[0]
        agrees = dept_agrees(instructor.get("department") or "", t.get("department") or "")
        return t, "name_exact" + ("_dept_confirmed" if agrees else "_dept_unconfirmed")

    # Several people genuinely share this name (there are two Michael Greens).
    # The department must single one out; otherwise we decline.
    by_dept = [t for t in named if dept_agrees(instructor.get("department") or "", t.get("department") or "")]
    if len(by_dept) == 1:
        return by_dept[0], "name_exact_dept_disambiguated"
    return None, f"ambiguous_{len(named)}_namesakes_dept_could_not_resolve"


# ── Main ──────────────────────────────────────────────────────────────────────

def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def load_instructors(client) -> list[dict]:
    rows, off = [], 0
    while True:
        b = (client.table("instructors").select("ucinetid,name,department")
             .range(off, off + 999).execute().data)
        rows += b
        if len(b) < 1000:
            break
        off += 1000
    return rows


def run(limit: int | None, do_load: bool) -> None:
    client = get_client()
    instructors = load_instructors(client)
    if limit:
        instructors = instructors[:limit]
    print(f"Instructors to match: {len(instructors)}\n")

    accepted: dict[str, dict] = {}      # ucinetid → record
    drops: list[str] = []
    reasons: dict[str, int] = defaultdict(int)
    claims: dict[int, list[str]] = defaultdict(list)   # legacyId → [ucinetid]

    for i, ins in enumerate(instructors, 1):
        uid, name = ins["ucinetid"], ins["name"]

        # Two passes: the trimmed "First Last" query has much better recall on RMP
        # (it will not find "Lisa Pearl" if asked for "Lisa Sue Pearl"), but the raw
        # name finds people whose RMP record carries the middle name too
        # ("Anna Kuntz Striedter").  Try trimmed, fall back to raw.  Precision is
        # unaffected either way — match_one still demands exact name agreement.
        queries = [search_query(name)]
        if name.strip() != queries[0]:
            queries.append(name)

        chosen, reason = None, "no_full_name_match"
        try:
            for q in queries:
                chosen, reason = match_one(ins, search_teachers(q))
                if chosen is not None:
                    break
                time.sleep(THROTTLE)
        except Exception as exc:
            reasons["search_error"] += 1
            drops.append(f"SEARCH_ERROR   | {uid:<14} | {name} | {exc}")
            time.sleep(THROTTLE)
            continue

        reasons[reason] += 1

        if chosen is None:
            drops.append(f"{reason.upper():<14} | {uid:<14} | {name}")
        else:
            claims[chosen["legacyId"]].append(uid)
            accepted[uid] = {
                "ucinetid": uid,
                "uci_name": name,
                "uci_department": ins.get("department"),
                "rmp_id": chosen["legacyId"],
                "rmp_first_name": chosen.get("firstName"),
                "rmp_last_name": chosen.get("lastName"),
                "rmp_department": chosen.get("department"),
                "overall_rating": chosen.get("avgRating"),
                "difficulty_rating": chosen.get("avgDifficulty"),
                "would_take_again_pct": (
                    chosen.get("wouldTakeAgainPercent")
                    if (chosen.get("wouldTakeAgainPercent") or -1) > 0 else None
                ),
                "num_ratings": chosen.get("numRatings"),
                "match_method": reason,
            }

        if i % 100 == 0:
            print(f"  [{i}/{len(instructors)}] accepted={len(accepted)} dropped={len(drops)}")
        time.sleep(THROTTLE)

    # ── Cardinality: one RMP record → at most one ucinetid.  Contested → drop all.
    contested = {rid: uids for rid, uids in claims.items() if len(uids) > 1}
    for rid, uids in contested.items():
        for uid in uids:
            rec = accepted.pop(uid, None)
            if rec:
                drops.append(
                    f"CONTESTED_RMP  | {uid:<14} | {rec['uci_name']} | "
                    f"rmp_id={rid} also claimed by {[u for u in uids if u != uid]}"
                )
        reasons["contested_rmp_id_dropped_all"] += len(uids)

    # Hard assertion — the invariant the old pipeline violated 2,276 times.
    final_claims: dict[int, list[str]] = defaultdict(list)
    for uid, rec in accepted.items():
        final_claims[rec["rmp_id"]].append(uid)
    bad = {r: u for r, u in final_claims.items() if len(u) > 1}
    if bad:
        raise AssertionError(f"cardinality violated: {len(bad)} rmp_id(s) map to >1 ucinetid: {bad}")
    print(f"\nCardinality OK — {len(final_claims)} rmp_ids → {len(accepted)} ucinetids, 1:1.")

    # ── Review text only for verified matches ────────────────────────────────
    print(f"\nFetching review text for {len(accepted)} verified matches...")
    for i, (uid, rec) in enumerate(accepted.items(), 1):
        try:
            rec["review_text"] = get_reviews(rec["rmp_id"], rec["num_ratings"] or 0) or None
        except Exception as exc:
            rec["review_text"] = None
            drops.append(f"REVIEW_ERROR   | {uid:<14} | {rec['uci_name']} | {exc}")
        if i % 100 == 0:
            print(f"  [{i}/{len(accepted)}] reviews fetched")
        time.sleep(THROTTLE)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(list(accepted.values()), indent=2))
    DROP_LOG.write_text("\n".join(drops) + ("\n" if drops else ""))

    print("\n" + "=" * 66)
    print("MATCH SUMMARY")
    print("=" * 66)
    total = len(instructors)
    print(f"  instructors processed : {total}")
    print(f"  ACCEPTED (1:1)        : {len(accepted)}  ({len(accepted)/total*100:.1f}%)")
    print(f"  DROPPED               : {total - len(accepted)}  ({(total-len(accepted))/total*100:.1f}%)")
    print("\n  Breakdown by reason:")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {r:<45} {n:>5}")
    if contested:
        print(f"\n  Contested RMP records (dropped entirely): {len(contested)}")
        worst = sorted(contested.items(), key=lambda kv: -len(kv[1]))[:5]
        for rid, uids in worst:
            print(f"    rmp_id={rid}: claimed by {len(uids)} instructors")
    print(f"\n  → {OUT_JSON}")
    print(f"  → {DROP_LOG}  ({len(drops)} entries)")

    if do_load:
        load_to_supabase(client, list(accepted.values()))


def load_to_supabase(client, records: list[dict]) -> None:
    """Replace rmp_reviews wholesale with the verified 1:1 matches."""
    print("\nLoading to Supabase (replacing rmp_reviews)...")
    cols = ["ucinetid", "overall_rating", "difficulty_rating", "would_take_again_pct",
            "num_ratings", "review_text", "rmp_id", "rmp_first_name", "rmp_last_name",
            "rmp_department", "match_method"]
    keep = {r["ucinetid"] for r in records}

    existing, off = [], 0
    while True:
        b = client.table("rmp_reviews").select("ucinetid").range(off, off + 999).execute().data
        existing += [r["ucinetid"] for r in b]
        if len(b) < 1000:
            break
        off += 1000
    stale = [u for u in existing if u not in keep]
    print(f"  deleting {len(stale)} rows whose match could not be verified")
    for i in range(0, len(stale), 100):
        client.table("rmp_reviews").delete().in_("ucinetid", stale[i : i + 100]).execute()

    rows = [{k: r.get(k) for k in cols} for r in records]
    print(f"  upserting {len(rows)} verified rows")
    for i in range(0, len(rows), 200):
        client.table("rmp_reviews").upsert(rows[i : i + 200], on_conflict="ucinetid").execute()
    print("  done.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--load", action="store_true", help="write results to Supabase")
    p.add_argument("--load-only", action="store_true",
                   help="skip the scrape; load an existing rmp_matches.json")
    a = p.parse_args()
    if a.load_only:
        load_to_supabase(get_client(), json.loads(OUT_JSON.read_text()))
    else:
        run(a.limit, a.load)
