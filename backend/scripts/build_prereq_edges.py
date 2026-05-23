import os
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

LOGIC_KEYS = {"AND", "OR", "NOT"}


def extract_edges(course_id: str, node: dict, parent_logic: str | None = None) -> list[dict]:
    edges = []

    for logic_key in LOGIC_KEYS:
        if logic_key not in node:
            continue

        for item in node[logic_key]:
            prereq_type = item.get("prereqType")

            if prereq_type == "course":
                edges.append({
                    "course_id": course_id,
                    "prereq_course_id": item.get("courseId"),
                    "prereq_type": "course",
                    "min_grade": item.get("minGrade"),
                    "is_coreq": item.get("coreq", False),
                    "logic_group": logic_key,
                    "logic_type": logic_key,
                })
            elif prereq_type == "exam":
                edges.append({
                    "course_id": course_id,
                    "prereq_course_id": item.get("examName"),
                    "prereq_type": "exam",
                    "min_grade": item.get("minGrade"),
                    "is_coreq": False,
                    "logic_group": logic_key,
                    "logic_type": logic_key,
                })
            else:
                # Nested logic group — recurse
                edges.extend(extract_edges(course_id, item, logic_key))

    return edges


def upsert_edges(client, course_id: str, edges: list[dict]) -> int:
    client.table("prereq_edges").delete().eq("course_id", course_id).execute()
    if not edges:
        return 0
    client.table("prereq_edges").insert(edges).execute()
    return len(edges)


def fetch_courses_with_prereqs(client, limit: int | None = None):
    if limit:
        return (
            client.table("courses")
            .select("id,prerequisite_tree")
            .not_.is_("prerequisite_tree", "null")
            .limit(limit)
            .execute().data
        )

    all_courses = []
    offset = 0
    while True:
        batch = (
            client.table("courses")
            .select("id,prerequisite_tree")
            .not_.is_("prerequisite_tree", "null")
            .range(offset, offset + 999)
            .execute().data
        )
        all_courses.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return all_courses


def main(test: bool = False) -> None:
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    limit = 5 if test else None
    courses = fetch_courses_with_prereqs(client, limit=limit)
    print(f"Processing {len(courses)} courses...")

    total_edges = 0
    total_courses = 0

    for row in courses:
        course_id = row["id"]
        tree = row["prerequisite_tree"]

        if not tree:
            continue

        edges = extract_edges(course_id, tree)
        count = upsert_edges(client, course_id, edges)
        if count:
            print(f"  {course_id}: {count} edge(s)")
        total_edges += count
        total_courses += 1

    print(f"\nDone. Courses with edges: {total_courses}, Total edges created: {total_edges}")


if __name__ == "__main__":
    test_mode = "--test" in sys.argv or len(sys.argv) > 1 and sys.argv[1] == "test"
    main(test=test_mode)
