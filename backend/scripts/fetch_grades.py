import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

GRADES_URL = "https://anteaterapi.com/v2/rest/grades/raw"

DEPARTMENTS = [
    "AC ENG", "AFAM", "ANATOMY", "ANTHRO", "ARABIC", "ARMN", "ART", "ART HIS",
    "ARTS", "ASIANAM", "ASL", "BANA", "BATS", "BIO SCI", "BIOCHEM", "BME",
    "BSEMD", "CBE", "CBEMS", "CHC/LAT", "CHEM", "CHINESE", "CLASSIC", "CLT&THY",
    "COGS", "COM LIT", "COMPSCI", "CRITISM", "CRM/LAW", "CSE", "DANCE", "DATA",
    "DEV BIO", "DRAMA", "E ASIAN", "EARTHSS", "EAS", "ECO EVO", "ECON", "ECPS",
    "EDUC", "EECS", "EHS", "ENGLISH", "ENGR", "ENGRCEE", "ENGRMAE", "ENGRMSE",
    "EPIDEM", "EURO ST", "FIN", "FLM&MDA", "FRENCH", "GDIM", "GEN&SEX", "GERMAN",
    "GLBL ME", "GLBLCLT", "GREEK", "HEBREW", "HISTORY", "HUMAN", "I&C SCI",
    "IN4MATX", "INNO", "INTL ST", "IRAN", "ITALIAN", "JAPANSE", "KOREAN", "LATIN",
    "LINGUIS", "LIT JRN", "LPS", "LSCI", "M&MG", "MATH", "MED HUM", "MGMT",
    "MGMT EP", "MGMT FE", "MGMT HC", "MGMTMBA", "MGMTPHD", "MNGE", "MOL BIO",
    "MPAC", "MSE", "MUSIC", "NET SYS", "NEURBIO", "NUR SCI", "PATH", "PED GEN",
    "PERSIAN", "PHARM", "PHILOS", "PHMD", "PHRMSCI", "PHY SCI", "PHYSICS",
    "PHYSIO", "POL SCI", "PORTUG", "PP&D", "PSCI", "PSY BEH", "PSYCH", "PUB POL",
    "PUBHLTH", "REL STD", "ROTC", "RUSSIAN", "SOC SCI", "SOCECOL", "SOCIOL",
    "SPANISH", "SPPS", "STATS", "SWE", "TOX", "UCDC", "UNI AFF", "UNI STU",
    "UPPP", "VIETMSE", "VIS STD", "WOMN ST", "WRITING",
]


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_imported_departments(client) -> set[str]:
    result = client.table("grade_distributions").select("course_id").limit(1).execute()
    # We track by department, so just return departments that have any rows
    imported = set()
    page_size = 1000
    offset = 0
    while True:
        rows = (
            client.table("grade_distributions")
            .select("course_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        for row in rows:
            # course_id starts with the department code (spaces removed)
            # We'll match departments in the main loop
            imported.add(row["course_id"])
        if len(rows) < page_size:
            break
        offset += page_size
    return imported


def build_course_lookup(client) -> dict[tuple, str]:
    """Returns {(department, courseNumber): course_id}"""
    lookup = {}
    page_size = 1000
    offset = 0
    while True:
        rows = (
            client.table("courses")
            .select("id,department,course_number")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        for row in rows:
            lookup[(row["department"], row["course_number"])] = row["id"]
        if len(rows) < page_size:
            break
        offset += page_size
    return lookup


def fetch_grades_for_department(department: str) -> list[dict]:
    response = requests.get(GRADES_URL, params={"department": department}, timeout=30)
    response.raise_for_status()
    return response.json().get("data", [])


def map_grade_to_db(grade: dict, course_id: str) -> dict:
    return {
        "course_id": course_id,
        "instructor_raw": " / ".join(grade.get("instructors", [])),
        "year": grade.get("year"),
        "quarter": grade.get("quarter"),
        "section_code": grade.get("sectionCode"),
        "grade_a_count": grade.get("gradeACount"),
        "grade_b_count": grade.get("gradeBCount"),
        "grade_c_count": grade.get("gradeCCount"),
        "grade_d_count": grade.get("gradeDCount"),
        "grade_f_count": grade.get("gradeFCount"),
        "grade_p_count": grade.get("gradePCount"),
        "grade_np_count": grade.get("gradeNPCount"),
        "grade_w_count": grade.get("gradeWCount"),
        "average_gpa": grade.get("averageGPA"),
    }


def run() -> None:
    client = get_client()

    print("Building course lookup...")
    course_lookup = build_course_lookup(client)
    print(f"  {len(course_lookup)} courses loaded.\n")

    print("Checking already-imported departments...")
    imported_course_ids = fetch_imported_departments(client)
    imported_depts = set()
    for dept in DEPARTMENTS:
        dept_key = dept.replace(" ", "")
        if any(cid.startswith(dept_key) for cid in imported_course_ids):
            imported_depts.add(dept)
    print(f"  {len(imported_depts)} departments already imported, skipping.\n")

    total = len(DEPARTMENTS)
    rows_inserted = 0
    errors = 0

    for i, department in enumerate(DEPARTMENTS, 1):
        if department in imported_depts:
            print(f"[{i}/{total}] {department} — skipped (already imported)")
            continue

        try:
            grades = fetch_grades_for_department(department)
        except Exception as e:
            print(f"[{i}/{total}] {department} — fetch error: {e}")
            errors += 1
            time.sleep(1)
            continue

        if not grades:
            print(f"[{i}/{total}] {department} — no data")
            time.sleep(0.5)
            continue

        rows = []
        for grade in grades:
            key = (grade.get("department"), grade.get("courseNumber"))
            course_id = course_lookup.get(key)
            if not course_id:
                continue
            rows.append(map_grade_to_db(grade, course_id))

        try:
            if rows:
                client.table("grade_distributions").upsert(
                    rows, on_conflict="year,quarter,section_code"
                ).execute()
                rows_inserted += len(rows)
            print(f"[{i}/{total}] {department} — {len(rows)} rows upserted")
        except Exception as e:
            print(f"[{i}/{total}] {department} — upsert error: {e}")
            errors += 1

        time.sleep(0.5)

    print(f"\nDone. Rows inserted: {rows_inserted}, Errors: {errors}")


if __name__ == "__main__":
    run()
