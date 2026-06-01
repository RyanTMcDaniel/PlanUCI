#!/usr/bin/env python3
"""
Scrape major requirements from the UCI catalogue and write to catalogue_requirements.

Usage:
    python scrape_major_requirements.py --test    # Applied Math only, prints rows, no upsert
    python scrape_major_requirements.py           # full scrape of all majors

Output schema (catalogue_requirements table):
    major_id        – Anteater API program ID (e.g. "BS-0K6C"), looked up by name
    specialization_id – concentration slug ("data-science") or NULL for core sections
    group_name      – human-readable section label
    requirement_type – "required" | "elective" | "GE"
    courses         – array of course IDs
    courses_needed  – how many from the array are needed
"""
import argparse
import json
import os
import re
import time
import uuid
from collections import defaultdict
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BASE_URL = "https://catalogue.uci.edu"
INDEX_URL = f"{BASE_URL}/undergraduatedegrees/"

TEST_MAJOR = (
    f"{BASE_URL}/schoolofphysicalsciences/departmentofmathematics"
    "/appliedandcomputationalmathematics_bs/",
    "Applied and Computational Mathematics B.S.",
    "APPLIEDANDCOMPUTATIONALMATHEMATICS_BS",
)

COMPOUND_COURSE_RE = re.compile(
    r"^([A-Z][A-Z0-9 &/]*)\s+([A-Z]?\d+[A-Z]{0,2}(?:-[A-Z]?\d+[A-Z]{0,2})*)$"
)

PICK_N_RE = re.compile(
    # P1: verb [at least] N [any qualifiers, incl. hyphenated] (courses|electives) [gap] from
    r"(?:select|choose|complete|take|pick)\s+(?:at\s+least\s+)?(\w+)\b.{0,60}?\b(?:courses?|electives?)\b.{0,80}?\bfrom\b"
    r"|"
    # P2: verb [at least] N of/from the following
    r"(?:select|choose|complete|take|pick)\s+(?:at\s+least\s+)?(\w+)\s+(?:of|from)\s+the\s+following"
    r"|"
    # P3: bare "N courses from the following" (no verb)
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+courses?\s+from\s+the\s+following"
    r"|"
    # P4: "at least N courses/electives from"
    r"at\s+least\s+(\w+)\s+(?:courses?|electives?)\s+from"
    r"|"
    # P5: "complete/select N elective courses" with no "from" clause
    r"(?:select|choose|complete|take|pick)\s+(?:at\s+least\s+)?(\w+)\b.{0,60}?\b(?:courses?|electives?)\b",
    re.IGNORECASE,
)

REQUIRED_RESET_RE = re.compile(
    r"^\s*(?:[A-Z]\.\s+)?(?:complete|required|all\s+of\s+the\s+following):?\s*$",
    re.IGNORECASE,
)

UNITS_RE = re.compile(r"(\d+)\s+units?", re.IGNORECASE)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PlanUCI-scraper/1.0)"})


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true",
                   help="Run Applied Math only; print rows as JSON; skip upsert")
    p.add_argument("--only-aliases", action="store_true",
                   help="Re-scrape only majors whose core name appears in SCRAPER_ALIASES")
    return p.parse_args()


# ── Course code helpers ───────────────────────────────────────────────────────

def normalize_course_id(dept: str, num: str) -> str:
    return dept.replace(" ", "") + num


def _parse_one_code(raw: str) -> list[tuple[str, str]]:
    m = COMPOUND_COURSE_RE.match(raw.strip())
    if not m:
        return []
    dept = m.group(1).strip()
    num_range = m.group(2)
    if "-" not in num_range:
        return [(dept, num_range)]
    parts = num_range.split("-")
    result = []
    prefix = re.match(r"^([A-Z]*)", parts[0]).group(1)
    for part in parts:
        if re.match(r"^\d", part):
            result.append((dept, prefix + part))
        else:
            result.append((dept, part))
            prefix = re.match(r"^([A-Z]*)", part).group(1)
    return result


def expand_course_code(raw_code: str) -> list[tuple[str, str]]:
    raw_code = raw_code.replace("\xa0", " ").strip()
    slash_parts = re.split(r"(?<=\d)/(?=[A-Z])", raw_code)
    if len(slash_parts) > 1:
        result = []
        for part in slash_parts:
            result.extend(_parse_one_code(part))
        return result
    return _parse_one_code(raw_code)


def word_to_int(word: str) -> Optional[int]:
    if word.isdigit():
        return int(word)
    return NUMBER_WORDS.get(word.lower())


def parse_pick_n(text: str) -> Optional[int]:
    m = PICK_N_RE.search(text)
    if not m:
        return None
    for g in m.groups():
        if g is not None:
            return word_to_int(g)
    return None


def classify_section(header: str) -> tuple[str, bool]:
    h = header.lower()
    if any(k in h for k in ("preparation", "lower-division", "lower division")):
        return "preparation", True
    if any(k in h for k in ("elective", "restricted elective", "technical elective")):
        return "elective", False
    if any(k in h for k in ("required", "core", "upper division", "upper-division")):
        return "core", True
    if any(k in h for k in ("general education", "ge requirement", "breadth")):
        return "GE", True
    if any(k in h for k in ("school requirement", "ics requirement", "school of")):
        return "school", True
    if "university" in h:
        return "GE", True
    if any(k in h for k in ("writing", "ethics")):
        return "school", True
    return "requirement", True


# ── Specialization detection ──────────────────────────────────────────────────

def _heading_to_spec(text: str) -> Optional[str]:
    """
    Derive specialization_id from a section heading.
    Returns None for core/shared sections, a slug for specialization-specific ones.

    Examples:
      "Core Requirements for all ACM Majors"           → None
      "Major Requirements"                             → None
      "Requirements for the B.S. in Computer Science" → None
      "Requirements for the ACM Major"                 → "general"
      "Requirements for ACM with a Concentration in Data Science" → "data-science"
      "Requirements for ACM with a Specialization in Math Bio"    → "mathematical-biology"
    """
    t = text.lower()
    # Explicitly shared/core sections → None
    if any(k in t for k in (
        "core requirement", "major requirement",
        "requirements for the b.s.", "requirements for the b.a.",
        "requirements for the b.f.a.", "requirements for the b.mus.",
        "requirements for the b.arch.",
    )):
        return None
    # Named concentration or specialization → slug
    for kw in ("concentration in ", "specialization in ", "emphasis in ", "track in ", "option in "):
        if kw in t:
            idx = t.index(kw) + len(kw)
            spec_name = text[idx:].strip().rstrip(".")
            return re.sub(r"[^a-z0-9]+", "-", spec_name.lower()).strip("-")
    # Generic "Requirements for the X Major" → "general" path
    if "requirements for" in t and "major" in t:
        return "general"
    return None


# ── Per-table row parser ──────────────────────────────────────────────────────

def _parse_table_rows(table) -> list[dict]:
    """
    Parse one sc_courselist table into per-course dicts.
    Each dict has: course_id, requirement_type, section_header,
                   elective_group, is_required, min_courses_required.
    """
    rows: list[dict] = []
    cur_req_type = "requirement"
    cur_section_req_type = "requirement"
    cur_is_required = True
    cur_elective_group: Optional[str] = None
    cur_section_header: Optional[str] = None
    cur_min_courses: Optional[int] = None
    cur_units: Optional[int] = None
    seen: set[tuple] = set()

    tbody = table.find("tbody") or table

    for tr in tbody.find_all("tr"):
        tr_classes = set(tr.get("class", []))

        if "areaheader" in tr_classes or "subheader" in tr_classes:
            td = tr.find("td") or tr.find("th")
            if td:
                header_text = td.get_text(separator=" ", strip=True)
                new_req_type, new_is_required = classify_section(header_text)
                if new_req_type != "requirement":
                    cur_req_type = new_req_type
                    cur_section_req_type = new_req_type
                    cur_is_required = new_is_required
                    cur_min_courses = None
                    cur_units = None
                    cur_elective_group = header_text if not new_is_required else None
                    cur_section_header = header_text
                elif not cur_is_required:
                    cur_elective_group = header_text
                    cur_min_courses = None
                    n = parse_pick_n(header_text)
                    if n is not None:
                        cur_min_courses = n
            continue

        tds = tr.find_all("td")
        is_comment = len(tds) == 1 or "comment" in " ".join(tr.get("class", []))
        if is_comment:
            text = tr.get_text(separator=" ", strip=True)
            if REQUIRED_RESET_RE.match(text):
                cur_req_type = cur_section_req_type
                cur_is_required = True
                cur_min_courses = None
                cur_elective_group = None
                continue
            n = parse_pick_n(text)
            if n is not None:
                cur_min_courses = n
                cur_is_required = False
                cur_req_type = "elective"
                cur_elective_group = text[:120]  # each pick-N comment starts a new group
            else:
                u_match = UNITS_RE.search(text)
                if u_match:
                    cur_units = int(u_match.group(1))
            continue

        code_td = tr.find("td", class_="codecol")
        if not code_td:
            continue

        raw_code = code_td.get_text(strip=True).replace("\xa0", " ")
        raw_code = re.sub(r"^or(?=[A-Z ])", "", raw_code).strip()

        pairs = expand_course_code(raw_code)
        if not pairs:
            if raw_code:
                print(f"  Skipping unparseable code: {raw_code!r}")
            continue

        for dept, num in pairs:
            course_id = normalize_course_id(dept, num)
            dedup_key = (course_id, cur_elective_group or cur_section_header)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            rows.append({
                "course_id": course_id,
                "requirement_type": cur_req_type,
                "section_header": cur_section_header,
                "elective_group": cur_elective_group,
                "is_required": cur_is_required,
                "min_courses_required": cur_min_courses if not cur_is_required else None,
            })

    return rows


# ── Grouping + catalogue format conversion ────────────────────────────────────

_REQ_TYPE_MAP = {
    "preparation": "required",
    "core":        "required",
    "requirement": "required",
    "school":      "required",
    "elective":    "elective",
    "GE":          "GE",
}


def _rows_to_catalogue_groups(
    per_course_rows: list[dict],
    major_id: str,
    spec_id: Optional[str],
) -> list[dict]:
    """Convert per-course rows into catalogue_requirements rows (one per group)."""
    # Group by (requirement_type, elective_group or section_header)
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in per_course_rows:
        group_label = row["elective_group"] or row["section_header"] or "Requirements"
        key = (row["requirement_type"], group_label)
        buckets[key].append(row)

    catalogue_rows = []
    for (req_type, group_label), rows in buckets.items():
        courses = [r["course_id"] for r in rows]
        min_c = rows[0]["min_courses_required"]
        is_req = rows[0]["is_required"]
        courses_needed = min_c if (not is_req and min_c is not None) else len(courses)
        db_req_type = _REQ_TYPE_MAP.get(req_type, "required")

        catalogue_rows.append({
            "major_id":         major_id,
            "specialization_id": spec_id,
            "group_name":       group_label[:250],
            "requirement_type": db_req_type,
            "courses":          courses,
            "courses_needed":   courses_needed,
        })

    return catalogue_rows


# ── Main extraction: DOM walk → groups ───────────────────────────────────────

def extract_catalogue_requirements(html: str, major_id: str) -> list[dict]:
    """
    Walk the requirements section in document order.
    Headings (h3/h4/h5) set the current specialization_id.
    Each sc_courselist table is parsed under the current specialization context.
    Returns a flat list of catalogue_requirements rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    req_div = (
        soup.find(id="requirementstextcontainer")
        or soup.find(id="requirementstext")
        or soup.find(attrs={"class": re.compile(r"requirementstext", re.I)})
    )
    if req_div is None:
        print("  WARNING: Could not find requirements container — parsing full page.")
        req_div = soup

    catalogue_rows: list[dict] = []
    current_spec: Optional[str] = None

    def _visit(node):
        nonlocal current_spec
        tag = getattr(node, "name", None)
        if not tag:
            return
        if tag in ("h3", "h4", "h5"):
            text = node.get_text(strip=True)
            if text:
                current_spec = _heading_to_spec(text)
        elif tag == "table" and "sc_courselist" in node.get("class", []):
            per_course = _parse_table_rows(node)
            if per_course:
                groups = _rows_to_catalogue_groups(per_course, major_id, current_spec)
                catalogue_rows.extend(groups)
        else:
            for child in node.children:
                _visit(child)

    for child in req_div.children:
        _visit(child)

    if not catalogue_rows:
        print("  WARNING: No sc_courselist tables found.")

    return catalogue_rows


# ── major_id lookup ───────────────────────────────────────────────────────────

# Explicit aliases: catalogue core name → Supabase major_id.
# Used when the auto name-match fails (UCI renamed the program or the API
# uses a slightly different display name).
SCRAPER_ALIASES: dict[str, str] = {
    # Name variants / renames
    "Music Theatre":                    "BFA-02B",
    "Physiology and Exercise Science":  "BS-08R",
    "Psychology":                       "BA-0EH",
    # Engineering — present in AnteaterAPI but not auto-matched
    "Civil Engineering":                "BS-284",
    "Electrical Engineering":           "BS-276",
    "Mechanical Engineering":           "BS-277",
    "Materials Science and Engineering":"BS-328",
    "Computer Engineering":             "BS-302",
    # ICS programs
    "Informatics":                      "BS-19H",
    "Game Design and Interactive Media":"BS-0GJ",
    # Sciences / other
    "Mathematics":                      "BS-540",
    "Microbiology and Immunology":      "BS-569",
    "Criminology, Law and Society":     "BA-220",
    "Business Information Management":  "BS-01N",
}

# Cache keyed by (degree_prefix, normalized_core_name) → shortest major_id
_api_major_cache: dict[tuple[str, str], str] = {}


def _normalize_name(name: str) -> str:
    """Lowercase, replace hyphens and & with spaces, collapse whitespace."""
    n = name.lower().replace("-", " ").replace("&", "and")
    return re.sub(r"\s+", " ", n).strip()


def _load_api_major_cache(client) -> None:
    if _api_major_cache:
        return
    r = client.table("major_requirements").select("major_id,major_name").execute()
    for row in r.data:
        # Strip "Major in " prefix from Anteater API names
        core = re.sub(r"^Major\s+in\s+", "", row["major_name"], flags=re.I).strip()
        core_norm = _normalize_name(core)
        # Degree prefix from major_id: "BS-279" → "BS", "BFA-796" → "BFA"
        dm = re.match(r"([A-Z]+)-", row["major_id"])
        degree = dm.group(1) if dm else ""
        key = (degree, core_norm)
        existing = _api_major_cache.get(key)
        # Keep the shortest (parent) ID for each (degree, name) pair
        if existing is None or len(row["major_id"]) < len(existing):
            _api_major_cache[key] = row["major_id"]


def lookup_api_major_id(catalogue_name: str, slug: str, client) -> Optional[str]:
    """
    Map a catalogue display name + URL slug to the Anteater API major_id.

    Checks SCRAPER_ALIASES first, then falls back to the auto-built cache.

    catalogue_name: "Applied and Computational Mathematics, B.S."
    slug:           "APPLIEDANDCOMPUTATIONALMATHEMATICS_BS"
    """
    # Strip optional comma + degree suffix: "Name, B.S." / "Name, B.F.A." → "Name"
    # [A-Za-z.]+ handles multi-part abbreviations like B.F.A. and B.Mus.
    core = re.sub(r",?\s+B\.[A-Za-z.]+$", "", catalogue_name).strip()

    # 1. Explicit alias table (highest priority)
    if core in SCRAPER_ALIASES:
        return SCRAPER_ALIASES[core]

    # 2. Auto name-match via DB cache
    _load_api_major_cache(client)
    sm = re.search(r"_(BS|BA|BFA|BMUS|BARCH)$", slug, re.I)
    degree = sm.group(1).upper() if sm else ""
    core_norm = _normalize_name(core)
    return _api_major_cache.get((degree, core_norm))


# ── Network helpers ───────────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    resp = SESSION.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


def fetch_major_list() -> list[tuple[str, str, str]]:
    """Returns (url, display_name, slug) for every undergraduate degree on the index."""
    html = fetch_html(INDEX_URL)
    soup = BeautifulSoup(html, "html.parser")
    majors: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"^(/[^\"]+_(bs|ba|bfa|bmus|barch))/?$", href, re.IGNORECASE)
        if not m:
            continue
        path = m.group(1).rstrip("/") + "/"
        if path in seen:
            continue
        seen.add(path)
        url = BASE_URL + path
        name = a.get_text(strip=True)
        slug = path.rstrip("/").split("/")[-1].upper()
        if name:
            majors.append((url, name, slug))

    print(f"Found {len(majors)} majors on the index page.")
    return majors


# ── DB client + upsert ────────────────────────────────────────────────────────

def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def upsert_catalogue(rows: list[dict], api_major_id: str) -> None:
    if not rows:
        print("  No rows to upsert.")
        return
    client = get_client()
    client.table("catalogue_requirements").delete().eq("major_id", api_major_id).execute()
    client.table("catalogue_requirements").insert(rows).execute()
    print(f"  Inserted {len(rows)} rows.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if args.test:
        majors = [TEST_MAJOR]
    else:
        majors = fetch_major_list()

    client = get_client()

    if args.only_aliases:
        def _core(name: str) -> str:
            return re.sub(r",?\s+B\.[A-Za-z.]+$", "", name).strip()
        majors = [(url, name, slug) for url, name, slug in majors
                  if _core(name) in SCRAPER_ALIASES]
        print(f"--only-aliases: {len(majors)} majors selected")

    for i, (url, name, slug) in enumerate(majors):
        if i > 0:
            time.sleep(0.5)

        api_id = lookup_api_major_id(name, slug, client)
        if api_id is None:
            print(f"[{i+1}/{len(majors)}] SKIP (no API match): {name}  ({slug})")
            continue

        print(f"\n[{i+1}/{len(majors)}] {name}  api_id={api_id}")
        print(f"  {url}")

        try:
            html = fetch_html(url)
        except Exception as exc:
            print(f"  ERROR fetching page: {exc}")
            continue

        catalogue_rows = extract_catalogue_requirements(html, api_id)

        if args.test:
            # Show summary by specialization, then full JSON
            by_spec: dict = defaultdict(list)
            for r in catalogue_rows:
                by_spec[r["specialization_id"]].append(r)
            print(f"\n  {len(catalogue_rows)} total rows across {len(by_spec)} specialization(s):")
            for spec, spec_rows in sorted(by_spec.items(), key=lambda x: str(x[0])):
                print(f"    spec={spec!r}: {len(spec_rows)} groups")
                for r in spec_rows:
                    needed = r["courses_needed"]
                    pool = len(r["courses"])
                    print(f"      [{r['requirement_type']:<8}] need={needed:<3} pool={pool:<3}  {r['group_name'][:60]!r}")
            print()
            print(json.dumps(catalogue_rows, indent=2))
            break

        if not catalogue_rows:
            print("  No requirements extracted — skipping upsert.")
            continue

        print(f"  Extracted {len(catalogue_rows)} requirement groups.")
        upsert_catalogue(catalogue_rows, api_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
