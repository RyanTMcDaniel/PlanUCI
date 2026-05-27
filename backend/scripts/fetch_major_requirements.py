"""
Fetches major requirements from the Anteater API and populates the
major_requirements table in Supabase.

Run from repo root:
  cd /Users/ryanmcdaniel/Desktop/PlanUCI
  source backend/venv/bin/activate
  python3 backend/scripts/fetch_major_requirements.py

IMPORTANT: truncate non-GE rows before running:
  client.table('major_requirements').delete().neq('major_id', 'ALL_MAJORS').execute()
"""

import os
import time

import httpx
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

BASE_URL = "https://anteaterapi.com/v2/rest"
DELAY = 0.5       # seconds between API requests
BATCH_SIZE = 500  # rows per Supabase insert


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_json(url: str, retries: int = 3) -> dict:
    """GET url with retry-on-429 backoff."""
    for attempt in range(retries):
        resp = httpx.get(url, timeout=20, headers={"Origin": "https://anteaterapi.com"})
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"    429 rate-limited — waiting {wait}s before retry {attempt + 1}/{retries}...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after {retries} retries: {url}")


def fetch_all_programs() -> list[dict]:
    """Fetch all programs in a single request (API returns full list, no pagination)."""
    data = fetch_json(f"{BASE_URL}/programs/majors")
    if not data.get("ok"):
        raise RuntimeError(f"programs/majors returned ok=false: {data.get('message')}")
    items = data.get("data", [])
    return [p for p in items if p.get("division") == "Undergraduate"]


def infer_type(label: str, ancestors: list[str], from_school: bool) -> str:
    if from_school:
        return "GE"
    combined = " ".join([label] + ancestors).lower()
    if "elective" in combined:
        return "elective"
    return "required"


def flatten_req(
    req: dict,
    major_id: str,
    major_name: str,
    parent_group_id: str | None,
    ancestors: list[str],
    from_school: bool,
) -> list[dict]:
    req_type = req.get("requirementType")
    req_id   = req.get("requirementId", "")
    label    = req.get("label", "")

    if req_type == "Course":
        courses = req.get("courses", [])
        return [{
            "major_id":         major_id,
            "major_name":       major_name,
            "requirement_group": req_id,
            "requirement_type": infer_type(label, ancestors, from_school),
            "courses":          courses,
            "courses_needed":   req.get("courseCount", len(courses)),
            "group_name":       label,
            "parent_group":     parent_group_id,
            "waivable":         False,
        }]

    if req_type == "Group":
        rows = []
        for child in req.get("requirements", []):
            rows.extend(flatten_req(
                child, major_id, major_name, req_id, ancestors + [label], from_school
            ))
        return rows

    return []


def fetch_and_flatten(prog_id: str, spec_id: str | None, major_name: str) -> tuple[list[dict], bool]:
    url = f"{BASE_URL}/programs/major?programId={prog_id}"
    if spec_id:
        url += f"&specializationId={spec_id}"
    time.sleep(DELAY)

    try:
        data = fetch_json(url)
    except Exception as e:
        print(f"    ERROR fetching {spec_id or prog_id}: {e}")
        return [], False

    if not data.get("ok"):
        return [], False

    resp     = data["data"]
    major_id = spec_id if spec_id else prog_id
    rows: list[dict] = []

    for req in (resp.get("schoolRequirements") or {}).get("requirements", []):
        rows.extend(flatten_req(req, major_id, major_name, None, [], from_school=True))

    for req in (resp.get("requirements") or []):
        rows.extend(flatten_req(req, major_id, major_name, None, [], from_school=False))

    return rows, True


def main() -> None:
    client = get_client()

    # Confirm GE rows are intact
    ge_count = client.table("major_requirements").select("*", count="exact").eq("major_id", "ALL_MAJORS").execute()
    print(f"GE rows present (ALL_MAJORS): {ge_count.count}")

    # Clear all non-GE rows before repopulating
    print("Truncating non-GE rows...")
    client.table("major_requirements").delete().neq("major_id", "ALL_MAJORS").execute()

    print("\nFetching undergraduate program list (paginated)...")
    programs = fetch_all_programs()
    print(f"  {len(programs)} undergraduate programs found\n")

    all_rows: list[dict] = []
    empty: list[str] = []
    n_fetched = 0
    n_targets = sum(
        (len(p.get("specializations", [])) + 1) if p.get("specializations") else 1
        for p in programs
    )

    for prog in programs:
        prog_id   = prog["id"]
        prog_name = prog["name"]
        specs     = prog.get("specializations", [])
        # For programs with specializations, also fetch the parent (no spec) to get
        # specialization-specific requirement groups that only appear at the parent level.
        targets   = [(prog_id, None)] + [(prog_id, s) for s in specs] if specs else [(prog_id, None)]

        for pid, sid in targets:
            rows, ok = fetch_and_flatten(pid, sid, prog_name)
            label = f"{prog_name} ({sid or pid})"
            if not ok or not rows:
                empty.append(sid or pid)
            else:
                all_rows.extend(rows)
                n_fetched += 1

            if n_fetched % 10 == 0 and n_fetched > 0:
                print(f"  [{n_fetched}/{n_targets}] {len(all_rows)} rows so far — last: {label}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Total programs/specs fetched: {n_fetched}")
    print(f"Total requirement rows:       {len(all_rows)}")

    type_counts: dict[str, int] = {}
    for row in all_rows:
        t = row["requirement_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    print("Requirement type breakdown:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t:<10} {c}")

    if empty:
        print(f"\nMajors with empty/failed requirements ({len(empty)}):")
        for m in empty[:20]:
            print(f"  - {m}")
        if len(empty) > 20:
            print(f"  ... and {len(empty) - 20} more")

    # ── Insert (GE rows already preserved — do NOT delete ALL_MAJORS) ────────
    print(f"\nInserting {len(all_rows)} rows in batches of {BATCH_SIZE}...")
    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i : i + BATCH_SIZE]
        client.table("major_requirements").insert(batch).execute()
        pct = min(i + BATCH_SIZE, len(all_rows))
        print(f"  {pct}/{len(all_rows)}")

    final = client.table("major_requirements").select("*", count="exact").execute()
    print(f"\nFinal row count in table: {final.count}")
    print("Done.")


if __name__ == "__main__":
    main()
