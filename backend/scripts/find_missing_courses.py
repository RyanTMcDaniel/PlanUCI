import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
ANTEATER_API_KEY = os.getenv("ANTEATER_API_KEY")

ANTEATER_HEADERS = {"x-api-key": ANTEATER_API_KEY} if ANTEATER_API_KEY else {}


def fetch_all_requirement_course_ids(client) -> set[str]:
    PAGE = 1000
    all_ids: set[str] = set()
    from_ = 0
    while True:
        resp = client.table("major_requirements").select("courses").range(from_, from_ + PAGE - 1).execute()
        rows = resp.data or []
        if not rows:
            break
        for row in rows:
            for cid in row.get("courses") or []:
                all_ids.add(cid)
        if len(rows) < PAGE:
            break
        from_ += PAGE
    return all_ids


def fetch_all_existing_course_ids(client) -> set[str]:
    PAGE = 1000
    all_ids: set[str] = set()
    from_ = 0
    while True:
        resp = client.table("courses").select("id").range(from_, from_ + PAGE - 1).execute()
        rows = resp.data or []
        if not rows:
            break
        for row in rows:
            all_ids.add(row["id"])
        if len(rows) < PAGE:
            break
        from_ += PAGE
    return all_ids


def fetch_course_from_api(course_id: str) -> dict | None:
    url = f"https://anteaterapi.com/v2/rest/courses/{course_id}"
    try:
        r = requests.get(url, headers=ANTEATER_HEADERS, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("data")
    except requests.RequestException as e:
        print(f"  Request error for {course_id}: {e}")
        return None


def map_course_to_db(course: dict) -> dict:
    return {
        "id": course.get("id"),
        "department": course.get("department"),
        "course_number": course.get("courseNumber"),
        "course_numeric": course.get("courseNumeric"),
        "title": course.get("title"),
        "description": course.get("description"),
        "school": course.get("school"),
        "department_name": course.get("departmentName"),
        "min_units": course.get("minUnits"),
        "max_units": course.get("maxUnits"),
        "course_level": course.get("courseLevel"),
        "restriction": course.get("restriction"),
        "ge_list": course.get("geList", []),
        "ge_text": course.get("geText"),
        "terms": course.get("terms", []),
        "prerequisite_text": course.get("prerequisiteText"),
        "prerequisite_tree": course.get("prerequisiteTree"),
        "repeatability": course.get("repeatability"),
        "grading_option": course.get("gradingOption"),
        "same_as": course.get("sameAs"),
        "corequisites": course.get("corequisites"),
    }


def main() -> None:
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("Fetching all course IDs from major_requirements...")
    req_ids = fetch_all_requirement_course_ids(client)
    print(f"  Found {len(req_ids)} unique course IDs referenced in requirements")

    print("Fetching all existing course IDs from courses table...")
    existing_ids = fetch_all_existing_course_ids(client)
    print(f"  Found {len(existing_ids)} existing courses in DB")

    missing = sorted(req_ids - existing_ids)
    print(f"\nMissing course IDs (in requirements but not in DB): {len(missing)}")

    if not missing:
        print("Nothing to do — all referenced courses exist in the DB.")
        return

    inserted: list[str] = []
    not_found: list[str] = []
    errors: list[str] = []

    for i, course_id in enumerate(missing, 1):
        print(f"  [{i}/{len(missing)}] Checking {course_id}...", end=" ", flush=True)
        course_data = fetch_course_from_api(course_id)

        if course_data is None:
            print("NOT FOUND in AnteaterAPI")
            not_found.append(course_id)
        else:
            row = map_course_to_db(course_data)
            try:
                client.table("courses").upsert(row, on_conflict="id").execute()
                print(f"inserted ({row.get('title') or 'no title'})")
                inserted.append(course_id)
            except Exception as e:
                print(f"DB ERROR: {e}")
                errors.append(course_id)

        time.sleep(0.2)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total missing course IDs:          {len(missing)}")
    print(f"Successfully inserted from API:    {len(inserted)}")
    print(f"Not found in AnteaterAPI:          {len(not_found)}")
    if errors:
        print(f"DB errors during insert:           {len(errors)}")

    if not_found:
        print("\nCourses not found in AnteaterAPI (likely bad data in requirements):")
        for cid in not_found:
            print(f"  {cid}")

    if errors:
        print("\nCourses that errored during DB insert:")
        for cid in errors:
            print(f"  {cid}")

    if inserted:
        print(f"\nInserted {len(inserted)} new courses into the DB.")


if __name__ == "__main__":
    main()
