"""
Course plan generator combining hard and soft constraints.

generate(major_id, completed_courses, graduation_quarter, units_per_quarter=16,
         waived_ges=[])
→ GenerationResult  (variants sorted best-first, feasibility metadata)

Steps inside generate():
  1. Load required + elective course groups for the major AND university-wide
     GE groups (major_id = "ALL_MAJORS"), merging and deduplicating.
  2. Run a feasibility check: raise FeasibilityError if there are not enough
     quarters to schedule all required courses.
  3. Build a topological ordering via prerequisite_tree evaluation (ASAP).
  4. Distribute courses across quarters respecting the unit cap.
  5. Generate 5 variants by perturbing the base plan with different seeds
     and running optimize() on each.  Return the 3 best.
"""

import datetime
import math
import os
import random
import re  # FIX 1
from copy import deepcopy
from dataclasses import dataclass, field

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import (
    CoursePlan,
    UNITS_PER_COURSE,
    _eval_tree,
    _load_aliases,  # FIX 6
    _norm,
    _qkey,
    coreq_split_pairs,
    unit_cap_tiers,
)
from .optimizer import OptimizationResult, optimize, _check_prereqs  # _check_prereqs for prereq-safe _perturb
from .offering_patterns import is_likely_offered
from .soft_constraints import _load_difficulty_scores  # FIX 4

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

# Maximum quarters the dynamic scheduler is allowed to extend beyond the
# user's requested graduation_quarter.  16 = 4 years of UCI quarters.
MAX_QUARTERS = 16


# ── Result / error types ──────────────────────────────────────────────────────

class FeasibilityError(Exception):
    """Raised when the graduation window cannot accommodate all required courses."""

    def __init__(
        self,
        message: str,
        quarters_available: int,
        quarters_needed: int,
        courses_to_complete: int,
        years_to_extend: int,
    ) -> None:
        super().__init__(message)
        self.quarters_available = quarters_available
        self.quarters_needed    = quarters_needed
        self.courses_to_complete = courses_to_complete
        self.years_to_extend    = years_to_extend


@dataclass
class GenerationResult:
    variants:           list[OptimizationResult]
    tight_timeline:     bool = False
    overflow_count:     int  = 0
    quarters_available: int  = 0
    quarters_needed:    int  = 0
    # requirement_group → list of course IDs selected from that group
    group_map:          dict[str, list[str]] = field(default_factory=dict)
    # Courses that could not be scheduled even after dynamic window extension.
    overflow_courses:   list[str] = field(default_factory=list)
    # How many extra quarters were appended beyond graduation_quarter (0 = none needed).
    extended_by:        int = 0
    # Course IDs satisfied by AP credit (excluded from the plan).
    ap_credited_courses: list[str] = field(default_factory=list)
    # Total UCI units awarded via AP credit.
    ap_units_awarded:    int = 0
    # Unresolved "choose N of M" decisions surfaced to the user (editor model).
    # The seed places only required courses; electives/GEs are returned here, NOT placed.
    choice_groups:       list[dict] = field(default_factory=list)


# ── Quarter helpers ───────────────────────────────────────────────────────────

def _current_quarter() -> str:
    """Return the active UCI quarter as 'YYYY_quarter' (winter/spring/fall)."""
    today = datetime.date.today()
    m, y = today.month, today.year
    if m <= 3:
        return f"{y}_winter"
    if m <= 6:
        return f"{y}_spring"
    return f"{y}_fall"


def _generate_quarters(graduation_quarter: str, start_quarter: str | None = None) -> list[str]:
    """Quarters from the start quarter through graduation_quarter inclusive.

    Uses the standard UCI sequence (winter, spring, fall) and skips summer.
    graduation_quarter must be in 'YYYY_quarter' format, e.g. '2029_spring'.

    start_quarter pins the first quarter of the window (e.g. '2026_fall').  The
    planner grid is a fixed, Fall-anchored structure, so the caller passes the
    grid's first quarter here to keep the optimizer window identical to what the
    grid renders — otherwise courses get scheduled into off-grid quarters (e.g.
    the real-clock 'YYYY_spring') that have no cell and silently disappear.  When
    omitted, falls back to the real current quarter (legacy/date-driven callers).
    """
    seq = ["winter", "spring", "fall"]
    current    = start_quarter or _current_quarter()
    start_year = int(current.split("_")[0])
    start_q    = current.split("_")[1]
    grad_q     = graduation_quarter.split("_")[1]

    if grad_q not in seq:
        raise ValueError(
            f"graduation_quarter must end in winter/spring/fall, got {graduation_quarter!r}"
        )

    try:
        idx = seq.index(start_q)
    except ValueError:
        idx = 2

    quarters: list[str] = []
    year = start_year
    grad_year = int(graduation_quarter.split("_")[0])

    while True:
        q    = seq[idx]
        qstr = f"{year}_{q}"
        quarters.append(qstr)
        if qstr == graduation_quarter:
            break
        idx = (idx + 1) % len(seq)
        if idx == 0:
            year += 1
        if year > grad_year + 1:   # safety: stop if we've overshot
            break

    return quarters


def _next_quarter(q: str) -> str:
    """Return the UCI quarter immediately after q.

    Sequence: winter → spring → fall → (winter of next year).
    """
    seq = ["winter", "spring", "fall"]
    year, season = q.rsplit("_", 1)
    idx = seq.index(season)
    next_idx = (idx + 1) % len(seq)
    next_year = int(year) + (1 if next_idx == 0 else 0)
    return f"{next_year}_{seq[next_idx]}"


# ── Course unit helper ────────────────────────────────────────────────────────

def _fetch_course_units(client, course_ids: list[str]) -> dict[str, int]:
    """Return {course_id: min_units} from the courses table.

    Falls back to UNITS_PER_COURSE for any course whose min_units is NULL.
    Used by _asap_schedule so that real unit totals (not assumed 4 per course)
    are checked against the per-quarter unit cap.
    """
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,min_units")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {
        r["id"]: max(1, int(r["min_units"]))
        for r in rows
        if r.get("min_units") is not None
    }


# ── Course term helper (FIX 5) ───────────────────────────────────────────────

def _fetch_course_terms(client, course_ids: list[str]) -> dict[str, list[str]]:
    """Return {course_id: [term_strings]} for each course, e.g. ['2026 Fall', '2025 Winter']."""
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,terms")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {r["id"]: (r.get("terms") or []) for r in rows}


def _fetch_ge_course_norms(client, course_ids: list[str]) -> set[str]:
    """Return the normalized ids of courses that satisfy at least one GE category.

    A course is a GE course iff its ``ge_list`` is non-empty.  Used by the hard
    GE-earliness deadline in _asap_schedule.  Plan ids arrive in either the spaced
    catalogue form ("I&C SCI 46") or the stripped DB form ("I&CSCI46"); query both
    so spaced GE ids still match (same pattern as soft_constraints._load_course_meta).
    """
    if not course_ids:
        return set()
    query_ids: set[str] = set()
    for cid in course_ids:
        query_ids.add(cid)
        query_ids.add(cid.replace(" ", "").upper())
    rows = (
        client.table("courses")
        .select("id,ge_list")
        .in_("id", list(query_ids))
        .execute()
        .data
    )
    return {_norm(r["id"]) for r in rows if r.get("ge_list")}


# ── Prereq depth helper (FIX 4) ──────────────────────────────────────────────

def _get_prereq_depths(client, norm_ids: list[str]) -> dict[str, int]:
    """Count direct prereq edges per course as a proxy for scheduling complexity."""
    if not norm_ids:
        return {}
    rows = (
        client.table("prereq_edges")
        .select("course_id")
        .in_("course_id", norm_ids)
        .execute()
        .data
    )
    counts: dict[str, int] = {}
    for row in rows:
        n = _norm(row["course_id"])
        counts[n] = counts.get(n, 0) + 1
    return counts


# ── Requirement loading ───────────────────────────────────────────────────────

def _resolve_ap_credits(
    client,
    ap_scores: dict[str, int],
    completed_norm: set[str],
) -> tuple[list[str], int]:
    """
    Look up ap_credits rows for the given ap_scores dict.
    Returns (credited_course_ids, total_units_awarded).

    A score of N grants credit from all rows where ap_score <= N, so a user
    with score 5 automatically inherits the 3-row and 4-row equivalencies too.
    Credited courses are added to completed_norm in-place.
    """
    if not ap_scores:
        return [], 0

    exam_names = list(ap_scores.keys())
    try:
        rows = (
            client.table("ap_credits")
            .select("ap_course_name,ap_score,units_awarded,course_equivalencies")
            .in_("ap_course_name", exam_names)
            .execute()
            .data
        ) or []
    except Exception as exc:
        print(f"[AP credits] WARNING: fetch failed: {exc}")
        return [], 0

    credited: list[str] = []
    total_units = 0
    seen_units: set[str] = set()  # track per-exam to avoid double-counting units

    for row in rows:
        exam   = row["ap_course_name"]
        needed = ap_scores.get(exam)
        if needed is None or row["ap_score"] > needed:
            continue

        # Count units for the highest qualifying row only (one entry per exam)
        units_key = f"{exam}:{row['ap_score']}"
        if units_key not in seen_units:
            seen_units.add(units_key)
            total_units += row.get("units_awarded") or 0

        # Encode exam satisfaction so _eval_item can check it via `available`.
        # Token format: "EXAMOK:<normed_exam_name>:<ap_score_threshold>"
        # A user with score N satisfies all rows where row ap_score <= N, so we
        # add one token per qualifying row.  _eval_item checks the minGrade in
        # the prereq tree against these tokens.
        completed_norm.add(f"EXAMOK:{_norm(exam)}:{row['ap_score']}")

        for cid in (row.get("course_equivalencies") or []):
            norm = _norm(cid)
            if norm not in completed_norm:
                completed_norm.add(norm)
                credited.append(cid)

    if credited:
        print(f"[AP credits] {len(credited)} courses credited from {len(ap_scores)} exams "
              f"(~{total_units} units): {credited}")

    return credited, total_units


def _load_requirement_rows(
    client,
    major_id: str,
    completed_norm: set[str],
    ap_scores: dict[str, int] | None = None,
) -> tuple[list[dict], list[dict], list[str], int]:
    """Load + merge requirement rows for a major.

    Returns (major_rows, ge_rows, ap_credited, ap_units).  Resolves AP credit
    (adding equivalencies to completed_norm in-place), merges the specialization
    parent rows and scraped catalogue_requirements, and fetches university-wide
    GE rows (major_id = 'ALL_MAJORS').  Shared by _collect_courses (flat N-pick)
    and collect_requirements (required/choice split).
    """
    # Resolve AP credit before loading requirements so equivalencies are excluded
    ap_credited, ap_units = _resolve_ap_credits(client, ap_scores or {}, completed_norm)
    major_rows = (
        client.table("major_requirements")
        .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", major_id)
        .execute()
        .data
    )

    # FIX 1: Specialization parent merge — if major_id ends with a letter suffix
    # (e.g. "BS-075A") also load the parent ("BS-075") and merge, deduplicating
    # by requirement_group + group_name so the spec's own rows take precedence.
    if re.search(r"[A-Z]$", major_id):
        parent_id = major_id[:-1]
        parent_rows = (
            client.table("major_requirements")
            .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
            .eq("major_id", parent_id)
            .execute()
            .data
        )
        existing_keys: set[str] = {
            (r.get("requirement_group") or "") + "|" + (r.get("group_name") or "")
            for r in major_rows
        }
        before_count = len(major_rows)
        for row in parent_rows:
            key = (row.get("requirement_group") or "") + "|" + (row.get("group_name") or "")
            if key not in existing_keys:
                major_rows.append(row)
                existing_keys.add(key)
        print(
            f"[FIX 1] Spec {major_id!r}: {before_count} spec rows + "
            f"{len(parent_rows)} parent ({parent_id!r}) rows → "
            f"{len(major_rows)} total after dedup"
        )
    else:
        print(f"[FIX 1] {major_id!r}: {len(major_rows)} rows (no parent merge needed)")

    # Catalogue merge — supplement/override API rows with scraped catalogue data.
    # The catalogue scraper uses its own major_id values that may differ from the
    # Anteater API IDs (e.g. Applied Math stores all specs under "BS-0K6C" while
    # the API uses "BS-0K6A" for Data Science).  Resolve dynamically:
    #   1. Look up major_name + specialization_name for this major_id
    #   2. Find which sibling major_id (same major_name) has catalogue rows
    #   3. Derive the spec slug from specialization_name (e.g. "Data Science" → "data-science")
    try:
        # ── Step 1: resolve catalogue major_id and spec slug ──────────────────
        cat_major_id = major_id
        cat_spec_id: str | None = None

        name_meta = (
            client.table("major_requirements")
            .select("major_name,specialization_name")
            .eq("major_id", major_id)
            .limit(1)
            .execute()
            .data
        )
        if name_meta:
            major_name = (name_meta[0].get("major_name") or "").strip()
            spec_name  = (name_meta[0].get("specialization_name") or "").strip()
            if spec_name:
                cat_spec_id = spec_name.lower().replace(" ", "-")

            # Find all major_ids sharing this program name, then probe catalogue
            sibling_rows = (
                client.table("major_requirements")
                .select("major_id")
                .eq("major_name", major_name)
                .execute()
                .data
            )
            sibling_ids = sorted({r["major_id"] for r in sibling_rows})
            for sid in sibling_ids:
                probe = (
                    client.table("catalogue_requirements")
                    .select("major_id")
                    .eq("major_id", sid)
                    .limit(1)
                    .execute()
                    .data
                )
                if probe:
                    cat_major_id = sid
                    break

        # ── Step 2: fetch core + spec catalogue rows ──────────────────────────
        cat_core = (
            client.table("catalogue_requirements")
            .select("group_name,requirement_type,courses,courses_needed")
            .eq("major_id", cat_major_id)
            .is_("specialization_id", "null")
            .execute()
            .data
        ) or []

        cat_spec: list[dict] = []
        if cat_spec_id:
            cat_spec = (
                client.table("catalogue_requirements")
                .select("group_name,requirement_type,courses,courses_needed")
                .eq("major_id", cat_major_id)
                .eq("specialization_id", cat_spec_id)
                .execute()
                .data
            ) or []

        cat_rows = cat_core + cat_spec
        if cat_rows:
            cat_shaped = [
                {
                    "requirement_group": r["group_name"],
                    "group_name":        r["group_name"],
                    "requirement_type":  r["requirement_type"],
                    "courses":           r["courses"],
                    "courses_needed":    r["courses_needed"],
                    "waivable":          False,
                }
                for r in cat_rows
            ]
            cat_names = {r["group_name"] for r in cat_rows}
            major_rows = [r for r in major_rows if r.get("group_name") not in cat_names]
            major_rows.extend(cat_shaped)
            print(
                f"[catalogue] {major_id!r} → cat={cat_major_id!r}: merged {len(cat_rows)} rows "
                f"({len(cat_core)} core + {len(cat_spec)} spec={cat_spec_id!r})"
            )
        else:
            print(f"[catalogue] {major_id!r} → cat={cat_major_id!r}: no catalogue rows found")
    except Exception as _cat_exc:
        print(f"[catalogue] WARNING: catalogue_requirements fetch failed: {_cat_exc}")

    ge_rows = (
        client.table("major_requirements")
        .select("requirement_group,group_name,requirement_type,courses,courses_needed,waivable")
        .eq("major_id", "ALL_MAJORS")
        .execute()
        .data
    )

    return major_rows, ge_rows, ap_credited, ap_units


def _collect_courses(
    client,
    major_id: str,
    completed_norm: set[str],
    waived_ges: list[str],
    specialization_id: str | None = None,
    ap_scores: dict[str, int] | None = None,
) -> tuple[list[str], dict[str, list[str]], list[str], int]:
    """Return (flat_list, group_map, ap_credited, ap_units) of courses still needed.

    Queries both the major's own requirements and the university-wide GE rows
    (major_id = "ALL_MAJORS").  GE groups whose requirement_group appears in
    waived_ges are skipped entirely.

    Also merges rows from catalogue_requirements (scraped directly from the UCI
    catalogue) when available.  Catalogue rows with the same group_name as an
    API row take precedence; new group_names are appended.

    AP scores are resolved first: their course equivalencies are added to
    completed_norm so the planner treats them as already satisfied.

    group_map maps requirement_group → list of course IDs selected from it,
    which callers can use to audit GE coverage.
    """
    # Load + merge requirement rows (AP resolve, spec parent, catalogue, GE).
    major_rows, ge_rows, ap_credited, ap_units = _load_requirement_rows(
        client, major_id, completed_norm, ap_scores
    )

    selected: list[str] = []
    seen: set[str] = set()
    group_map: dict[str, list[str]] = {}

    # FIX 4: load difficulty scores once for elective sorting
    diff_scores = _load_difficulty_scores()

    def _pick(req: dict, use_plan_norm: set[str] | None = None) -> None:
        """Process one requirement row, appending chosen courses to selected/seen/group_map.

        use_plan_norm: when set, already_satisfied counts courses from this set
        (major-row snapshot) plus completed, not the live `seen` cursor.  This
        prevents GE rows from under-counting when alias mismatches hide coverage
        from major rows.  Candidate filtering still uses `seen` to avoid re-picks.
        """
        req_group = req.get("requirement_group") or ""
        waivable  = req.get("waivable", False)

        if waivable and req_group in waived_ges:
            return

        course_list: list[str] = req.get("courses") or []
        needed: int = req.get("courses_needed") or len(course_list)

        if use_plan_norm is not None:
            # FIX 2a: GE rows — count satisfaction using the live `seen` set
            # (which includes major rows AND previously processed GE rows), so that
            # a GE course selected by an earlier GE row isn't re-counted here.
            # `use_plan_norm` is kept for the 2b overlap detection print but not
            # used for the count because plan_courses_norm would miss GE-to-GE picks.
            ge_pool_norm = {_norm(c) for c in course_list}
            already_satisfied = len(ge_pool_norm & (completed_norm | seen))
            still_needed = max(0, needed - already_satisfied)
        else:
            already_have = sum(
                1 for c in course_list
                if _norm(c) in completed_norm or _norm(c) in seen
            )
            still_needed = max(0, needed - already_have)

        if still_needed == 0:
            return

        candidates = [
            c for c in course_list
            if _norm(c) not in completed_norm and _norm(c) not in seen
        ]

        req_type_str = (req.get("requirement_type") or "required").lower()
        if "elective" in req_type_str:
            # FIX 4: elective rows — sort by difficulty ASC, prereq_depth ASC, alpha.
            # Simpler courses (lower difficulty, fewer upstream prereqs) slot earlier.
            prereq_counts = _get_prereq_depths(client, [_norm(c) for c in candidates])
            candidates.sort(key=lambda c: (
                diff_scores.get(_norm(c), 5.0),
                prereq_counts.get(_norm(c), 0),
                c,
            ))
        else:
            # Prefer I&CSCI variants so prereq trees (which use "I&C SCI" notation)
            # resolve correctly via _norm.
            candidates.sort(
                key=lambda c: (0 if "I&CSCI" in c or "ICS" in _norm(c)[:3] else 1, c)
            )

        chosen = candidates[:still_needed]
        for course in chosen:
            selected.append(course)
            seen.add(_norm(course))
        if chosen:
            # FIX 2: accumulate instead of overwriting so multiple rows for the
            # same requirement_group all appear in the map.
            group_map.setdefault(req_group, []).extend(chosen)

    # Process major rows first (required + elective + major-specific GE)
    for req in major_rows:
        _pick(req)

    # FIX 2: snapshot after major rows so GE rows can see what is already covered
    plan_courses_norm = set(seen)

    # FIX 2b: identify required rows whose pool overlaps >50% with any GE pool
    all_ge_norms: set[str] = set()
    for ge_req in ge_rows:
        all_ge_norms.update(_norm(c) for c in (ge_req.get("courses") or []))

    for req in major_rows:
        if (req.get("requirement_type") or "").lower() == "required":
            pool = {_norm(c) for c in (req.get("courses") or [])}
            if pool:
                overlap = len(pool & all_ge_norms) / len(pool)
                if overlap > 0.5:
                    print(
                        f"[FIX 2b] Required row {req.get('group_name')!r} treated as "
                        f"GE-satisfying ({overlap:.0%} overlap with GE pools)"
                    )

    # Print GE course counts before GE processing (for test visibility)
    print(f"[FIX 2] Before GE rows: plan has {len(selected)} major courses")

    # Process GE rows using the major-row snapshot for already_satisfied
    for req in ge_rows:
        _pick(req, use_plan_norm=plan_courses_norm)

    # FIX 2: Print GE group counts after processing
    print(f"[FIX 2] GE group counts after processing ({major_id!r}):")
    for ge_req in ge_rows:
        rg = ge_req.get("requirement_group") or ""
        if rg in group_map:
            entries = group_map[rg]
            print(f"  {rg}: {len(entries)} course(s) → {entries}")

    return selected, group_map, ap_credited, ap_units


# ── Required / choice split (editor model) ────────────────────────────────────

_SEASONS = ("Winter", "Spring", "Summer", "Fall")


def _seasons_offered(terms: list[str]) -> list[str]:
    """Reduce a course's historical term list to the distinct seasons it's offered.

    e.g. ['2025 Fall', '2026 Winter', '2026 Fall'] → ['Winter', 'Fall'].
    """
    found: set[str] = set()
    for t in terms or []:
        parts = t.split(" ", 1)
        if len(parts) == 2:
            season = parts[1].strip().capitalize()
            for s in _SEASONS:
                if season.startswith(s):
                    found.add(s)
                    break
    return [s for s in _SEASONS if s in found]


def _is_choice_row(req: dict) -> bool:
    """A requirement row is a *choice* when fewer courses are needed than offered.

    courses_needed < len(courses)  → choose N of M  (elective pool, GE pool,
    "X or Y" alternatives).  Otherwise every listed course must be taken (take-all),
    which is a hard requirement seeded into the plan.
    """
    courses = req.get("courses") or []
    needed = req.get("courses_needed")
    if not courses:
        return False
    if needed is None:
        return False  # needed unspecified → take all
    return needed < len(courses)


def collect_requirements(
    client,
    major_id: str,
    completed_norm: set[str],
    waived_ges: list[str] | None = None,
    specialization_id: str | None = None,
    ap_scores: dict[str, int] | None = None,
) -> tuple[list[str], list[dict], list[str], int]:
    """Split a major's requirements into REQUIRED courses vs. CHOICE groups.

    Editor model: the system seeds only mandatory courses; "choose N of M" pools
    (electives, GE categories, "X or Y" alternatives) are surfaced for the user to
    resolve, NOT auto-picked.

    Returns (required_courses, choice_groups, ap_credited, ap_units):

      required_courses : flat list of take-all course IDs (every student must take),
                         minus completed / AP-credited, de-duplicated.
      choice_groups    : list of unresolved decisions, each:
          { "group_id", "label", "choose_n",
            "options": [ {course_id, title, units, difficulty, terms_offered,
                          has_prereqs}, ... ] }

    Choice-group options are NOT placed into any plan.
    """
    waived_ges = waived_ges or []
    _load_aliases(client)  # FIX 6

    major_rows, ge_rows, ap_credited, ap_units = _load_requirement_rows(
        client, major_id, completed_norm, ap_scores
    )
    diff_scores = _load_difficulty_scores()

    required: list[str] = []
    required_seen: set[str] = set()
    choice_rows: list[dict] = []

    def _consume(req: dict) -> None:
        req_group = req.get("requirement_group") or req.get("group_name") or ""
        if req.get("waivable", False) and req_group in waived_ges:
            return
        courses = req.get("courses") or []
        if not courses:
            return
        if _is_choice_row(req):
            choice_rows.append(req)
            return
        # Take-all row: every course is mandatory.
        for c in courses:
            nc = _norm(c)
            if nc in completed_norm or nc in required_seen:
                continue
            required.append(c)
            required_seen.add(nc)

    for req in major_rows:
        _consume(req)
    for req in ge_rows:
        _consume(req)

    # ── Enrich choice options with course metadata (one batched query) ──────────
    all_option_ids = sorted({
        c for req in choice_rows for c in (req.get("courses") or [])
        if _norm(c) not in completed_norm
    })
    meta: dict[str, dict] = {}
    if all_option_ids:
        rows = (
            client.table("courses")
            .select("id,title,min_units,terms,prerequisite_tree")
            .in_("id", all_option_ids)
            .execute()
            .data
        )
        meta = {_norm(r["id"]): r for r in rows}

    choice_groups: list[dict] = []
    for req in choice_rows:
        courses = req.get("courses") or []
        needed = req.get("courses_needed") or 1
        # Drop options already completed / AP-credited; reduce N accordingly.
        already = sum(1 for c in courses if _norm(c) in completed_norm)
        choose_n = max(0, needed - already)
        options_ids = [c for c in courses if _norm(c) not in completed_norm]
        if choose_n == 0 or not options_ids:
            continue  # group already satisfied by completed courses

        options = []
        for c in options_ids:
            m = meta.get(_norm(c), {})
            options.append({
                "course_id":     c,
                "title":         m.get("title"),
                "units":         m.get("min_units") or UNITS_PER_COURSE,
                "difficulty":    diff_scores.get(_norm(c)),  # normalized 1-10 or None
                "terms_offered": _seasons_offered(m.get("terms") or []),
                "has_prereqs":   bool(m.get("prerequisite_tree")),
            })

        choice_groups.append({
            "group_id":  req.get("requirement_group") or req.get("group_name") or "",
            "label":     req.get("group_name") or req.get("requirement_group") or "Choice",
            "choose_n":  choose_n,
            "options":   options,
        })

    return required, choice_groups, ap_credited, ap_units


def get_requirements_state(
    plan: CoursePlan,
    waived_ges: list[str] | None = None,
    ap_scores: dict[str, int] | None = None,
) -> dict:
    """Report what the user still needs to decide for `plan`'s major.

    Returns:
        {
          "required_placed": [course_ids in the seed that are mandatory],
          "choice_groups":   [ each choice group from collect_requirements, plus
                               "placed" and "remaining" (choose_n minus options
                               already placed) ],
          "all_satisfied":   bool  — every group's remaining == 0 AND every
                               required course is placed.
        }
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    _load_aliases(client)  # FIX 6
    completed_norm = {_norm(c) for c in plan.completed_courses}

    required, choice_groups, _, _ = collect_requirements(
        client, plan.major_id, completed_norm, waived_ges or [], None, ap_scores
    )

    # Mandatory backbone = required take-all courses + their injected prereq chains.
    # Backbone courses commonly ALSO appear as options inside elective/GE pools
    # (e.g. a required upper-div course is listed in "11 Upper-Div Electives"); they
    # must NOT be counted as the user having "chosen" that elective, else big pools
    # look satisfied at the seed.  Backbone membership is the source of truth for
    # what's mandatory vs. a genuine user choice.
    trees = _fetch_prereq_trees(client, required)
    backbone, _ = _resolve_implicit_prereqs(list(required), trees, completed_norm, client)
    backbone_norm = {_norm(c) for c in backbone}

    planned_norm = {
        _norm(c) for cs in plan.planned_courses.values() for c in cs
    }

    # Mandatory courses currently placed in the plan.
    required_placed = [
        c for cs in plan.planned_courses.values() for c in cs
        if _norm(c) in backbone_norm
    ]

    annotated: list[dict] = []
    for g in choice_groups:
        # Count only genuine user picks: options placed that are NOT backbone courses.
        placed = sum(
            1 for o in g["options"]
            if _norm(o["course_id"]) in planned_norm
            and _norm(o["course_id"]) not in backbone_norm
        )
        remaining = max(0, g["choose_n"] - placed)
        annotated.append({**g, "placed": placed, "remaining": remaining})

    required_missing = [
        c for c in backbone
        if _norm(c) not in planned_norm and _norm(c) not in completed_norm
    ]
    all_satisfied = (
        not required_missing
        and all(g["remaining"] == 0 for g in annotated)
    )

    return {
        "required_placed": required_placed,
        "choice_groups":   annotated,
        "all_satisfied":   all_satisfied,
    }


# ── Feasibility check ─────────────────────────────────────────────────────────

def _check_feasibility(
    total_courses: int,
    quarters: list[str],
    units_per_quarter: int,
    graduation_quarter: str,
) -> tuple[bool, int, int]:
    """Return (tight_timeline, quarters_available, quarters_needed).

    Raises FeasibilityError only when even the 4-year (MAX_QUARTERS) extension
    cannot mathematically fit all courses.  Window-too-short cases where
    quarters_needed <= MAX_QUARTERS are handled by the dynamic extension loop in
    generate() — no error is raised for those.

    tight_timeline is True when fewer than 2 quarters of slack remain between
    the user's requested graduation_quarter and the pure-capacity minimum.
    """
    max_per_q          = units_per_quarter // UNITS_PER_COURSE
    quarters_available = len(quarters)
    quarters_needed    = math.ceil(total_courses / max_per_q) if max_per_q else 0

    if quarters_needed > MAX_QUARTERS:
        # Truly infeasible: even 4 years of quarters can't hold this many courses.
        excess_q            = quarters_needed - MAX_QUARTERS
        courses_to_complete = excess_q * max_per_q
        years_to_extend     = math.ceil(excess_q / 3)
        raise FeasibilityError(
            f"Cannot fit {total_courses} course(s) in even {MAX_QUARTERS} quarters "
            f"(4-year maximum). "
            f"Complete {courses_to_complete} more courses first "
            f"or extend graduation by {years_to_extend} year(s).",
            quarters_available=quarters_available,
            quarters_needed=quarters_needed,
            courses_to_complete=courses_to_complete,
            years_to_extend=years_to_extend,
        )

    tight = (quarters_available - quarters_needed) <= 1
    return tight, quarters_available, quarters_needed


# ── Prereq data ───────────────────────────────────────────────────────────────

def _fetch_prereq_trees(client, course_ids: list[str]) -> dict[str, dict]:
    """Return {norm_id: prerequisite_tree} for each course that has one."""
    if not course_ids:
        return {}
    rows = (
        client.table("courses")
        .select("id,prerequisite_tree")
        .in_("id", course_ids)
        .execute()
        .data
    )
    return {
        _norm(r["id"]): r["prerequisite_tree"]
        for r in rows
        if r.get("prerequisite_tree")
    }


def _required_norms_from_tree(tree: dict, all_norm: set[str]) -> set[str]:
    """Return course norms that are AND-required by tree but absent from all_norm.

    AND: collect missing from every child.
    OR:  if any alternative is already in all_norm, nothing is required.
         Otherwise, collect missing from the first course alternative.
    NOT/exam: ignored.
    """
    for key in ("AND", "OR", "NOT"):
        if key not in tree:
            continue
        items = tree[key]
        if key == "AND":
            result: set[str] = set()
            for item in items:
                result |= _required_norms_from_item(item, all_norm)
            return result
        if key == "OR":
            for item in items:
                if item.get("prereqType") == "course" and _norm(item.get("courseId", "")) in all_norm:
                    return set()
            for item in items:
                if item.get("prereqType") == "course":
                    cid = _norm(item.get("courseId", ""))
                    if cid not in all_norm:
                        return {cid}
            return set()
        if key == "NOT":
            return set()
    return set()


def _required_norms_from_item(item: dict, all_norm: set[str]) -> set[str]:
    t = item.get("prereqType")
    if t == "course":
        cid = _norm(item.get("courseId", ""))
        return set() if cid in all_norm else {cid}
    if t == "exam":
        return set()
    return _required_norms_from_tree(item, all_norm)


def _resolve_implicit_prereqs(
    courses_to_plan: list[str],
    trees: dict[str, dict],
    completed_norm: set[str],
    client,
) -> tuple[list[str], dict[str, dict]]:
    """Inject prerequisite courses that are required but missing from courses_to_plan.

    Iterates until stable. For each course with a prereq tree, finds AND-required
    prerequisites that are not in the plan or completed, then fetches and adds them.
    This handles cases where a requirement group only picks 1 course from a sequence
    (e.g. ICS31) but downstream courses need the full chain (ICS32 → ICS33).
    """
    courses_set = list(courses_to_plan)
    all_norm = {_norm(c) for c in courses_set} | completed_norm

    changed = True
    while changed:
        changed = False
        missing_norms: set[str] = set()
        for norm_id, tree in trees.items():
            missing_norms |= _required_norms_from_tree(tree, all_norm)

        if not missing_norms:
            break

        # Convert norm IDs back to DB-style IDs to look up.
        # Prereq trees use "I&C SCI 33" → norm "ICS33"; DB stores "I&CSCI33".
        lookup_ids = []
        for n in missing_norms:
            lookup_ids.append(n)
            if n.startswith("ICS"):
                lookup_ids.append(n.replace("ICS", "I&CSCI", 1))

        rows = (
            client.table("courses")
            .select("id,prerequisite_tree")
            .in_("id", lookup_ids)
            .execute()
            .data
        )
        for row in rows:
            raw_id = row["id"]
            n = _norm(raw_id)
            if n not in all_norm:
                courses_set.append(raw_id)
                all_norm.add(n)
                if row.get("prerequisite_tree"):
                    trees[n] = row["prerequisite_tree"]
                changed = True

    return courses_set, trees


# ── Coreq helper ─────────────────────────────────────────────────────────────

def _collect_and_coreq_norms(tree: dict | None) -> set[str]:
    """Return the normed IDs of all AND-required corequisite courses in a tree.

    Only AND-linked coreqs are returned — an OR branch means the coreq is
    optional (one of several alternatives), so we don't force it.
    """
    if not tree:
        return set()
    result: set[str] = set()
    if "AND" in tree:
        for item in tree["AND"]:
            if item.get("prereqType") == "course" and item.get("coreq"):
                result.add(_norm(item.get("courseId", "")))
            elif "AND" in item or "OR" in item:
                result |= _collect_and_coreq_norms(item)
    return result


# ── ASAP scheduler ────────────────────────────────────────────────────────────

# Hard GE-earliness deadline: a GE course must land no later than this 0-indexed
# quarter (5 = end of Year 2: Fall Y1, Winter Y1, Spring Y1, Fall Y2, Winter Y2,
# Spring Y2).  Enforced in _asap_schedule.  The ONLY exception is a GE whose
# prerequisites are not themselves schedulable within Y1-Y2 ("prereq blocked").
GE_LAST_QUARTER_IDX = 5


def _asap_schedule(
    courses: list[str],
    prereq_trees: dict[str, dict],
    quarters: list[str],
    units_per_quarter: int,
    completed_norm: set[str],
    terms_by_course: dict[str, list[str]] | None = None,
    units_by_course: dict[str, int] | None = None,
    ge_norms: set[str] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Schedule each course in the earliest quarter its prerequisites allow.

    Returns (plan_dict, overflow) where overflow contains courses that could
    not be placed within the graduation window.

    Placement respects two constraints per quarter:
    - Term availability (FIX 5): course must be historically offered that season.
    - Unit cap: cumulative units of placed courses must not exceed units_per_quarter.
      Actual min_units from the courses table are used when units_by_course is
      provided; otherwise UNITS_PER_COURSE (4) is assumed per course.

    Hard GE earliness (CHANGE 1): when ``ge_norms`` is supplied, GE-satisfying
    courses are subject to a hard deadline — they may not be placed beyond quarter
    index ``GE_LAST_QUARTER_IDX`` (end of Year 2).  Within the Y1-Y2 window GEs are
    sorted AHEAD of non-GE upper-division electives so they claim early slots
    before those electives ("make room for the GE before non-GE electives").  A GE
    whose prerequisites only become available after Y2 is genuinely "prereq
    blocked": such a GE is allowed to land in Y3 at its earliest feasible slot and
    is flagged.  A GE that WAS prereq-ready during Y1-Y2 but didn't fit (capacity)
    is held out of Y3+ entirely — pure-elective GEs must land in Y1 or Y2.
    """
    ge_norms = ge_norms or set()
    available: set[str] = set(completed_norm)
    remaining: list[str] = list(courses)
    plan: dict[str, list[str]] = {q: [] for q in quarters}

    # GE norms that became prereq-ready (prereq tree satisfiable) at some point
    # during the Y1-Y2 window.  Used to tell a capacity-blocked GE (was ready, must
    # NOT spill into Y3) from a prereq-blocked GE (never ready in Y1-Y2, may spill).
    ge_prereq_ready: set[str] = set()
    # Prereq-blocked GEs that were allowed to land after Y2 (for the flag print).
    late_ge_blocked: set[str] = set()

    def _course_units(course: str) -> int:
        """Actual units for a course, falling back to the global assumed constant."""
        if units_by_course:
            return (units_by_course.get(course)
                    or units_by_course.get(_norm(course))
                    or UNITS_PER_COURSE)
        return UNITS_PER_COURSE

    def _offered_in_quarter(course: str, quarter: str) -> bool:
        """Return True if course is offered in this quarter's season type."""
        if not terms_by_course:
            return True
        terms = terms_by_course.get(course) or []
        if not terms:
            return True  # empty → offered all quarters
        q_season = quarter.split("_", 1)[1].capitalize()  # "2026_fall" → "Fall"
        return any(
            len(t.split(" ", 1)) == 2
            and t.split(" ", 1)[1].strip().capitalize() == q_season
            for t in terms
        )

    def _placement_key(course_id: str) -> tuple[int, int, str]:
        """Candidate ordering: lower-division courses (course number < 100) first
        so they unlock their upper-div successors early; then, AMONG upper-division
        courses only, GE-satisfying courses ahead of non-GE electives so GEs claim
        scarce Y1-Y2 slots before deadline-free electives.  Lower-div ordering is
        left untouched (alpha) so required prereq chains aren't displaced by a
        lower-div GE.  Alphabetical final tiebreak keeps output stable.
        """
        m = re.search(r'\d+', course_id)
        num = int(m.group(0)) if m else 9999
        is_lower = 0 if num < 100 else 1
        ge_rank = (0 if _norm(course_id) in ge_norms else 1) if is_lower else 0
        return (is_lower, ge_rank, course_id)

    for q_idx, quarter in enumerate(quarters):
        while remaining:
            q_units = sum(_course_units(c) for c in plan[quarter])
            if q_units >= units_per_quarter:
                break  # quarter is full by unit budget

            same_q_norms: frozenset[str] = frozenset(_norm(x) for x in plan[quarter])

            # Build a norm→raw index over remaining for O(1) coreq lookup.
            remaining_norm_to_raw: dict[str, str] = {}
            for raw in remaining:
                n = _norm(raw)
                if n not in remaining_norm_to_raw:
                    remaining_norm_to_raw[n] = raw

            # Collect all eligible candidates for this quarter slot, then pick the
            # highest-priority one via _placement_key (lower-div first; GEs ahead of
            # non-GE upper-div electives).

            # Norms that are an AND-coreq of some still-unplaced course.  Such a
            # course (e.g. MATH105LA, the lab coreq of MATH105A) must NEVER be
            # placed on its own in an earlier quarter — coreqs have to share a
            # quarter.  It is pulled into its partner's quarter via
            # coreqs_to_place below, so we skip it as a standalone candidate here.
            # Once the partner is placed/completed it drops out of this set and
            # any leftover can be scheduled normally.
            coreq_target_norms: set[str] = set()
            for other in remaining:
                coreq_target_norms |= _collect_and_coreq_norms(prereq_trees.get(_norm(other)))

            eligible_candidates: list[tuple[str, list[str]]] = []

            for c in remaining:
                if _norm(c) in coreq_target_norms:
                    continue  # only placeable alongside its coreq partner

                tree = prereq_trees.get(_norm(c))

                # Identify coreqs that aren't yet satisfied (prior or current quarter)
                required_coreqs = _collect_and_coreq_norms(tree)
                unresolved = [
                    n for n in required_coreqs
                    if n not in available and n not in same_q_norms
                ]

                # Optimistic prereq check: assume unresolved coreqs will be placed
                # in this quarter so their coreq leaves evaluate to True.
                hypothetical_q = same_q_norms | set(unresolved)
                if tree and not _eval_tree(tree, available, hypothetical_q):
                    continue  # non-coreq prereq still missing — truly ineligible

                # Hard GE earliness deadline (CHANGE 1).  Once a GE's prereqs are
                # satisfiable it is "prereq-ready"; a prereq-ready GE must land by
                # the end of Year 2 (GE_LAST_QUARTER_IDX).  Beyond that, only a GE
                # that was NEVER prereq-ready in Y1-Y2 (genuinely prereq-blocked)
                # may be placed; a GE that was ready but didn't fit (capacity) is
                # held back so pure-elective GEs cannot spill into Y3+.
                if _norm(c) in ge_norms:
                    if q_idx <= GE_LAST_QUARTER_IDX:
                        ge_prereq_ready.add(_norm(c))
                    elif _norm(c) in ge_prereq_ready:
                        continue  # capacity-blocked GE — never spill into Y3+
                    else:
                        late_ge_blocked.add(_norm(c))  # prereq-blocked — allow late

                if not _offered_in_quarter(c, quarter):
                    continue

                # Verify each unresolved coreq can actually be placed here.
                candidate_coreqs: list[str] = []
                ok = True
                extra_units = 0
                for n in unresolved:
                    raw_coreq = remaining_norm_to_raw.get(n)
                    if raw_coreq is None or raw_coreq == c:
                        ok = False
                        break  # coreq not in plan — can't satisfy
                    coreq_tree = prereq_trees.get(n)
                    if coreq_tree and not _eval_tree(coreq_tree, available, hypothetical_q):
                        ok = False
                        break
                    if not _offered_in_quarter(raw_coreq, quarter):
                        ok = False
                        break
                    candidate_coreqs.append(raw_coreq)
                    extra_units += _course_units(raw_coreq)

                if not ok:
                    continue

                # Unit budget: main course + all unresolved coreqs must fit together.
                if q_units + _course_units(c) + extra_units > units_per_quarter:
                    continue

                eligible_candidates.append((c, candidate_coreqs))

            if not eligible_candidates:
                break  # nothing eligible this quarter; advance to next

            # Sort: lower-div first, GEs ahead of non-GE upper-div, then alpha.
            eligible_candidates.sort(key=lambda t: _placement_key(t[0]))
            main_course, coreqs_to_place = eligible_candidates[0]

            # Place coreqs first so they're in the quarter when validation runs.
            for coreq_raw in coreqs_to_place:
                if coreq_raw in remaining:
                    remaining.remove(coreq_raw)
                    plan[quarter].append(coreq_raw)

            remaining.remove(main_course)
            plan[quarter].append(main_course)

        # Minimum-units pass: if the quarter is under 12 units but still has
        # room, do one more sweep for any eligible course that fits.  This
        # catches edge cases where the primary while-loop exited early (e.g.
        # the first eligible candidate was too large to fit but a smaller one
        # exists later in the sorted order).  Prereqs are never violated here.
        MIN_UNITS_PER_QUARTER = 12
        q_units_now = sum(_course_units(c) for c in plan[quarter])
        if q_units_now < MIN_UNITS_PER_QUARTER and remaining:
            same_q_norms_fill: frozenset[str] = frozenset(_norm(x) for x in plan[quarter])
            remaining_norm_fill: dict[str, str] = {}
            for raw in remaining:
                n = _norm(raw)
                if n not in remaining_norm_fill:
                    remaining_norm_fill[n] = raw
            coreq_target_fill: set[str] = set()
            for other in remaining:
                coreq_target_fill |= _collect_and_coreq_norms(prereq_trees.get(_norm(other)))
            fill_candidates: list[tuple[str, list[str]]] = []
            for c in remaining:
                if _norm(c) in coreq_target_fill:
                    continue  # coreq leaf — only placeable alongside its partner
                tree = prereq_trees.get(_norm(c))
                required_coreqs = _collect_and_coreq_norms(tree)
                unresolved = [
                    n for n in required_coreqs
                    if n not in available and n not in same_q_norms_fill
                ]
                hypothetical_q = same_q_norms_fill | set(unresolved)
                if tree and not _eval_tree(tree, available, hypothetical_q):
                    continue
                # Hard GE deadline (CHANGE 1) — mirror the main pass so the
                # min-units sweep can't backfill a prereq-ready GE past Year 2.
                if _norm(c) in ge_norms:
                    if q_idx <= GE_LAST_QUARTER_IDX:
                        ge_prereq_ready.add(_norm(c))
                    elif _norm(c) in ge_prereq_ready:
                        continue
                    else:
                        late_ge_blocked.add(_norm(c))
                if not _offered_in_quarter(c, quarter):
                    continue
                candidate_coreqs: list[str] = []
                ok = True
                extra_units = 0
                for n in unresolved:
                    raw_coreq = remaining_norm_fill.get(n)
                    if raw_coreq is None or raw_coreq == c:
                        ok = False
                        break
                    coreq_tree = prereq_trees.get(n)
                    if coreq_tree and not _eval_tree(coreq_tree, available, hypothetical_q):
                        ok = False
                        break
                    if not _offered_in_quarter(raw_coreq, quarter):
                        ok = False
                        break
                    candidate_coreqs.append(raw_coreq)
                    extra_units += _course_units(raw_coreq)
                if not ok:
                    continue
                if q_units_now + _course_units(c) + extra_units > units_per_quarter:
                    continue
                fill_candidates.append((c, candidate_coreqs))
            fill_candidates.sort(key=lambda t: _placement_key(t[0]))
            for fill_course, fill_coreqs in fill_candidates:
                if fill_course not in remaining:
                    continue
                q_units_now = sum(_course_units(c) for c in plan[quarter])
                if q_units_now >= units_per_quarter:
                    break
                extra = sum(_course_units(r) for r in fill_coreqs if r in remaining)
                if q_units_now + _course_units(fill_course) + extra > units_per_quarter:
                    continue
                for coreq_raw in fill_coreqs:
                    if coreq_raw in remaining:
                        remaining.remove(coreq_raw)
                        plan[quarter].append(coreq_raw)
                remaining.remove(fill_course)
                plan[quarter].append(fill_course)

        # Make this quarter's courses available to all subsequent quarters.
        available.update(_norm(c) for c in plan[quarter])

    if late_ge_blocked:
        print(
            f"  [GE deadline] {len(late_ge_blocked)} prereq-blocked GE(s) allowed "
            f"past Year 2 (prereqs not schedulable in Y1-Y2): {sorted(late_ge_blocked)}"
        )

    return plan, remaining  # remaining = overflow


# ── Perturbation ──────────────────────────────────────────────────────────────

def _perturb(
    plan: CoursePlan,
    rng: random.Random,
    n_swaps: int,
    trees: dict[str, dict] | None = None,
) -> CoursePlan:
    """Swap n_swaps random course pairs between quarters.

    When `trees` is provided, each swap is validated against the prereq trees and
    immediately undone if it would introduce a prereq violation.  This prevents
    the optimizer from receiving a pre-broken starting plan that it would
    immediately return unchanged (the early-exit path in optimize()).
    """
    p = deepcopy(plan)
    quarters = list(p.planned_courses.keys())
    for _ in range(n_swaps):
        non_empty = [q for q in quarters if p.planned_courses.get(q)]
        if len(non_empty) < 2:
            break
        q1, q2 = rng.sample(non_empty, 2)
        c1 = rng.choice(p.planned_courses[q1])
        c2 = rng.choice(p.planned_courses[q2])
        before_split = coreq_split_pairs(p, trees) if trees else set()
        p.planned_courses[q1].remove(c1)
        p.planned_courses[q2].remove(c2)
        p.planned_courses[q1].append(c2)
        p.planned_courses[q2].append(c1)
        # Reject swap if it introduces a prereq violation or splits a coreq pair
        if trees and (
            _check_prereqs(p, trees)
            or (coreq_split_pairs(p, trees) - before_split)
        ):
            p.planned_courses[q1].remove(c2)
            p.planned_courses[q2].remove(c1)
            p.planned_courses[q1].append(c1)
            p.planned_courses[q2].append(c2)
    return p


# ── Elective overflow swap (Step 3) ─────────────────────────────────────────

def _try_swap_elective_overflow(
    client,
    major_id: str,
    overflow: list[str],
    courses_to_plan: list[str],
    trees: dict[str, dict],
    quarters: list[str],
    units_per_quarter: int,
    completed_norm: set[str],
    course_terms: dict[str, list[str]],
    diff_scores: dict[str, float],
    units_by_course: dict[str, int] | None = None,
    ge_norms: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """For overflow courses that come from elective rows, try swapping them with
    simpler (lower difficulty + prereq depth) alternatives from the same pool.

    Returns (updated_courses_to_plan, remaining_overflow).
    Any required overflow courses are left untouched.
    """
    # Re-query major rows (including parent merge for specialisations)
    major_rows = (
        client.table("major_requirements")
        .select("requirement_group,group_name,requirement_type,courses,courses_needed")
        .eq("major_id", major_id)
        .execute()
        .data
    )
    if re.search(r"[A-Z]$", major_id):
        parent_id = major_id[:-1]
        parent_rows = (
            client.table("major_requirements")
            .select("requirement_group,group_name,requirement_type,courses,courses_needed")
            .eq("major_id", parent_id)
            .execute()
            .data
        )
        existing_keys = {
            (r.get("requirement_group") or "") + "|" + (r.get("group_name") or "")
            for r in major_rows
        }
        for row in parent_rows:
            key = (row.get("requirement_group") or "") + "|" + (row.get("group_name") or "")
            if key not in existing_keys:
                major_rows.append(row)

    # Build norm_id → (requirement_type, full_pool) for fast lookup
    req_info: dict[str, tuple[str, list[str]]] = {}
    for row in major_rows:
        rtype = (row.get("requirement_type") or "required").lower()
        for c in (row.get("courses") or []):
            req_info.setdefault(_norm(c), (rtype, row.get("courses") or []))

    current_plan_norms = {_norm(c) for c in courses_to_plan}
    updated = list(courses_to_plan)
    remaining = list(overflow)

    for oc in list(overflow):
        nc = _norm(oc)
        info = req_info.get(nc)
        if not info:
            continue
        rtype, pool = info
        if "elective" not in rtype:
            continue  # required overflow — can't swap

        # Find alternatives not yet in the plan
        alternatives = [
            c for c in pool
            if _norm(c) not in current_plan_norms
            and _norm(c) not in completed_norm
            and _norm(c) != nc
        ]
        if not alternatives:
            continue

        # Sort: lowest difficulty, fewest prereq edges, alpha tiebreak
        alt_norms = [_norm(a) for a in alternatives]
        alt_depths = _get_prereq_depths(client, alt_norms)
        alternatives.sort(key=lambda c: (
            diff_scores.get(_norm(c), 5.0),
            alt_depths.get(_norm(c), 0),
            c,
        ))

        # Try up to 3 candidates; accept the first that removes the overflow
        for alt in alternatives[:3]:
            trial = [c for c in updated if _norm(c) != nc]
            trial.append(alt)
            if alt not in course_terms:
                course_terms.update(_fetch_course_terms(client, [alt]))
            _, trial_overflow = _asap_schedule(
                trial, trees, quarters, units_per_quarter,
                completed_norm, terms_by_course=course_terms,
                units_by_course=units_by_course, ge_norms=ge_norms,
            )
            if oc not in trial_overflow and alt not in trial_overflow:
                print(f"  [Step 3] Swapped elective overflow {oc!r} → {alt!r}")
                updated = trial
                current_plan_norms.discard(nc)
                current_plan_norms.add(_norm(alt))
                remaining.remove(oc)
                break

    return updated, remaining


# ── Public API ────────────────────────────────────────────────────────────────

def generate(
    major_id: str,
    completed_courses: list[str],
    graduation_quarter: str,
    units_per_quarter: int = 16,
    waived_ges: list[str] | None = None,
    specialization_id: str | None = None,
    ap_scores: dict[str, int] | None = None,
    start_quarter: str | None = None,
    seed_courses: list[str] | None = None,
    seed_only: bool = False,
) -> GenerationResult:
    """Generate up to 3 optimized plan variants for a major.

    Parameters
    ----------
    waived_ges : list of requirement_group codes (e.g. ["GE_VI"]) to skip.
                 Only groups marked waivable=True in the DB are skippable.
    seed_courses : extra course IDs that MUST appear in the plan in addition to
                 the major's required courses — e.g. the user's current schedule
                 plus any GE/Minor picks chosen on the frontend.  They are merged
                 into the course set (deduped, minus completed/AP); the scheduler
                 is free to reorder them for prereqs / difficulty balance, but
                 never drops them.  This is what makes autofill additive instead
                 of wiping the existing schedule.
    seed_only :  when True, the major's required courses are NOT auto-added — the
                 plan is built from seed_courses (+ their implicit prereqs) only.
                 Used by GE/Minor autofill so clicking "Auto-fill GE" doesn't also
                 pull in every remaining major requirement.

    Returns
    -------
    GenerationResult with variants sorted by soft_score ascending (best first)
    and feasibility metadata.  Raises FeasibilityError before returning if the
    graduation window is too short.
    """
    if waived_ges is None:
        waived_ges = []

    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    _load_aliases(client)  # FIX 6: build alias map before any _norm() calls
    completed_norm = {_norm(c) for c in completed_courses}

    # 1. Editor model: seed ONLY required (take-all) courses. "Choose N of M"
    # pools (electives, GE categories, "X or Y") are surfaced as choice_groups for
    # the user to resolve — they are NOT auto-picked or placed here.
    # AP scores are resolved inside collect_requirements; their equivalencies are
    # added to completed_norm so they are excluded from the plan automatically.
    courses_to_plan, choice_groups, ap_credited, ap_units = collect_requirements(
        client, major_id, completed_norm, waived_ges, specialization_id, ap_scores
    )
    group_map: dict[str, list[str]] = {}  # no auto-picked choices in the seed

    # seed_only (GE/Minor autofill): drop the required-course backbone so the plan
    # is built from the caller's seed (+ implicit prereqs) alone.
    if seed_only:
        courses_to_plan = []

    # Merge caller-supplied seed courses (current schedule + GE/Minor picks).
    # courses_to_plan holds RAW ids deduped via _norm — match that here so a
    # seeded course already present as a required course isn't duplicated.
    if seed_courses:
        seen = {_norm(c) for c in courses_to_plan}
        for c in seed_courses:
            nc = _norm(c)
            if nc and nc not in seen and nc not in completed_norm:
                courses_to_plan.append(c)
                seen.add(nc)

    if not courses_to_plan:
        return GenerationResult(variants=[], choice_groups=choice_groups)

    # 2. Quarters available before graduation
    quarters = _generate_quarters(graduation_quarter, start_quarter)
    if not quarters:
        return GenerationResult(variants=[])

    # 3. Feasibility check — raises FeasibilityError if impossible
    tight, q_available, q_needed = _check_feasibility(
        len(courses_to_plan), quarters, units_per_quarter, graduation_quarter
    )

    # 4. Load prereq trees, inject implicit prereqs, re-check feasibility, then schedule
    trees = _fetch_prereq_trees(client, courses_to_plan)
    courses_to_plan, trees = _resolve_implicit_prereqs(
        courses_to_plan, trees, completed_norm, client
    )
    # Re-check feasibility after injection — may have added required prereq courses
    tight, q_available, q_needed = _check_feasibility(
        len(courses_to_plan), quarters, units_per_quarter, graduation_quarter
    )
    # Fetch term availability and actual unit counts before scheduling
    course_terms = _fetch_course_terms(client, courses_to_plan)
    course_units = _fetch_course_units(client, courses_to_plan)
    # GE-satisfying course norms drive the hard GE-earliness deadline (CHANGE 1).
    ge_norms = _fetch_ge_course_norms(client, courses_to_plan)

    # Dynamic window extension — retry ASAP adding 1 quarter at a time if
    # overflow exists, up to MAX_QUARTERS total.  The stricter per-quarter prereq
    # enforcement means chains like ICS31→32→33 must span sequential quarters.
    working_quarters = list(quarters)
    # Unit-cap escalation: try the requested cap, then 20, then 24 — all within
    # the fixed grid window — before spilling into extra (off-grid) quarters.
    # A looser cap is adopted only when it actually reduces overflow; overflow
    # that is bound by prereq-chain depth (not unit capacity) is left for the
    # quarter-extension step rather than needlessly packing quarters denser.
    effective_units = units_per_quarter
    plan_dict, overflow = _asap_schedule(
        courses_to_plan, trees, working_quarters, effective_units,
        completed_norm, terms_by_course=course_terms, units_by_course=course_units,
        ge_norms=ge_norms,
    )
    for cap in unit_cap_tiers(units_per_quarter)[1:]:
        if not overflow:
            break
        trial_plan, trial_overflow = _asap_schedule(
            courses_to_plan, trees, working_quarters, cap,
            completed_norm, terms_by_course=course_terms, units_by_course=course_units,
            ge_norms=ge_norms,
        )
        if len(trial_overflow) < len(overflow):
            effective_units, plan_dict, overflow = cap, trial_plan, trial_overflow
    if effective_units != units_per_quarter:
        print(f"  Raised per-quarter cap to {effective_units} units to fit all courses.")

    extended_by = 0

    while overflow and len(working_quarters) < MAX_QUARTERS:
        working_quarters.append(_next_quarter(working_quarters[-1]))
        extended_by += 1
        plan_dict, overflow = _asap_schedule(
            courses_to_plan, trees, working_quarters, effective_units,
            completed_norm, terms_by_course=course_terms, units_by_course=course_units,
            ge_norms=ge_norms,
        )

    if extended_by:
        print(
            f"  Extended window by {extended_by} quarter(s) beyond "
            f"{graduation_quarter} to fit all courses."
        )

    # For any remaining overflow, try swapping elective courses with simpler
    # alternatives from the same pool.  Editor model: the seed is required-only
    # (group_map is empty — no auto-picked electives), so this swap is skipped —
    # swapping a *required* course for an elective alternative would both drop a
    # mandatory course and risk introducing an unmet-prereq course into the seed.
    if overflow and group_map:
        diff_scores = _load_difficulty_scores()
        courses_to_plan, overflow = _try_swap_elective_overflow(
            client, major_id, overflow, courses_to_plan, trees,
            working_quarters, effective_units, completed_norm,
            course_terms, diff_scores, units_by_course=course_units,
            ge_norms=ge_norms,
        )
        if overflow:
            print(
                f"  Warning: {len(overflow)} course(s) could not be scheduled "
                f"within {MAX_QUARTERS} quarters — flagged as unschedulable: {overflow}"
            )

    # Final ASAP run with the (possibly swap-updated) courses_to_plan
    if overflow:
        plan_dict, overflow = _asap_schedule(
            courses_to_plan, trees, working_quarters, effective_units,
            completed_norm, terms_by_course=course_terms, units_by_course=course_units,
            ge_norms=ge_norms,
        )

    # graduation_year: use the last quarter in the working window
    grad_year = int(working_quarters[-1].split("_")[0])
    # AP-credited courses were added to completed_norm (used by the scheduler) but
    # CoursePlan.completed_courses is what prereqs_satisfied checks.  Include them
    # so the optimizer and prereq screener see AP courses as already completed.
    effective_completed = list(completed_courses) + ap_credited
    base_plan = CoursePlan(
        major_id=major_id,
        completed_courses=effective_completed,
        planned_courses={q: cs for q, cs in plan_dict.items() if cs},
        graduation_year=grad_year,
        units_per_quarter=effective_units,
    )

    # 5. Generate variants: perturb then optimize with different seeds
    configs = [
        (42,  0),   # (seed, n_swaps)
        (13,  4),
        (7,   8),
        (99, 12),
        (17, 16),
    ]

    variants: list[OptimizationResult] = []
    for seed, n_swaps in configs:
        rng = random.Random(seed)
        starting = _perturb(base_plan, rng, n_swaps, trees=trees) if n_swaps else base_plan
        result = optimize(starting, max_iter=150, seed=seed)
        variants.append(result)

    variants.sort(key=lambda r: r.soft_score)
    best_variants = variants[:3]

    # Annotate each variant with offering-pattern warnings (soft, not hard).
    # A course scheduled in a quarter it historically isn't offered gets a
    # warning appended to violations — plan is not invalidated.
    for variant in best_variants:
        for quarter, courses in variant.plan.planned_courses.items():
            q_name = quarter.split("_", 1)[1]  # "2026_fall" → "fall"
            for course in courses:
                likely, reason = is_likely_offered(course, q_name)
                if not likely:
                    variant.violations.append(
                        f"[offering] {course} in {quarter}: {reason}"
                    )

    return GenerationResult(
        variants=best_variants,
        tight_timeline=tight,
        overflow_count=len(overflow),
        quarters_available=len(working_quarters),
        quarters_needed=q_needed,
        group_map=group_map,
        overflow_courses=list(overflow),
        extended_by=extended_by,
        ap_credited_courses=ap_credited,
        ap_units_awarded=ap_units,
        choice_groups=choice_groups,
    )

