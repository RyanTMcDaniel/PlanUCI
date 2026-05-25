"""
Inserts university-wide GE requirements into the major_requirements table
as major_id = "ALL_MAJORS" so they apply to every student.

Course pools are built by scanning courses.ge_list for the canonical GE
strings present in the UCI courses database.  GE VI (Language Other Than
English) is marked waivable=True because it can be satisfied by AP/HS
credit without taking a UCI course.

Note on GE I mapping (corrected from common confusion):
  GE Ia: Lower Division Writing  → GE_I_WRITING_LD  (courses_needed=2)
  GE Ib: Upper Division Writing  → GE_I_WRITING_UD  (courses_needed=1)
  "GE Id" does not exist in the UCI courses database.

Run this script after the table has the waivable column:
  ALTER TABLE major_requirements
    ADD COLUMN IF NOT EXISTS waivable boolean DEFAULT false;
"""

import os

from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

MAJOR_ID   = "ALL_MAJORS"
MAJOR_NAME = "University-Wide GE Requirements"
BATCH_SIZE = 500

# ── GE category definitions ───────────────────────────────────────────────────
# ge_match: substring(s) that must appear in any element of courses.ge_list
# The courses table stores entries like "GE II: Science and Technology" so we
# match on the canonical prefix (e.g. "GE II") using str.startswith().

GE_DEFINITIONS = [
    {
        "requirement_group": "GE_I_WRITING_LD",
        "group_name":        "Writing Lower Division",
        "courses_needed":    2,
        "ge_prefixes":       ["GE Ia"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_I_WRITING_UD",
        "group_name":        "Writing Upper Division",
        "courses_needed":    1,
        "ge_prefixes":       ["GE Ib"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_II",
        "group_name":        "Science and Technology",
        "courses_needed":    3,
        "ge_prefixes":       ["GE II"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_III",
        "group_name":        "Social and Behavioral Sciences",
        "courses_needed":    3,
        "ge_prefixes":       ["GE III"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_IV",
        "group_name":        "Arts and Humanities",
        "courses_needed":    3,
        "ge_prefixes":       ["GE IV"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_Va",
        "group_name":        "Quantitative Literacy",
        "courses_needed":    1,
        "ge_prefixes":       ["GE Va"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_Vb",
        "group_name":        "Formal Reasoning",
        "courses_needed":    1,
        "ge_prefixes":       ["GE Vb"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_V_THIRD",
        "group_name":        "Quantitative or Formal Reasoning (third course)",
        "courses_needed":    1,
        "ge_prefixes":       ["GE Va", "GE Vb"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_VI",
        "group_name":        "Language Other Than English",
        "courses_needed":    1,
        "ge_prefixes":       ["GE VI"],
        "waivable":          True,   # satisfiable by AP/HS credit
    },
    {
        "requirement_group": "GE_VII",
        "group_name":        "Multicultural Studies",
        "courses_needed":    1,
        "ge_prefixes":       ["GE VII"],
        "waivable":          False,
    },
    {
        "requirement_group": "GE_VIII",
        "group_name":        "International/Global Issues",
        "courses_needed":    1,
        "ge_prefixes":       ["GE VIII"],
        "waivable":          False,
    },
]

MIGRATION_SQL = """
-- Run in the Supabase SQL editor before executing this script:
ALTER TABLE major_requirements
  ADD COLUMN IF NOT EXISTS waivable boolean DEFAULT false;
"""


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def check_schema(client) -> bool:
    """Return True if the waivable column is present."""
    try:
        client.table("major_requirements").insert({
            "major_id":          "__schema_check__",
            "major_name":        "__schema_check__",
            "requirement_group": "__schema_check__",
            "requirement_type":  "GE",
            "courses":           [],
            "courses_needed":    0,
            "waivable":          False,
        }).execute()
        client.table("major_requirements") \
            .delete().eq("major_id", "__schema_check__").execute()
        return True
    except Exception:
        return False


def fetch_all_ge_courses(client) -> dict[str, list[str]]:
    """
    Return {course_id: [ge_list entries]} for every course that has at least
    one GE entry.  Fetches in pages of 1 000 rows.
    """
    result: dict[str, list[str]] = {}
    offset = 0
    while True:
        rows = (
            client.table("courses")
            .select("id,ge_list")
            .not_.is_("ge_list", "null")
            .range(offset, offset + 999)
            .execute()
            .data
        )
        for r in rows:
            ge = r.get("ge_list") or []
            if ge:
                result[r["id"]] = ge
        if len(rows) < 1000:
            break
        offset += 1000
    return result


def build_pool(
    ge_courses: dict[str, list[str]],
    prefixes: list[str],
) -> list[str]:
    """Return course IDs whose ge_list contains an entry starting with any prefix."""
    pool = []
    for course_id, ge_list in ge_courses.items():
        if any(
            entry.startswith(prefix)
            for entry in ge_list
            for prefix in prefixes
        ):
            pool.append(course_id)
    return sorted(pool)


def main() -> None:
    client = get_client()

    print("Checking schema for waivable column...")
    if not check_schema(client):
        print("\nMissing column. Run this SQL in the Supabase SQL editor:\n")
        print(MIGRATION_SQL)
        print("Then re-run this script.")
        return
    print("  Schema OK\n")

    print("Fetching courses with GE designations...")
    ge_courses = fetch_all_ge_courses(client)
    print(f"  {len(ge_courses)} courses with at least one GE entry\n")

    # Build each category's course pool and assemble rows
    rows_to_insert: list[dict] = []

    print(f"{'Category':<28}  {'Prefixes':<20}  {'Courses':>7}  Waivable")
    print("-" * 68)

    for defn in GE_DEFINITIONS:
        pool = build_pool(ge_courses, defn["ge_prefixes"])
        print(
            f"  {defn['group_name']:<26}  "
            f"{str(defn['ge_prefixes']):<20}  "
            f"{len(pool):>7}  "
            f"{'yes' if defn['waivable'] else ''}"
        )
        rows_to_insert.append({
            "major_id":          MAJOR_ID,
            "major_name":        MAJOR_NAME,
            "requirement_group": defn["requirement_group"],
            "requirement_type":  "GE",
            "courses":           pool,
            "courses_needed":    defn["courses_needed"],
            "group_name":        defn["group_name"],
            "parent_group":      None,
            "waivable":          defn["waivable"],
        })

    print()

    # Clear existing ALL_MAJORS rows and repopulate
    print("Clearing existing ALL_MAJORS rows...")
    client.table("major_requirements").delete().eq("major_id", MAJOR_ID).execute()

    print(f"Inserting {len(rows_to_insert)} GE requirement rows...")
    for i in range(0, len(rows_to_insert), BATCH_SIZE):
        batch = rows_to_insert[i : i + BATCH_SIZE]
        client.table("major_requirements").insert(batch).execute()

    print(f"\nDone. {len(rows_to_insert)} rows inserted for major_id={MAJOR_ID!r}.")

    # Verify
    check = (
        client.table("major_requirements")
        .select("requirement_group,courses_needed,waivable")
        .eq("major_id", MAJOR_ID)
        .execute()
        .data
    )
    print(f"\nVerification — {len(check)} rows in DB for {MAJOR_ID!r}:")
    for row in check:
        flag = " [waivable]" if row.get("waivable") else ""
        print(f"  {row['requirement_group']:<22}  needs {row['courses_needed']}{flag}")


if __name__ == "__main__":
    main()
