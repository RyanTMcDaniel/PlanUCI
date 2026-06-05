"""
Soft constraint scorers for CoursePlan objects.
Each scorer returns a penalty float in [0.0, 1.0] — lower is better.

Usage:
    plan = CoursePlan(major_id="BS-201A", ...)
    total_penalty, breakdown = score(plan)
"""

import csv
import os
import re
import statistics

from dotenv import load_dotenv
from supabase import create_client

from .hard_constraints import CoursePlan, UNITS_PER_COURSE, _norm, _qkey

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIFFICULTY_CSV = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "ml", "data", "course_features.csv")
)

WEIGHTS = {
    "difficulty_balance":   0.40,
    "ge_distribution":      0.20,
    "workload_progression": 0.20,
    "major_clustering":     0.20,
    # Adjacent-quarter smoothing (2× the implicit progression weight).
    # Penalises sharp swings between consecutive quarters, including cliff jumps.
    "adjacent_smoothing":   0.40,
    # Discourage under-loaded quarters: non-empty quarters should carry >=12 units.
    "min_units_load":       0.30,
    # Penalise quarters that exceed the unit cap. Weighted above difficulty_balance
    # and adjacent_smoothing so the hill-climber actively moves courses OUT of
    # over-cap quarters (e.g. into otherwise-empty later quarters) rather than
    # leaving them packed.
    "over_cap_penalty":     0.50,
    # Earliness preferences: nudge lower-division courses and GE-satisfying
    # courses toward earlier quarters. Kept below difficulty_balance (0.40) and
    # adjacent_smoothing (0.40) so they never override difficulty/feasibility,
    # but raised enough that their per-move deltas (≈ weight × 1/n) actually
    # register against difficulty_balance moves instead of being drowned out.
    "lower_div_earliness":  0.60,
    "ge_earliness":         0.50,
}

_DEFAULT_DIFFICULTY = 5.0   # used when a course has no entry in the CSV
_MAX_VARIANCE       = 16.0  # variance of [1.0, 9.0] with n=2, used for normalization


# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_difficulty_scores() -> dict[str, float]:
    """Load normalized course_id → difficulty_score from course_features.csv."""
    out: dict[str, float] = {}
    try:
        with open(_DIFFICULTY_CSV, newline="") as fh:
            for row in csv.DictReader(fh):
                out[_norm(row["course_id"])] = float(row["difficulty_score"])
    except FileNotFoundError:
        pass
    return out


def _load_course_meta(client, course_ids: list[str]) -> dict[str, dict]:
    """Fetch ge_list, department and min_units for each course id from Supabase.

    The `courses.id` column stores ids with spaces stripped (catalog text like
    "BIO SCI 93" / "I&C SCI 46" is stored as "BIOSCI93" / "I&CSCI46"), but plan
    ids arrive in either form — injected prereqs keep the spaced catalog text.
    A raw `.in_("id", course_ids)` therefore silently misses every spaced id, so
    those courses' ge_list / min_units drop out of all meta-based soft
    constraints (ge_earliness, ge_distribution, major_clustering, …) — making the
    earliness weights effectively 0 for GE courses seeded with a spaced id. Query
    both the raw id and a space-stripped, uppercased variant so both forms match;
    the result stays keyed by _norm(id) so lookups via _norm(course) still hit.
    """
    if not course_ids:
        return {}
    query_ids = set()
    for cid in course_ids:
        query_ids.add(cid)
        query_ids.add(cid.replace(" ", "").upper())
    rows = (
        client.table("courses")
        .select("id,department,ge_list,min_units")
        .in_("id", list(query_ids))
        .execute()
        .data
    )
    return {_norm(r["id"]): r for r in rows}


def _infer_dept(normalized_id: str) -> str:
    """Rough department extraction when courses-table meta is unavailable.

    "COMPSCI161" → "COMPSCI", "IN4MATX43" → "IN4MATX", "WRITING39A" → "WRITING"
    """
    return re.sub(r"\d+[A-Za-z]*$", "", normalized_id)


# Canonical per-quarter difficulty — MUST match the frontend's
# calculateQuarterDifficulty (PlannerClient.tsx).  Decaying weights make the
# hardest course in a quarter dominate; a count penalty discourages overloading.
_DECAY_WEIGHTS = [1.0, 0.85, 0.72, 0.61, 0.52, 0.44]
_DECAY_TAIL    = 0.4   # weight for the 7th+ hardest course


def quarter_difficulty(course_ids: list[str], diff_scores: dict[str, float]) -> float:
    """Single canonical per-quarter difficulty score, matching the frontend.

    Mirrors `calculateQuarterDifficulty`'s `adjustedDifficulty`:
      1. Sort course difficulties descending.
      2. Weighted mean with decaying weights [1.0, 0.85, 0.72, 0.61, 0.52, 0.44]
         (0.4 for any 7th+ course) so the hardest course dominates.
      3. Add a count penalty of max(0, (n - 3) * 0.3).
      4. Clamp to 10.0.

    Courses missing from diff_scores fall back to _DEFAULT_DIFFICULTY (the
    backend's existing convention); they still count toward the penalty.
    """
    if not course_ids:
        return 0.0
    scores = sorted(
        (diff_scores.get(_norm(c), _DEFAULT_DIFFICULTY) for c in course_ids),
        reverse=True,
    )
    weighted_sum = 0.0
    total_weight = 0.0
    for i, s in enumerate(scores):
        w = _DECAY_WEIGHTS[i] if i < len(_DECAY_WEIGHTS) else _DECAY_TAIL
        weighted_sum += s * w
        total_weight += w
    difficulty_component = (
        weighted_sum / total_weight if total_weight > 0 else _DEFAULT_DIFFICULTY
    )
    count_penalty = max(0.0, (len(scores) - 3) * 0.3)
    return min(10.0, difficulty_component + count_penalty)


def _quarter_difficulties(plan: CoursePlan, diff_scores: dict[str, float]) -> list[float]:
    """Per-quarter canonical difficulty in chronological order (non-empty quarters only)."""
    out = []
    for q in sorted(plan.planned_courses.keys(), key=_qkey):
        courses = plan.planned_courses[q]
        if courses:
            out.append(quarter_difficulty(courses, diff_scores))
    return out


# ── Scorers ───────────────────────────────────────────────────────────────────

def difficulty_balance(plan: CoursePlan, diff_scores: dict[str, float]) -> float:
    """Variance of per-quarter average difficulty, normalized to [0, 1].

    A plan where one quarter averages 9.0 and another 2.0 scores near 1.0;
    every quarter at 5.5 scores 0.0.
    """
    avgs = _quarter_difficulties(plan, diff_scores)
    if len(avgs) < 2:
        return 0.0
    return min(statistics.variance(avgs) / _MAX_VARIANCE, 1.0)


def ge_distribution(plan: CoursePlan, course_meta: dict[str, dict]) -> float:
    """Penalty for GE courses clustered in the same quarter.

    Each quarter with more than 2 GEs contributes excess to the score.
    Normalized by the worst-case excess (all GEs piled in one quarter).
    """
    sorted_quarters = sorted(plan.planned_courses.keys(), key=_qkey)
    ge_counts: list[int] = []
    total_ges = 0

    for q in sorted_quarters:
        n = sum(
            1 for c in plan.planned_courses[q]
            if course_meta.get(_norm(c), {}).get("ge_list")
        )
        ge_counts.append(n)
        total_ges += n

    if total_ges == 0:
        return 0.0

    excess = sum(max(0, n - 2) for n in ge_counts)
    max_excess = max(0, total_ges - 2)  # worst case: every GE in one quarter
    if max_excess == 0:
        return 0.0
    return min(excess / max_excess, 1.0)


def workload_progression(plan: CoursePlan, diff_scores: dict[str, float]) -> float:
    """Penalty when difficulty trend across quarters is flat or decreasing.

    Fits an OLS slope to chronological average-difficulty values.
    A strongly increasing plan scores 0.0; a strongly decreasing one scores 1.0.
    Max penalized slope is −2.0 points per quarter.
    """
    avgs = _quarter_difficulties(plan, diff_scores)
    if len(avgs) < 2:
        return 0.0

    n = len(avgs)
    xs = list(range(n))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(avgs)

    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, avgs))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return 0.0

    slope = num / den
    if slope >= 0:
        return 0.0
    _MAX_SLOPE = 2.0
    return min(-slope / _MAX_SLOPE, 1.0)


def major_clustering(plan: CoursePlan, course_meta: dict[str, dict]) -> float:
    """Penalty for same-department courses scattered across distant quarters.

    For each department with ≥2 planned courses, the quarter-index span is
    computed (0 = all same quarter, n−1 = maximally spread).  Spans of 0–2
    quarters incur no penalty; anything wider is penalized linearly.
    """
    sorted_quarters = sorted(plan.planned_courses.keys(), key=_qkey)
    n_quarters = len(sorted_quarters)
    if n_quarters <= 1:
        return 0.0

    q_idx = {q: i for i, q in enumerate(sorted_quarters)}
    dept_indices: dict[str, list[int]] = {}

    for q in sorted_quarters:
        for course in plan.planned_courses[q]:
            meta = course_meta.get(_norm(course), {})
            dept = meta.get("department") or _infer_dept(_norm(course))
            dept_indices.setdefault(dept, []).append(q_idx[q])

    penalties: list[float] = []
    max_span = n_quarters - 1
    for dept, idxs in dept_indices.items():
        if len(idxs) < 2:
            continue
        span = max(idxs) - min(idxs)
        # Spans ≤ 2 are acceptable; penalize proportionally beyond that
        penalty = max(0.0, span - 2) / max_span if max_span > 2 else 0.0
        penalties.append(penalty)

    return statistics.mean(penalties) if penalties else 0.0


def adjacent_smoothing(plan: CoursePlan, diff_scores: dict[str, float]) -> float:
    """Penalty for sharp difficulty swings between consecutive quarters.

    Two-component penalty:
    1. Proportional: each adjacent pair with |Δ| > 1.5 contributes linearly.
       A swing of 1.5 → 0 penalty; a swing of 3.5 → 1.0 penalty per pair.
    2. Cliff-jump: any adjacent pair with |Δ| > 2.0 adds a flat +0.5 penalty.
       This is the 'flat -50' in the design brief scaled to the [0, 1] scorer
       range (0.5 × weight 0.40 dominates other scorers when triggered).

    Result is averaged over pairs and clamped to [0, 1].
    """
    avgs = _quarter_difficulties(plan, diff_scores)
    if len(avgs) < 2:
        return 0.0

    total = 0.0
    n_pairs = len(avgs) - 1

    for a, b in zip(avgs, avgs[1:]):
        diff = abs(b - a)
        if diff > 1.5:
            # Proportional component: ramps from 0 at diff=1.5 to 1.0 at diff=3.5
            total += min(1.0, (diff - 1.5) / 2.0)
        if diff > 2.0:
            # Cliff-jump flat penalty — designed to dominate and force the
            # optimizer to reject moves that create jumps > 2 difficulty points.
            total += 0.5

    return min(1.0, total / n_pairs)


_MIN_UNITS_TARGET = 12  # quarters below this many units are under-loaded


def min_units_load(plan: CoursePlan, course_meta: dict[str, dict]) -> float:
    """Penalty for non-empty quarters carrying fewer than 12 units.

    Empty quarters are ignored — a gap quarter is fine.  For each non-empty
    quarter, the shortfall below 12 units is normalized to [0, 1] (>=12 units → 0,
    0 units → 1) and the penalties are averaged.  Real per-course units come from
    course_meta; missing entries fall back to UNITS_PER_COURSE.
    """
    shortfalls: list[float] = []
    for courses in plan.planned_courses.values():
        if not courses:
            continue  # empty quarter — not penalized
        units = sum(
            (course_meta.get(_norm(c), {}).get("min_units") or UNITS_PER_COURSE)
            for c in courses
        )
        shortfalls.append(max(0.0, _MIN_UNITS_TARGET - units) / _MIN_UNITS_TARGET)
    if not shortfalls:
        return 0.0
    return sum(shortfalls) / len(shortfalls)


def over_cap_penalty(
    plan: CoursePlan, units_per_quarter: int, course_meta: dict[str, dict]
) -> float:
    """Penalty for quarters whose unit total exceeds the cap.

    For every quarter the normalised excess ``max(0, units - cap) / cap`` is
    computed (quarters at or under the cap contribute 0) and SUMMED across
    quarters.  (Summing rather than averaging over all quarters is deliberate:
    dividing by the full quarter count diluted the term ~Nx below min_units_load,
    so the hill-climber wouldn't move courses out of over-cap quarters — the whole
    point of this term.)  Real per-course units come from course_meta; missing
    entries fall back to UNITS_PER_COURSE.  Lower is better, like every soft term.
    """
    if units_per_quarter <= 0:
        return 0.0
    total = 0.0
    for courses in plan.planned_courses.values():
        units = sum(
            (course_meta.get(_norm(c), {}).get("min_units") or UNITS_PER_COURSE)
            for c in courses
        )
        total += max(0.0, units - units_per_quarter) / units_per_quarter
    return total


def _course_number(course_id: str) -> int | None:
    """Leading course number from a (normalized) id: 'MATH2D' → 2, 'ICS161' → 161."""
    m = re.search(r"(\d+)", _norm(course_id))
    return int(m.group(1)) if m else None


def lower_div_earliness(plan: CoursePlan, locked_norm: frozenset[str] = frozenset()) -> float:
    """Earliness preference for lower-division (course number < 100) courses.

    Mean of (quarter_index / total_quarters) over UNLOCKED lower-div courses, so
    a lower-div course placed late incurs a higher penalty. Soft only — never
    blocks a late placement that prereqs/availability require. Locked courses are
    excluded (they can't move, so they'd only add a constant offset). Returns
    0.0 when there are no unlocked lower-div courses.
    """
    sorted_q = sorted(plan.planned_courses.keys(), key=_qkey)
    n = len(sorted_q)
    if n == 0:
        return 0.0
    penalties: list[float] = []
    for i, q in enumerate(sorted_q):
        for c in plan.planned_courses[q]:
            if _norm(c) in locked_norm:
                continue
            num = _course_number(c)
            if num is not None and num < 100:
                penalties.append(i / n)
    return sum(penalties) / len(penalties) if penalties else 0.0


def ge_earliness(
    plan: CoursePlan, course_meta: dict[str, dict], locked_norm: frozenset[str] = frozenset()
) -> float:
    """Earliness preference for GE-satisfying courses (non-empty ge_list).

    Same shape as lower_div_earliness: mean of (quarter_index / total_quarters)
    over UNLOCKED GE courses. Front-loads GE completion. Soft only.
    """
    sorted_q = sorted(plan.planned_courses.keys(), key=_qkey)
    n = len(sorted_q)
    if n == 0:
        return 0.0
    penalties: list[float] = []
    for i, q in enumerate(sorted_q):
        for c in plan.planned_courses[q]:
            if _norm(c) in locked_norm:
                continue
            if course_meta.get(_norm(c), {}).get("ge_list"):
                penalties.append(i / n)
    return sum(penalties) / len(penalties) if penalties else 0.0


# ── Combined scorer ───────────────────────────────────────────────────────────

def score(plan: CoursePlan) -> tuple[float, dict[str, float]]:
    """Run all soft constraints and return (combined_penalty, breakdown).

    combined_penalty is in [0.0, 1.0] — lower is better.
    breakdown maps each scorer name to its individual penalty.
    """
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    diff_scores = _load_difficulty_scores()

    all_ids = [c for courses in plan.planned_courses.values() for c in courses]
    meta = _load_course_meta(client, all_ids)

    breakdown = {
        "difficulty_balance":   difficulty_balance(plan, diff_scores),
        "ge_distribution":      ge_distribution(plan, meta),
        "workload_progression": workload_progression(plan, diff_scores),
        "major_clustering":     major_clustering(plan, meta),
        "min_units_load":       min_units_load(plan, meta),
    }
    combined = sum(WEIGHTS[k] * v for k, v in breakdown.items())
    return combined, breakdown

