#!/usr/bin/env python3
"""
Scrape CS B.S. major requirements from the UCI catalogue and upsert into Supabase.

Install deps before running:
    pip install playwright beautifulsoup4
    playwright install chromium
"""
import asyncio
import os
import re
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from supabase import create_client

load_dotenv()

MAJOR_CODE = "CS_BS"
MAJOR_NAME = "Computer Science B.S."
CATALOGUE_URL = (
    "https://catalogue.uci.edu/donaldbrenschoolofinformationandcomputersciences"
    "/departmentofcomputerscience/computerscience_bs/#requirementstext"
)

# Matches dept + number, including compound dash ranges like "31-32-33" or "H32-33" or "2A-2B".
COMPOUND_COURSE_RE = re.compile(
    r"^([A-Z][A-Z0-9 &/]*)\s+([A-Z]?\d+[A-Z]{0,2}(?:-[A-Z]?\d+[A-Z]{0,2})*)$"
)

# Covers several pick-N patterns:
#   "select/choose/... [at least] N [0-3 qualifier words] courses from"
#   "select/... N of/from the following"
#   "N courses from the following"  (standalone, common in areaheaders)
#   "at least N courses from"
PICK_N_RE = re.compile(
    r"(?:select|choose|complete|take|pick)\s+(?:at\s+least\s+)?(\w+)(?:\s+\w+){0,3}\s+courses?\s+from"
    r"|"
    r"(?:select|choose|complete|take|pick)\s+(?:at\s+least\s+)?(\w+)\s+(?:of|from)\s+the\s+following"
    r"|"
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+courses?\s+from\s+the\s+following"
    r"|"
    r"at\s+least\s+(\w+)\s+courses?\s+from",
    re.IGNORECASE,
)

# Detects a "B. Complete:" / "Required:" type note that resets back to required mode
REQUIRED_RESET_RE = re.compile(
    r"^\s*(?:[A-Z]\.\s+)?(?:complete|required|all\s+of\s+the\s+following):?\s*$",
    re.IGNORECASE,
)

UNITS_RE = re.compile(r"(\d+)\s+units?", re.IGNORECASE)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def normalize_course_id(dept: str, num: str) -> str:
    """'I&C SCI', '31' -> 'I&CSCI31'; 'COMPSCI', '161' -> 'COMPSCI161'"""
    return dept.replace(" ", "") + num


def expand_course_code(raw_code: str) -> list[tuple[str, str]]:
    """
    Parse and expand compound codes.
      'I&C SCI 31-32-33' -> [('I&C SCI','31'), ('I&C SCI','32'), ('I&C SCI','33')]
      'I&C SCI H32-33'   -> [('I&C SCI','H32'), ('I&C SCI','H33')]
      'MATH 2A-2B'       -> [('MATH','2A'), ('MATH','2B')]
      'COMPSCI 161'      -> [('COMPSCI','161')]
    """
    raw_code = raw_code.replace("\xa0", " ")
    m = COMPOUND_COURSE_RE.match(raw_code)
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
    """Returns (requirement_type, is_required)."""
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


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


async def get_page_html() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print("Navigating to catalogue...")
        await page.goto(CATALOGUE_URL, wait_until="networkidle", timeout=30000)

        # Three passes to handle nested toggles
        for _ in range(3):
            for selector in [
                "[aria-expanded='false']",
                "button.collapsed",
                ".toggle_control",
                "a.toggle-course-list",
                "button[data-toggle='collapse']",
                "a[data-toggle='collapse']",
                ".acalog-accordion-link",
            ]:
                try:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        try:
                            await el.click(timeout=500)
                        except Exception:
                            pass
                except Exception:
                    pass
            await page.wait_for_timeout(600)

        # Also expand any literal "+" / "Show" text buttons
        try:
            for btn in await page.query_selector_all("button, a"):
                try:
                    text = (await btn.inner_text()).strip()
                    if text in ("+", "Show", "Expand", "expand all", "show all"):
                        await btn.click(timeout=500)
                except Exception:
                    pass
        except Exception:
            pass

        await page.wait_for_timeout(1500)
        html = await page.content()
        await browser.close()
        return html


def filter_valid_course_ids(rows: list[dict], client) -> list[dict]:
    """Remove rows whose course_id doesn't exist in the courses table (FK guard)."""
    ids_to_check = list({r["course_id"] for r in rows})
    valid: set[str] = set()
    page_size = 500
    for i in range(0, len(ids_to_check), page_size):
        batch = ids_to_check[i : i + page_size]
        result = client.table("courses").select("id").in_("id", batch).execute()
        for row in result.data:
            valid.add(row["id"])
    skipped = sorted(r["course_id"] for r in rows if r["course_id"] not in valid)
    if skipped:
        print(f"  Skipping {len(skipped)} course IDs not found in courses table: {skipped}")
    return [r for r in rows if r["course_id"] in valid]


def extract_requirements(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    req_div = (
        soup.find(id="requirementstextcontainer")
        or soup.find(id="requirementstext")
        or soup.find(attrs={"class": re.compile(r"requirementstext", re.I)})
    )
    if req_div is None:
        print("WARNING: Could not find requirements container — parsing full page.")
        req_div = soup

    rows: list[dict] = []
    tables = req_div.find_all("table", class_="sc_courselist")
    if not tables:
        print("WARNING: No sc_courselist tables found. Page structure may have changed.")
        return rows

    seen: set[tuple] = set()  # (course_id, elective_group) dedup within batch

    for table in tables:
        cur_req_type = "requirement"
        cur_section_req_type = "requirement"  # set by explicit areaheader; restored on reset
        cur_is_required = True
        cur_elective_group: Optional[str] = None
        cur_min_courses: Optional[int] = None
        cur_units: Optional[int] = None
        cur_notes: Optional[str] = None

        tbody = table.find("tbody") or table

        for tr in tbody.find_all("tr"):
            tr_classes = set(tr.get("class", []))

            # --- Area / sub-header row ---
            if "areaheader" in tr_classes or "subheader" in tr_classes:
                td = tr.find("td") or tr.find("th")
                if td:
                    header_text = td.get_text(separator=" ", strip=True)
                    new_req_type, new_is_required = classify_section(header_text)

                    if new_req_type != "requirement":
                        # Explicit type change — reset all section state
                        cur_req_type = new_req_type
                        cur_section_req_type = new_req_type
                        cur_is_required = new_is_required
                        cur_min_courses = None
                        cur_units = None
                        cur_notes = None
                        cur_elective_group = header_text if not new_is_required else None
                    elif not cur_is_required:
                        # Ambiguous heading within an elective section —
                        # new specialization sub-group: update label and reset min_courses
                        cur_elective_group = header_text
                        cur_min_courses = None  # reset; pick-N from header or next note will set it
                        n = parse_pick_n(header_text)
                        if n is not None:
                            cur_min_courses = n
                    # else: ambiguous heading in a required section — treat as sub-label, no change
                continue

            # --- Note / comment row ---
            tds = tr.find_all("td")
            is_comment = len(tds) == 1 or "comment" in " ".join(tr.get("class", []))
            if is_comment:
                text = tr.get_text(separator=" ", strip=True)

                # "B. Complete:" — signals a shift back to required mode
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
                    if not cur_elective_group:
                        cur_elective_group = text[:120]
                else:
                    u_match = UNITS_RE.search(text)
                    if u_match:
                        cur_units = int(u_match.group(1))
                        cur_notes = text
                continue

            # --- Course row ---
            code_td = tr.find("td", class_="codecol")
            if not code_td:
                continue

            raw_code = code_td.get_text(strip=True).replace("\xa0", " ")
            # Strip leading "or" with or without a space before the department code
            raw_code = re.sub(r"^or(?=[A-Z ])", "", raw_code).strip()

            # Per-course units from hourscol
            hours_td = tr.find("td", class_="hourscol")
            units_val = cur_units
            if hours_td:
                h_text = hours_td.get_text(strip=True)
                if h_text.isdigit():
                    units_val = int(h_text)

            pairs = expand_course_code(raw_code)
            if not pairs:
                if raw_code:
                    print(f"  Skipping unparseable code: {raw_code!r}")
                continue

            for dept, num in pairs:
                course_id = normalize_course_id(dept, num)
                dedup_key = (course_id, cur_elective_group)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                rows.append({
                    "course_id": course_id,
                    "major_code": MAJOR_CODE,
                    "major_name": MAJOR_NAME,
                    "requirement_type": cur_req_type,
                    "elective_group": cur_elective_group,
                    "is_required": cur_is_required,
                    "min_courses_required": cur_min_courses if not cur_is_required else None,
                    "units_required": units_val,
                    "notes": cur_notes,
                })

    return rows


def upsert_requirements(rows: list[dict]) -> None:
    if not rows:
        print("No rows to upsert.")
        return
    client = get_client()
    rows = filter_valid_course_ids(rows, client)
    if not rows:
        print("No valid rows to insert after FK filtering.")
        return
    # Delete existing rows for this major, then insert fresh — avoids needing a
    # unique constraint and handles structural changes to the requirements page.
    client.table("major_requirements").delete().eq("major_code", MAJOR_CODE).execute()
    client.table("major_requirements").insert(rows).execute()
    print(f"Inserted {len(rows)} rows into major_requirements.")


async def main() -> None:
    html = await get_page_html()
    print("Page loaded. Parsing requirements...")
    rows = extract_requirements(html)

    if not rows:
        print("No requirements extracted — the page structure may have changed.")
        return

    print(f"\nExtracted {len(rows)} course requirements:")
    for r in rows:
        flag = "required" if r["is_required"] else f"elective (min={r['min_courses_required']})"
        print(f"  {r['course_id']:<25} {r['requirement_type']:<15} {flag}"
              + (f"  [{r['elective_group'][:40]}]" if r["elective_group"] else ""))

    print()
    upsert_requirements(rows)


if __name__ == "__main__":
    asyncio.run(main())
