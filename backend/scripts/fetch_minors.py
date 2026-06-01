"""
Fetch all UCI minors from AnteaterAPI GraphQL and populate:
  - minors              (id, name)
  - minor_requirements  (flattened course/group/unit/marker rows)

Run from backend/:
  cd /Users/ryanmcdaniel/Downloads/PlanUCI/backend
  ./venv_new/bin/python3 scripts/fetch_minors.py

To wipe and re-run:
  DELETE FROM minor_requirements;
  DELETE FROM minors;
"""

import os
import time
import httpx
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

GRAPHQL_URL = "https://anteaterapi.com/v2/graphql"
API_KEY     = os.getenv("ANTEATER_API_KEY", "")
HEADERS     = {"Content-Type": "application/json", "x-api-key": API_KEY}
DELAY       = 0.3   # seconds between per-minor requests
BATCH_SIZE  = 200   # rows per Supabase upsert


# ── GraphQL helpers ───────────────────────────────────────────────────────────

def gql(query: str) -> dict:
    resp = httpx.post(GRAPHQL_URL, json={"query": query}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


MINOR_REQ_QUERY = """
{{ minor(query: {{ programId: "{pid}" }}) {{
  id name
  requirements {{
    __typename
    ... on ProgramCourseRequirement {{
      label requirementId courseCount courses
    }}
    ... on ProgramGroupRequirement {{
      label requirementId requirementCount requirements
    }}
    ... on ProgramUnitRequirement  {{ label requirementId }}
    ... on ProgramMarkerRequirement {{ label requirementId }}
  }}
}} }}
"""


# ── Requirement flattening ────────────────────────────────────────────────────

def flatten_requirements(minor_id: str, requirements: list) -> list[dict]:
    """
    Convert the API requirements list into flat minor_requirements rows.

    ProgramGroupRequirement children are stored with parent_requirement_id
    pointing to the group row's requirement_id.

    Pick-N semantics:
      courses_needed < len(courses)  → elective pool (pick N from X)
      courses_needed = len(courses)  → all courses required
      group_requirement_count < N    → satisfy N of M sub-requirements
    """
    rows = []

    for sort_idx, req in enumerate(requirements):
        typename = req.get("__typename", "")

        if typename == "ProgramCourseRequirement":
            rows.append({
                "minor_id":               minor_id,
                "requirement_id":         req["requirementId"],
                "requirement_type":       "course",
                "label":                  req["label"],
                "courses":                req.get("courses") or [],
                "courses_needed":         req.get("courseCount"),
                "parent_requirement_id":  None,
                "group_requirement_count": None,
                "sort_order":             sort_idx,
            })

        elif typename == "ProgramGroupRequirement":
            # Insert the group container row first
            rows.append({
                "minor_id":               minor_id,
                "requirement_id":         req["requirementId"],
                "requirement_type":       "group",
                "label":                  req["label"],
                "courses":                None,
                "courses_needed":         None,
                "parent_requirement_id":  None,
                "group_requirement_count": req.get("requirementCount"),
                "sort_order":             sort_idx,
            })

            # Children come back as plain dicts (JSON scalar from the API)
            children = req.get("requirements") or []
            for child_idx, child in enumerate(children):
                child_type = child.get("requirementType", "Course").lower()
                if child_type == "course":
                    rows.append({
                        "minor_id":               minor_id,
                        "requirement_id":         child["requirementId"],
                        "requirement_type":       "course",
                        "label":                  child["label"],
                        "courses":                child.get("courses") or [],
                        "courses_needed":         child.get("courseCount"),
                        "parent_requirement_id":  req["requirementId"],
                        "group_requirement_count": None,
                        "sort_order":             child_idx,
                    })
                elif child_type == "group":
                    rows.append({
                        "minor_id":               minor_id,
                        "requirement_id":         child["requirementId"],
                        "requirement_type":       "group",
                        "label":                  child["label"],
                        "courses":                None,
                        "courses_needed":         None,
                        "parent_requirement_id":  req["requirementId"],
                        "group_requirement_count": child.get("requirementCount"),
                        "sort_order":             child_idx,
                    })
                # unit/marker children are uncommon — skip silently

        elif typename == "ProgramUnitRequirement":
            rows.append({
                "minor_id":               minor_id,
                "requirement_id":         req["requirementId"],
                "requirement_type":       "unit",
                "label":                  req["label"],
                "courses":                None,
                "courses_needed":         None,
                "parent_requirement_id":  None,
                "group_requirement_count": None,
                "sort_order":             sort_idx,
            })

        elif typename == "ProgramMarkerRequirement":
            rows.append({
                "minor_id":               minor_id,
                "requirement_id":         req["requirementId"],
                "requirement_type":       "marker",
                "label":                  req["label"],
                "courses":                None,
                "courses_needed":         None,
                "parent_requirement_id":  None,
                "group_requirement_count": None,
                "sort_order":             sort_idx,
            })

    return rows


def dedup_requirement_ids(rows: list[dict]) -> list[dict]:
    """Ensure (minor_id, requirement_id) is unique within each minor.

    AnteaterAPI occasionally reuses a single fallback requirementId across
    several narrative ("verified…") rows that have no enumerable courses.
    On collision we suffix the 2nd+ occurrence with "__N" and remap any child
    rows that pointed at a suffixed parent (parents with children never collide
    in the source data, so the remap is unambiguous).
    """
    # Group rows by minor so suffixes are scoped per-minor
    by_minor: dict[str, list[dict]] = {}
    for r in rows:
        by_minor.setdefault(r["minor_id"], []).append(r)

    for minor_rows in by_minor.values():
        seen: dict[str, int] = {}
        remap: dict[str, str] = {}  # original_id → new_id (for parent relinking)
        for r in minor_rows:
            rid = r["requirement_id"]
            if rid in seen:
                seen[rid] += 1
                new_id = f"{rid}__{seen[rid]}"
                r["requirement_id"] = new_id
                remap[rid] = new_id  # last suffix wins for relink (childless in practice)
            else:
                seen[rid] = 0
        # Relink children whose parent got suffixed (groups don't collide, so safe)
        for r in minor_rows:
            pid = r.get("parent_requirement_id")
            if pid and pid in remap:
                r["parent_requirement_id"] = remap[pid]

    return rows


# ── Supabase helpers ──────────────────────────────────────────────────────────

def upsert_batch(client, table: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        client.table(table).upsert(
            rows[i : i + BATCH_SIZE], on_conflict="id"
        ).execute()


def upsert_req_batch(client, rows: list[dict]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        client.table("minor_requirements").upsert(
            rows[i : i + BATCH_SIZE], on_conflict="minor_id,requirement_id"
        ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    # 1. Fetch all minor stubs
    print("Fetching minor list…")
    all_minors = gql('{ minors { id name } }')["minors"]
    print(f"  {len(all_minors)} minors found")

    # 2. Upsert minor rows
    minor_rows = [{"id": m["id"], "name": m["name"]} for m in all_minors]
    upsert_batch(client, "minors", minor_rows)
    print(f"  Upserted {len(minor_rows)} rows into minors")

    # 3. Fetch requirements for each minor and upsert
    req_rows_all: list[dict] = []
    errors: list[str] = []

    for idx, m in enumerate(all_minors, 1):
        pid, name = m["id"], m["name"]
        try:
            data = gql(MINOR_REQ_QUERY.format(pid=pid))
            minor_data = data.get("minor")
            if not minor_data:
                print(f"  [{idx}/{len(all_minors)}] {name} — no data returned")
                continue

            reqs = flatten_requirements(pid, minor_data.get("requirements") or [])
            req_rows_all.extend(reqs)

            pick_n = sum(
                1 for r in reqs
                if r["requirement_type"] == "course"
                and r["courses"]
                and r["courses_needed"] is not None
                and r["courses_needed"] < len(r["courses"])
            )
            print(f"  [{idx:2d}/{len(all_minors)}] {name} — "
                  f"{len(reqs)} req rows, {pick_n} pick-N elective pools")

        except Exception as exc:
            errors.append(f"{name} ({pid}): {exc}")
            print(f"  [{idx}/{len(all_minors)}] ERROR {name}: {exc}")

        time.sleep(DELAY)

    # 4. Dedup colliding requirement_ids, then upsert all requirement rows
    req_rows_all = dedup_requirement_ids(req_rows_all)
    print(f"\nUpserting {len(req_rows_all)} requirement rows…")
    upsert_req_batch(client, req_rows_all)

    # 5. Summary
    print(f"\nDone.")
    print(f"  {len(all_minors)} minors")
    print(f"  {len(req_rows_all)} requirement rows")
    course_reqs = [r for r in req_rows_all if r["requirement_type"] == "course"]
    pick_n_total = sum(
        1 for r in course_reqs
        if r["courses"] and r["courses_needed"] is not None
        and r["courses_needed"] < len(r["courses"])
    )
    print(f"  {pick_n_total} pick-N elective pool requirements")
    if errors:
        print(f"\n  {len(errors)} errors:")
        for e in errors:
            print(f"    {e}")


if __name__ == "__main__":
    main()
