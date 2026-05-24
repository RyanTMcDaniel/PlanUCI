"""
Fuzzy-match UCI instructors to rmp_reviews records.

The rmp_reviews table links to instructors via ucinetid (set at scrape time).
Instructors whose ucinetid already appears in rmp_reviews are considered matched.
For unlinked instructors, we fuzzy-match their name against the instructor names
that ARE linked to rmp_reviews records, then update rmp_reviews.ucinetid when
confident enough.

Score >= 88  → auto-link (update rmp_reviews.ucinetid to the unlinked instructor)
Score 60–87  → log to unmatched_review.txt for manual review
Score < 60   → log as "no candidate found"
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from rapidfuzz import fuzz, process
from supabase import create_client

load_dotenv()

MATCH_THRESHOLD = 88
REVIEW_THRESHOLD = 60
UNMATCHED_LOG = Path(__file__).parent / "unmatched_review.txt"


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def load_all(client, table, fields):
    rows = []
    offset = 0
    page_size = 1000
    while True:
        batch = (
            client.table(table)
            .select(fields)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def run():
    client = get_client()

    print("Loading instructors...")
    instructors = load_all(client, "instructors", "ucinetid,name")
    print(f"  {len(instructors)} instructors loaded.")

    print("Loading rmp_reviews...")
    rmp_reviews = load_all(client, "rmp_reviews", "id,ucinetid")
    print(f"  {len(rmp_reviews)} rmp_reviews loaded.\n")

    instructor_by_ucinetid = {r["ucinetid"]: r["name"] for r in instructors}
    rmp_by_ucinetid = {r["ucinetid"]: r["id"] for r in rmp_reviews}

    # Build candidates: rmp_reviews that have a resolvable instructor name.
    # Each entry is (rmp_review_id, ucinetid_currently_linked, instructor_name).
    candidates = [
        (r["id"], r["ucinetid"], instructor_by_ucinetid[r["ucinetid"]])
        for r in rmp_reviews
        if r["ucinetid"] in instructor_by_ucinetid
    ]
    candidate_names = [c[2] for c in candidates]

    matched = 0
    flagged = 0
    unmatched = 0
    log_lines = []

    for instructor in instructors:
        ucinetid = instructor["ucinetid"]
        name = instructor["name"]

        # Already linked via ucinetid — no action needed.
        if ucinetid in rmp_by_ucinetid:
            matched += 1
            continue

        if not candidate_names:
            unmatched += 1
            log_lines.append(f"NO CANDIDATE | {name}")
            continue

        result = process.extractOne(name, candidate_names, scorer=fuzz.WRatio)
        if result is None:
            unmatched += 1
            log_lines.append(f"NO CANDIDATE | {name}")
            continue

        best_name, score, idx = result
        best_rmp_id, best_linked_ucinetid, _ = candidates[idx]

        if score >= MATCH_THRESHOLD:
            # Reassign the rmp_review to the unlinked instructor.
            # This replaces the current ucinetid link on the rmp_reviews row.
            client.table("rmp_reviews").update({"ucinetid": ucinetid}).eq("id", best_rmp_id).execute()
            matched += 1
            print(
                f"  MATCHED: {name!r} → {best_name!r} "
                f"(score={score:.1f}, rmp_id={best_rmp_id}, "
                f"reassigned from {best_linked_ucinetid})"
            )
        elif score >= REVIEW_THRESHOLD:
            flagged += 1
            log_lines.append(
                f"REVIEW | instructor: {name} | best candidate: {best_name} | score: {score:.1f}"
            )
        else:
            unmatched += 1
            log_lines.append(
                f"NO CANDIDATE | instructor: {name} | best candidate: {best_name} | score: {score:.1f}"
            )

    with open(UNMATCHED_LOG, "w") as f:
        f.write("\n".join(log_lines))
        if log_lines:
            f.write("\n")

    if log_lines:
        print(f"\nLogged {len(log_lines)} entries to {UNMATCHED_LOG.name}")

    print(f"\n--- Summary ---")
    print(f"Matched:            {matched}")
    print(f"Flagged for review: {flagged}")
    print(f"Unmatched:          {unmatched}")


if __name__ == "__main__":
    run()
