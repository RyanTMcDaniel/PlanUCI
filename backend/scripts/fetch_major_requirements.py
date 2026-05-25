"""
Fetches major requirements from the Anteater API and populates the
major_requirements table in Supabase.

Expected table schema (run migration in Supabase SQL editor if needed):
  CREATE TABLE major_requirements (
    id               bigserial PRIMARY KEY,
    major_id         text NOT NULL,          -- program ID or specialization ID
    major_name       text NOT NULL,
    requirement_group text NOT NULL,         -- requirementId of this Course node
    requirement_type text NOT NULL,          -- 'required' | 'elective' | 'GE'
    courses          jsonb NOT NULL,         -- array of course ID strings
    courses_needed   int  NOT NULL,          -- how many courses to take from list
    group_name       text,                   -- label of this Course node
    parent_group     text                    -- requirementId of parent Group node
  );
"""

import os
import time

import httpx
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

BASE_URL = "https://anteaterapi.com/v2/rest"
DELAY = 0.5
BATCH_SIZE = 500


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_json(url: str) -> dict:
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


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
    parent_group_id: str | None,
    ancestors: list[str],
    from_school: bool,
) -> list[dict]:
    req_type = req.get("requirementType")
    req_id = req.get("requirementId", "")
    label = req.get("label", "")

    if req_type == "Course":
        courses = req.get("courses", [])
        return [{
            "major_id": major_id,
            "requirement_group": req_id,
            "requirement_type": infer_type(label, ancestors, from_school),
            "courses": courses,
            "courses_needed": req.get("courseCount", len(courses)),
            "group_name": label,
            "parent_group": parent_group_id,
        }]

    if req_type == "Group":
        rows = []
        for child in req.get("requirements", []):
            rows.extend(flatten_req(child, major_id, req_id, ancestors + [label], from_school))
        return rows

    return []


def fetch_and_flatten(prog_id: str, spec_id: str | None) -> tuple[list[dict], bool]:
    url = f"{BASE_URL}/programs/major?programId={prog_id}"
    if spec_id:
        url += f"&specializationId={spec_id}"
    time.sleep(DELAY)

    data = fetch_json(url)
    if not data.get("ok"):
        return [], False

    resp = data["data"]
    major_id = spec_id if spec_id else prog_id
    rows = []

    school_reqs = (resp.get("schoolRequirements") or {}).get("requirements", [])
    for req in school_reqs:
        rows.extend(flatten_req(req, major_id, None, [], from_school=True))

    for req in (resp.get("requirements") or []):
        rows.extend(flatten_req(req, major_id, None, [], from_school=False))

    return rows, True


MIGRATION_SQL = """
-- Run this in the Supabase SQL editor before executing this script:
TRUNCATE TABLE major_requirements;
ALTER TABLE major_requirements
  DROP COLUMN IF EXISTS major_code,
  DROP COLUMN IF EXISTS course_id,
  DROP COLUMN IF EXISTS elective_group,
  DROP COLUMN IF EXISTS is_required,
  DROP COLUMN IF EXISTS units_required,
  DROP COLUMN IF EXISTS notes,
  DROP COLUMN IF EXISTS min_courses_required,
  ADD COLUMN IF NOT EXISTS major_id         text,
  ADD COLUMN IF NOT EXISTS major_name       text,
  ADD COLUMN IF NOT EXISTS requirement_group text,
  ADD COLUMN IF NOT EXISTS requirement_type text,
  ADD COLUMN IF NOT EXISTS courses          jsonb,
  ADD COLUMN IF NOT EXISTS courses_needed   int,
  ADD COLUMN IF NOT EXISTS group_name       text,
  ADD COLUMN IF NOT EXISTS parent_group     text;
"""


def check_schema(client) -> bool:
    """Returns True if the table has the expected columns."""
    try:
        client.table("major_requirements").insert({
            "major_id": "__schema_check__",
            "major_name": "__schema_check__",
            "requirement_group": "__schema_check__",
            "requirement_type": "required",
            "courses": [],
            "courses_needed": 0,
            "group_name": None,
            "parent_group": None,
        }).execute()
        client.table("major_requirements").delete().eq("major_id", "__schema_check__").execute()
        return True
    except Exception:
        return False


def main() -> None:
    client = get_client()

    print("Checking table schema...")
    if not check_schema(client):
        print("\nTable schema does not match. Run this SQL in the Supabase SQL editor:\n")
        print(MIGRATION_SQL)
        print("Then re-run this script.")
        return

    print("  Schema OK\n")
    print("Fetching undergraduate programs...")
    programs = [
        m for m in fetch_json(f"{BASE_URL}/programs/majors")["data"]
        if m["division"] == "Undergraduate"
    ]
    print(f"  {len(programs)} undergraduate programs\n")

    all_rows: list[dict] = []
    empty: list[str] = []
    n_fetched = 0

    for prog in programs:
        prog_id = prog["id"]
        prog_name = prog["name"]
        specs = prog.get("specializations", [])
        targets = [(prog_id, s) for s in specs] if specs else [(prog_id, None)]

        for pid, sid in targets:
            rows, ok = fetch_and_flatten(pid, sid)
            label = f"{prog_name} ({sid or pid})"
            if not ok or not rows:
                print(f"  WARNING: empty — {label}")
                empty.append(sid or pid)
                continue
            for row in rows:
                row["major_name"] = prog_name
            all_rows.extend(rows)
            n_fetched += 1
            print(f"  {label}: {len(rows)} rows")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\nTotal programs/specs fetched: {n_fetched}")
    print(f"Total requirement rows:       {len(all_rows)}")

    type_counts = {}
    for row in all_rows:
        type_counts[row["requirement_type"]] = type_counts.get(row["requirement_type"], 0) + 1
    print("Requirement type breakdown:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t:<10} {c}")

    if empty:
        print(f"\nMajors with empty/failed requirements ({len(empty)}):")
        for m in empty:
            print(f"  - {m}")

    # ── Clear and repopulate ─────────────────────────────────────────────────
    print("\nClearing major_requirements table...")
    client.table("major_requirements").delete().gt("id", 0).execute()

    print(f"Inserting {len(all_rows)} rows...")
    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i : i + BATCH_SIZE]
        client.table("major_requirements").insert(batch).execute()
        print(f"  {min(i + BATCH_SIZE, len(all_rows))}/{len(all_rows)}")

    print("\nDone.")


if __name__ == "__main__":
    main()
