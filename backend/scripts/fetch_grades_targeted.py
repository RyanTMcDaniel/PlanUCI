"""
Re-import grades for specific departments, bypassing the skip-by-prefix logic.
Only upserts rows not already present (on_conflict=year,quarter,section_code).
"""
import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

GRADES_URL = "https://anteaterapi.com/v2/rest/grades/raw"

# Departments where the original skip logic incorrectly marked them as done
# because a handful of grad-course rows existed (COMPSCI199, MATH10, etc.)
TARGET_DEPARTMENTS = [
    "COMPSCI",
    "MATH",
    "STATS",
    "I&C SCI",
]


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


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
    response = requests.get(
        GRADES_URL,
        params={"department": department},
        headers={"Origin": "https://anteaterapi.com"},
        timeout=60,
    )
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

    total = len(TARGET_DEPARTMENTS)
    rows_inserted = 0
    errors = 0

    for i, department in enumerate(TARGET_DEPARTMENTS, 1):
        print(f"[{i}/{total}] Fetching {department}...")
        try:
            grades = fetch_grades_for_department(department)
        except Exception as e:
            print(f"  fetch error: {e}")
            errors += 1
            time.sleep(2)
            continue

        if not grades:
            print(f"  no data returned")
            continue

        print(f"  {len(grades)} grade records from API")

        rows = []
        skipped = 0
        for grade in grades:
            key = (grade.get("department"), grade.get("courseNumber"))
            course_id = course_lookup.get(key)
            if not course_id:
                skipped += 1
                continue
            rows.append(map_grade_to_db(grade, course_id))

        print(f"  {len(rows)} rows mapped ({skipped} skipped — no course match)")

        if not rows:
            continue

        # Upsert in batches of 500
        batch_size = 500
        for j in range(0, len(rows), batch_size):
            batch = rows[j : j + batch_size]
            try:
                client.table("grade_distributions").upsert(
                    batch, on_conflict="year,quarter,section_code"
                ).execute()
                rows_inserted += len(batch)
                print(f"  upserted batch {j//batch_size + 1} ({len(batch)} rows)")
            except Exception as e:
                print(f"  upsert error: {e}")
                errors += 1

        time.sleep(1)

    print(f"\nDone. Total rows upserted: {rows_inserted}, Errors: {errors}")


if __name__ == "__main__":
    run()
