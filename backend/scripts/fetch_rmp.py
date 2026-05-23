import base64
import os
import re
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

UCI_SCHOOL_ID = 1074
GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
}
PROF_QUERY = {
    "query": "query RatingsListQuery($id: ID!) {node(id: $id) {... on Teacher {school {id} firstName lastName numRatings avgDifficulty avgRating department wouldTakeAgainPercent}}}",
    "variables": {},
}
RATINGS_QUERY = {
    "query": "query RatingsListQuery($count: Int! $id: ID! $courseFilter: String $cursor: String) {node(id: $id) {... on Teacher {ratings(first: $count, after: $cursor, courseFilter: $courseFilter) {edges {node {comment}}}}}}",
    "variables": {},
}


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def search_professor_ids(name: str) -> list[int]:
    url = f"https://www.ratemyprofessors.com/search/professors/{UCI_SCHOOL_ID}?q={name}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return [int(i) for i in re.findall(r'"legacyId":(\d+)', r.text) if int(i) != UCI_SCHOOL_ID]


def get_professor_info(prof_id: int) -> dict | None:
    encoded = base64.b64encode(f"Teacher-{prof_id}".encode()).decode()
    h = {**HEADERS, "Referer": f"https://www.ratemyprofessors.com/ShowRatings.jsp?tid={prof_id}"}
    r = requests.post(GRAPHQL_URL, json={**PROF_QUERY, "variables": {"id": encoded}}, headers=h, timeout=15)
    return r.json()["data"]["node"]


def get_reviews(prof_id: int, num_ratings: int) -> str:
    encoded = base64.b64encode(f"Teacher-{prof_id}".encode()).decode()
    h = {**HEADERS, "Referer": f"https://www.ratemyprofessors.com/ShowRatings.jsp?tid={prof_id}"}
    r = requests.post(
        GRAPHQL_URL,
        json={**RATINGS_QUERY, "variables": {"id": encoded, "count": min(num_ratings, 100)}},
        headers=h,
        timeout=15,
    )
    edges = r.json()["data"]["node"]["ratings"]["edges"]
    comments = [e["node"]["comment"].strip() for e in edges if e["node"].get("comment", "").strip()]
    return " | ".join(comments)


def find_best_professor(name: str) -> tuple[dict, str] | None:
    ids = search_professor_ids(name)
    if not ids:
        return None

    best = None
    best_reviews = ""
    for prof_id in ids[:3]:
        info = get_professor_info(prof_id)
        if not info:
            continue
        if best is None or info["numRatings"] > best["numRatings"]:
            best = info
            best_reviews = get_reviews(prof_id, info["numRatings"])

    return (best, best_reviews) if best else None


def map_rmp_to_db(ucinetid: str, info: dict, review_text: str) -> dict:
    wta = info["wouldTakeAgainPercent"]
    return {
        "ucinetid": ucinetid,
        "overall_rating": info["avgRating"],
        "difficulty_rating": info["avgDifficulty"],
        "would_take_again_pct": wta if wta and wta > 0 else None,
        "num_ratings": info["numRatings"],
        "review_text": review_text or None,
        "sentiment_label": None,
    }


def load_instructors(client, limit: int = None) -> list[dict]:
    instructors = []
    page_size = 1000
    offset = 0
    while True:
        rows = (
            client.table("instructors")
            .select("ucinetid,name,shortened_names")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        instructors.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return instructors[:limit] if limit else instructors


def fetch_imported_ucinetids(client) -> set[str]:
    imported = set()
    page_size = 1000
    offset = 0
    while True:
        rows = (
            client.table("rmp_reviews")
            .select("ucinetid")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        for row in rows:
            imported.add(row["ucinetid"])
        if len(rows) < page_size:
            break
        offset += page_size
    return imported


def run(limit: int = None, dry_run: bool = False) -> None:
    client = get_client()

    print("Loading instructors...")
    instructors = load_instructors(client, limit)
    print(f"  {len(instructors)} instructors loaded.")

    print("Checking already-imported instructors...")
    imported = fetch_imported_ucinetids(client)
    print(f"  {len(imported)} already imported, skipping.\n")

    found = 0
    not_found = 0
    skipped = 0
    errors = 0

    for i, instructor in enumerate(instructors, 1):
        ucinetid = instructor["ucinetid"]
        name = instructor["name"]

        if ucinetid in imported:
            skipped += 1
            continue

        try:
            result = find_best_professor(name)

            if result is None:
                print(f"[{i}] {name} — not found on RMP")
                not_found += 1
            else:
                info, review_text = result
                row = map_rmp_to_db(ucinetid, info, review_text)

                if dry_run:
                    print(f"[{i}] {name} — rating: {row['overall_rating']}, difficulty: {row['difficulty_rating']}, "
                          f"would_take_again: {row['would_take_again_pct']:.1f}%" if row['would_take_again_pct'] else
                          f"[{i}] {name} — rating: {row['overall_rating']}, difficulty: {row['difficulty_rating']}, "
                          f"num_ratings: {row['num_ratings']}, reviews: {len(review_text)} chars")
                else:
                    client.table("rmp_reviews").upsert(row, on_conflict="ucinetid").execute()
                    print(f"[{i}] {name} — upserted (rating: {row['overall_rating']}, {row['num_ratings']} ratings)")
                found += 1

        except Exception as e:
            print(f"[{i}] {name} — error: {e}")
            errors += 1

        time.sleep(0.5)

    print(f"\nDone. Found: {found}, Not found: {not_found}, Skipped: {skipped}, Errors: {errors}")


if __name__ == "__main__":
    run()
