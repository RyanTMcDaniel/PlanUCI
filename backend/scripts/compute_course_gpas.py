"""
Compute avg_gpa for every course in the courses table using grade_distributions.

Phase 1 — single paginated scan of grade_distributions into memory:
  For each row, accumulate (sum of weighted points, count of letter-graded students).
  GPA = (a*4 + b*3 + c*2 + d*1) / (a+b+c+d+f)

Phase 2 — update courses table:
  One update call per course; reconnect the client every RECONNECT_EVERY requests
  to avoid Supabase's HTTP/2 stream-count limit (~10k streams per connection).

Usage (from repo root):
  cd /Users/ryanmcdaniel/PlanUCI
  source backend/venv/bin/activate
  python3 backend/scripts/compute_course_gpas.py
"""
import os
import time
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

PAGE = 1000          # rows per Supabase page
RECONNECT_EVERY = 400  # reconnect client after this many UPDATE calls
PROGRESS_EVERY = 500


def make_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Phase 1: build GPA lookup from grade_distributions ────────────────────────

def build_gpa_lookup(client) -> dict[str, tuple[float, int]]:
    """
    Returns {course_id: (total_points, total_letter_graded_students)}.
    course_id is stored without spaces (the canonical format in grade_distributions).
    """
    lookup: dict[str, tuple[float, int]] = {}
    offset = 0
    total_rows = 0

    print("Phase 1: scanning grade_distributions...")
    while True:
        rows = (
            client.table("grade_distributions")
            .select("course_id,grade_a_count,grade_b_count,grade_c_count,grade_d_count,grade_f_count")
            .range(offset, offset + PAGE - 1)
            .execute()
            .data
        )
        if not rows:
            break
        for r in rows:
            cid = (r.get("course_id") or "").replace(" ", "")
            if not cid:
                continue
            a = r.get("grade_a_count") or 0
            b = r.get("grade_b_count") or 0
            c = r.get("grade_c_count") or 0
            d = r.get("grade_d_count") or 0
            f = r.get("grade_f_count") or 0
            pts, tot = lookup.get(cid, (0.0, 0))
            lookup[cid] = (pts + a*4.0 + b*3.0 + c*2.0 + d*1.0, tot + a+b+c+d+f)
        total_rows += len(rows)
        if len(rows) < PAGE:
            break
        offset += PAGE

    print(f"  Scanned {total_rows} rows → {len(lookup)} distinct course_ids with letter grades")
    return lookup


# ── Phase 2: fetch course IDs and write GPAs ──────────────────────────────────

def fetch_course_ids(client) -> list[str]:
    ids = []
    offset = 0
    while True:
        rows = client.table("courses").select("id").range(offset, offset + PAGE - 1).execute().data
        if not rows:
            break
        ids.extend(r["id"] for r in rows)
        if len(rows) < PAGE:
            break
        offset += PAGE
    return ids


def run():
    client = make_client()
    gpa_lookup = build_gpa_lookup(client)

    print("\nFetching course IDs from courses table...")
    course_ids = fetch_course_ids(client)
    print(f"  {len(course_ids)} courses\n")

    print("Phase 2: updating courses.avg_gpa...")
    n_with_gpa = 0
    n_null = 0
    n_updated = 0
    requests_since_reconnect = 0

    for i, cid in enumerate(course_ids, 1):
        norm = cid.replace(" ", "")
        pts, tot = gpa_lookup.get(norm, (0.0, 0))

        if tot > 0:
            gpa = round(pts / tot, 2)
            n_with_gpa += 1
        else:
            gpa = None
            n_null += 1

        # Reconnect before hitting the HTTP/2 stream limit
        if requests_since_reconnect >= RECONNECT_EVERY:
            client = make_client()
            requests_since_reconnect = 0

        client.table("courses").update({"avg_gpa": gpa}).eq("id", cid).execute()
        n_updated += 1
        requests_since_reconnect += 1

        if i % PROGRESS_EVERY == 0:
            pct = 100 * i // len(course_ids)
            print(f"  [{i}/{len(course_ids)} {pct}%] {n_with_gpa} with GPA, {n_null} null")

    print(f"\n=== Final Summary ===")
    print(f"  Total courses processed : {len(course_ids)}")
    print(f"  Updated in courses table: {n_updated}")
    print(f"  With GPA data           : {n_with_gpa}")
    print(f"  Null (no grade data)    : {n_null}")


if __name__ == "__main__":
    run()
