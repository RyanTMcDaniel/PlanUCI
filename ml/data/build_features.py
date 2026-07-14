import json
import os
import re

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from dotenv import load_dotenv
from rapidfuzz import process as fuzz_process
from sentence_transformers import SentenceTransformer
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend", ".env")
load_dotenv(_ENV)

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(DATA_DIR, "..", "models", "difficulty_nlp_v2")
SIGNAL_WEIGHTS_PATH = os.path.join(DATA_DIR, "..", "models", "signal_weights.json")
EMBEDDING_DIM = 384
BATCH_SIZE = 32
MIN_GRADED = 15  # minimum graded students per section

# Blend weights for the difficulty scoring heuristic — a documented PRIOR over the
# three signals, not a validated model.  Note the circularity: difficulty_score below
# is built FROM these weights, and ml/models/fit_signal_weights.py then re-derives the
# weights from difficulty_score.  That loop is why no accuracy metric is claimed for
# the blend anywhere.  See the docstring in fit_signal_weights.py and DECISIONS.md.
SIGNAL_WEIGHTS = json.load(open(SIGNAL_WEIGHTS_PATH))["coefficients"]


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_all(client, table: str, columns: str) -> list[dict]:
    rows, page_size, offset = [], 1000, 0
    while True:
        batch = (
            client.table(table).select(columns)
            .range(offset, offset + page_size - 1).execute().data
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def normalize_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[,.\-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def build_name_to_ucinetid(instructors_df: pd.DataFrame) -> dict[str, str]:
    """Map each shortened_name variant → ucinetid."""
    lookup = {}
    for _, row in instructors_df.iterrows():
        for name in (row["shortened_names"] or []):
            lookup[normalize_name(name)] = row["ucinetid"]
    return lookup


def build_fuzzy_corpus(instructors_df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Build a flat list of (candidate_string, ucinetid) for fuzzy matching."""
    candidates, ucinetids = [], []
    for _, row in instructors_df.iterrows():
        uid = row["ucinetid"]
        # Include full name
        if row.get("name"):
            candidates.append(normalize_name(str(row["name"])))
            ucinetids.append(uid)
        # Include all shortened_name variants
        for name in (row["shortened_names"] or []):
            candidates.append(normalize_name(name))
            ucinetids.append(uid)
    return candidates, ucinetids


def fuzzy_match_names(
    unmatched_names: pd.Series,
    candidates: list[str],
    ucinetids: list[str],
    threshold: int = 95,
) -> tuple[dict[str, str], list[dict]]:
    """Return ({instructor_raw_clean → ucinetid}, match_details) for names above threshold."""
    unique_names = unmatched_names.dropna().unique()
    result = {}
    details = []
    for name in unique_names:
        norm = normalize_name(name)
        hit = fuzz_process.extractOne(norm, candidates, score_cutoff=threshold)
        if hit:
            best_candidate, score, idx = hit
            result[name] = ucinetids[idx]
            details.append({
                "original": name,
                "matched": best_candidate,
                "score": score,
                "ucinetid": ucinetids[idx],
            })
    return result, details


def nlp_scores(encoder, classifier, courses_df: pd.DataFrame, device) -> pd.Series:
    """Run v2 classifier; convert softmax to 1–10 score: easy→2, medium→5.5, hard→9."""
    title = courses_df["title"].fillna(courses_df["id"])
    texts = (title + ": " + courses_df["description"].fillna("")).tolist()

    encoder.eval()
    classifier.eval()
    all_probs = []
    with torch.no_grad():
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            features = encoder.preprocess(batch)
            features = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                        for k, v in features.items()}
            logits = classifier(encoder(features)["sentence_embedding"])
            all_probs.append(torch.softmax(logits, dim=-1).cpu())
    probs = torch.cat(all_probs, dim=0).numpy()  # (N, 3): easy, medium, hard
    scores = probs[:, 0] * 2.0 + probs[:, 1] * 5.5 + probs[:, 2] * 9.0
    return pd.Series(scores, index=courses_df["id"], name="nlp_score")


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}\n")

    client = get_client()

    # ── Fetch raw tables ────────────────────────────────────────────────────
    print("Fetching courses...")
    courses_df = pd.DataFrame(fetch_all(client, "courses", "id,title,description,department"))
    print(f"  {len(courses_df)} courses")

    print("Fetching instructors...")
    instructors_df = pd.DataFrame(fetch_all(client, "instructors", "ucinetid,shortened_names"))
    print(f"  {len(instructors_df)} instructors")

    print("Fetching course_instructors...")
    ci_df = pd.DataFrame(fetch_all(client, "course_instructors", "course_id,ucinetid"))
    print(f"  {len(ci_df)} (course, instructor) pairs")

    print("Fetching grade_distributions...")
    grades_df = pd.DataFrame(fetch_all(
        client, "grade_distributions",
        "course_id,instructor_raw,grade_a_count,grade_b_count,grade_c_count,"
        "grade_d_count,grade_f_count"
    ))
    print(f"  {len(grades_df)} grade rows")

    print("Fetching rmp_reviews...")
    rmp_df = pd.DataFrame(fetch_all(client, "rmp_reviews", "ucinetid,difficulty_rating,num_ratings"))
    print(f"  {len(rmp_df)} rmp reviews")

    # ── NLP scores ──────────────────────────────────────────────────────────
    print("\nLoading NLP model and scoring courses...")
    encoder = SentenceTransformer(MODEL_DIR, device=str(device))
    classifier = nn.Linear(EMBEDDING_DIM, 3).to(device)
    classifier.load_state_dict(
        torch.load(os.path.join(MODEL_DIR, "classifier.pt"), map_location=device)
    )
    courses_with_desc = courses_df.dropna(subset=["description"])
    nlp_series = nlp_scores(encoder, classifier, courses_with_desc, device)
    print(f"  NLP scores computed for {len(nlp_series)} courses")

    # ── Expand multi-instructor strings split by "/" ─────────────────────────
    _gcols = ["grade_a_count", "grade_b_count", "grade_c_count",
              "grade_d_count", "grade_f_count"]
    grades_df[_gcols] = grades_df[_gcols].fillna(0).astype(float)

    multi_mask = grades_df["instructor_raw"].str.contains("/", na=False)
    n_multi = int(multi_mask.sum())
    expanded_rows = []
    for _, row in grades_df[multi_mask].iterrows():
        parts = [p.strip() for p in str(row["instructor_raw"]).split("/")]
        valid = [p for p in parts if p.upper() != "STAFF" and p]
        if not valid:
            expanded_rows.append(row.to_dict())
            continue
        n = len(valid)
        for name in valid:
            new_row = row.to_dict()
            new_row["instructor_raw"] = name
            for col in _gcols:
                new_row[col] = row[col] / n
            expanded_rows.append(new_row)

    if n_multi > 0:
        grades_df = pd.concat(
            [grades_df[~multi_mask], pd.DataFrame(expanded_rows)],
            ignore_index=True,
        )
    print(f"\nMulti-instructor strings: {n_multi} rows → {len(expanded_rows)} rows after splitting")

    # ── Exact match: instructor_raw → ucinetid via shortened_names ──────────
    name_to_uid = build_name_to_ucinetid(instructors_df)
    grades_df["instructor_raw_clean"] = grades_df["instructor_raw"].apply(
        lambda x: normalize_name(str(x)) if pd.notna(x) else None
    )
    grades_df["ucinetid"] = grades_df["instructor_raw_clean"].map(name_to_uid)

    exact_matched = grades_df["ucinetid"].notna().sum()
    print(f"\nInstructor matching:")
    print(f"  Exact match:  {exact_matched}/{len(grades_df)} rows "
          f"({exact_matched/len(grades_df)*100:.1f}%)")

    # ── Fuzzy fallback for unmatched rows ────────────────────────────────────
    unmatched_mask = grades_df["ucinetid"].isna()
    print(f"  Running fuzzy match on {unmatched_mask.sum()} unmatched rows "
          f"({grades_df['instructor_raw_clean'][unmatched_mask].nunique()} unique names)...")

    candidates, candidate_uids = build_fuzzy_corpus(instructors_df)
    fuzzy_map, fuzzy_details = fuzzy_match_names(
        grades_df.loc[unmatched_mask, "instructor_raw_clean"],
        candidates, candidate_uids, threshold=95,
    )
    grades_df.loc[unmatched_mask, "ucinetid"] = (
        grades_df.loc[unmatched_mask, "instructor_raw_clean"].map(fuzzy_map)
    )

    fuzzy_matched = grades_df["ucinetid"].notna().sum() - exact_matched
    total_matched = grades_df["ucinetid"].notna().sum()
    print(f"  Fuzzy match:  {fuzzy_matched} additional rows matched")
    print(f"  Overall:      {total_matched}/{len(grades_df)} rows "
          f"({total_matched/len(grades_df)*100:.1f}%)")

    # ── Fuzzy match diagnostics ───────────────────────────────────────────────
    details_df = pd.DataFrame(fuzzy_details)

    buckets = [(85, 89), (90, 94), (95, 99), (100, 100)]
    print("\n  Match score distribution:")
    for lo, hi in buckets:
        count = int(((details_df["score"] >= lo) & (details_df["score"] <= hi)).sum()) if not details_df.empty else 0
        label = f"{lo}-{hi}" if lo != hi else f"{lo}"
        print(f"    {label:>7}: {count:>5} unique names")

    if not details_df.empty:
        fuzzy_rows = grades_df[
            unmatched_mask & grades_df["ucinetid"].notna()
        ][["course_id", "instructor_raw_clean"]].copy()
        fuzzy_rows = fuzzy_rows.merge(
            details_df[["original", "matched", "score"]],
            left_on="instructor_raw_clean", right_on="original", how="left"
        )
        sample = fuzzy_rows.sample(n=min(20, len(fuzzy_rows)), random_state=42)
        print("\n  20 random fuzzy-matched pairs:")
        print(f"  {'course_id':<20} {'original':<30} {'matched':<30} {'score':>5}")
        print(f"  {'-'*20} {'-'*30} {'-'*30} {'-'*5}")
        for _, row in sample.iterrows():
            print(f"  {row['course_id']:<20} {row['instructor_raw_clean']:<30} "
                  f"{row['matched']:<30} {row['score']:>5.1f}")

    # ── Per-instructor GPA from matched grade rows ───────────────────────────
    grade_cols = ["grade_a_count", "grade_b_count", "grade_c_count",
                  "grade_d_count", "grade_f_count"]
    grades_df[grade_cols] = grades_df[grade_cols].fillna(0)
    grades_df["graded"] = grades_df[grade_cols].sum(axis=1)
    grades_df["weighted_points"] = (
        grades_df["grade_a_count"] * 4 + grades_df["grade_b_count"] * 3
        + grades_df["grade_c_count"] * 2 + grades_df["grade_d_count"] * 1
    )

    matched_grades = grades_df[
        grades_df["ucinetid"].notna() & (grades_df["graded"] >= MIN_GRADED)
    ]

    inst_gpa = (
        matched_grades.groupby(["course_id", "ucinetid"])
        .apply(lambda g: pd.Series({
            "weighted_gpa": g["weighted_points"].sum() / g["graded"].sum(),
            "sections_taught": len(g),
        }), include_groups=False)
        .reset_index()
    )

    # ── Build base (course_id, ucinetid) frame from course_instructors ───────
    base = ci_df.copy()
    base = base.merge(inst_gpa, on=["course_id", "ucinetid"], how="left")
    base["sections_taught"] = base["sections_taught"].fillna(0).astype(int)

    # ── Attach NLP score (per course) ────────────────────────────────────────
    base["nlp_score"] = base["course_id"].map(nlp_series)

    # ── Attach RMP score (per instructor, normalized 1-5 → 1-10) ────────────
    rmp_agg = (
        rmp_df.groupby("ucinetid")
        .apply(lambda g: np.average(g["difficulty_rating"],
                                    weights=g["num_ratings"].clip(lower=1)),
               include_groups=False)
        .reset_index(name="rmp_difficulty")
    )
    base = base.merge(rmp_agg, on="ucinetid", how="left")
    # avgDifficulty == 0 means "no ratings on record", not "trivially easy". Treating
    # it as a real value produced rmp_score = -1.25 and dragged the blend below scale.
    base.loc[base["rmp_difficulty"] <= 0, "rmp_difficulty"] = np.nan
    base["rmp_score"] = (base["rmp_difficulty"] - 1) / 4.0 * 9.0 + 1.0

    # ── Convert GPA → difficulty score 1-10 ─────────────────────────────────
    base["gpa_score"] = ((4.0 - base["weighted_gpa"]) / 3.0 * 10.0).clip(1.0, 10.0)

    signal_cols = ["nlp_score", "gpa_score", "rmp_score"]

    # ── Put the three signals on a COMMON SCALE before blending ───────────────
    # The weights are only meaningful if the signals are comparable, and raw they
    # are not: nlp and rmp both centre near 5.6 on the nominal 1-10 scale, but
    # gpa_score is crushed into the bottom (mean 1.9) because UCI GPAs cluster near
    # 3.4 and gpa_score = (4 - gpa)/3 * 10.
    #
    # That mismatch made the missing-signal path biased. Renormalising the weights
    # (below) is only scale-preserving if the dropped signal has the same
    # distribution as the ones that remain. It didn't: dropping rmp (mean 5.57) and
    # reweighting toward gpa (mean 1.9) pushed scores DOWN by 0.674 on average, so a
    # course whose professors happened to lack an RMP match scored systematically
    # EASIER — an artifact of name-matching luck, not of the course.
    #
    # Rank-normalising each signal to 1-10 over the rows where it is present gives
    # all three the same marginal distribution, which makes weight renormalisation
    # valid and the 2-signal path unbiased against the 3-signal path by construction.
    # The blend weights themselves are untouched (signal_weights.json stays frozen);
    # this fixes the scale, not the prior.
    def _rank_to_1_10(s: pd.Series) -> pd.Series:
        # float64 throughout: nlp_score arrives as float32 from torch and would
        # otherwise reject the float64 ranks on assignment.
        out = s.astype("float64")
        present = out.notna()
        n = int(present.sum())
        if n <= 1:
            return out
        ranks = out[present].rank(method="average") - 1.0
        out.loc[present] = 1.0 + 9.0 * (ranks / (n - 1))
        return out

    std_cols = []
    for c in signal_cols:
        base[c + "_std"] = _rank_to_1_10(base[c])
        std_cols.append(c + "_std")

    # ── Weighted blend over the standardised signals ──────────────────────────
    # When a signal is absent we DROP its weight and renormalise the remainder to
    # sum to 1. Example (rmp missing): (nlp*0.358 + gpa*0.291) / (0.358 + 0.291).
    def blend(row, cols) -> float:
        num = denom = 0.0
        for c in cols:
            v = row[c + "_std"]
            if pd.notna(v):
                num += SIGNAL_WEIGHTS[c] * v
                denom += SIGNAL_WEIGHTS[c]
        return num / denom if denom > 0 else np.nan

    def present_cols(row) -> tuple[str, ...]:
        return tuple(c for c in signal_cols if pd.notna(row[c + "_std"]))

    base["_pattern"] = base.apply(present_cols, axis=1)
    base["_blend"] = base.apply(lambda r: blend(r, signal_cols), axis=1)

    # ── Missingness calibration ───────────────────────────────────────────────
    # Standardising the signals is necessary but NOT sufficient. Each signal is
    # rank-normalised over the rows where IT is present, and those populations are
    # not the same: on rows carrying all three signals, nlp_score_std averages 6.80
    # while rmp_score_std averages 5.44. So dropping rmp and reweighting toward nlp
    # still drifted scores upward (+0.35) — a smaller, opposite-signed version of the
    # original bias, but a bias all the same.
    #
    # Fix: on the reference population (rows that DO have all three signals), measure
    # what each missingness pattern would score versus the full 3-signal blend, and
    # subtract that offset. A 2-signal row is then an unbiased estimate of what the
    # 3-signal blend would have said, by construction, so a course is not scored
    # easier or harder merely because its professors happened to lack an RMP match.
    ref = base[base["_pattern"].map(len) == len(signal_cols)]
    full = ref.apply(lambda r: blend(r, signal_cols), axis=1)
    offsets: dict[tuple[str, ...], float] = {}
    for pat in base["_pattern"].unique():
        if not pat:
            continue
        partial = ref.apply(lambda r: blend(r, pat), axis=1)
        offsets[pat] = float((full - partial).mean()) if len(ref) else 0.0

    print("\n  Missingness calibration (offset added to each pattern):")
    for pat, off in sorted(offsets.items(), key=lambda kv: -len(kv[0])):
        n = int((base["_pattern"] == pat).sum())
        print(f"    {'+'.join(c.replace('_score','') for c in pat):<16} n={n:<6} offset {off:+.4f}")

    base["difficulty_score"] = (
        base["_blend"] + base["_pattern"].map(offsets).astype(float)
    ).clip(1.0, 10.0)

    # ── Confidence — keyed on WHICH signals are present, nothing more ──────────
    # The calibration above makes a 2-signal row UNBIASED, but unbiased is not
    # precise: a row scored from the NLP classifier alone rests entirely on a text
    # model with a held-out macro F1 of 0.576, and carries far more variance than a
    # row corroborated by grade history and professor ratings. Nothing else in the
    # pipeline communicates that, so a course with no data looked exactly as
    # authoritative as one with three independent signals.
    #
    # This is deliberately NOT a probabilistic confidence — we have no calibrated
    # basis for one. It is a direct, auditable function of the missingness pattern.
    CONFIDENCE = {
        ("nlp_score", "gpa_score", "rmp_score"): "high",    # all three corroborate
        ("nlp_score", "gpa_score"):              "medium",  # text + real grade history
        ("nlp_score", "rmp_score"):              "medium",  # text + professor ratings
        ("nlp_score",):                          "low",     # text alone — a guess
    }
    base["signals_present"] = base["_pattern"].map(
        lambda p: "+".join(c.replace("_score", "") for c in p) if p else ""
    )
    base["confidence"] = base["_pattern"].map(lambda p: CONFIDENCE.get(p, "low"))
    base.loc[base["difficulty_score"].isna(), "confidence"] = None

    # Bias check on the reference population, scored both ways. This is the defect
    # that motivated the calibration, so it is measured rather than trusted.
    print("\n  Bias check — same rows, scored with vs without each signal:")
    for pat in sorted(offsets, key=lambda p: -len(p)):
        if len(pat) == len(signal_cols):
            continue
        shift = (ref.apply(lambda r: blend(r, pat), axis=1) + offsets[pat] - full).mean()
        label = "+".join(c.replace("_score", "") for c in pat)
        print(f"    {label:<16} residual shift vs full blend: {shift:+.4f}")

    base = base.drop(columns=["_pattern", "_blend"])

    # ── Course-level difficulty: sections_taught-weighted avg ─────────────────
    def course_difficulty(g):
        valid = g[g["difficulty_score"].notna()]
        if valid.empty:
            return np.nan
        w = valid["sections_taught"].clip(lower=1)
        return np.average(valid["difficulty_score"], weights=w)

    course_df = (
        base.groupby("course_id")
        .apply(course_difficulty, include_groups=False)
        .reset_index(name="difficulty_score")
    )

    # Course-level confidence: the best-supported instructor row backing the course.
    # A course counts as "high" if at least one of its instructors is corroborated by
    # all three signals — that instructor's data is what the course score leans on.
    _RANK = {"low": 1, "medium": 2, "high": 3}
    _UNRANK = {v: k for k, v in _RANK.items()}
    course_conf = (
        base.dropna(subset=["difficulty_score"])
        .assign(_r=lambda d: d["confidence"].map(_RANK))
        .groupby("course_id")["_r"].max()
        .map(_UNRANK)
    )
    course_df["confidence"] = course_df["course_id"].map(course_conf)

    # ── NLP-only fallback for instructorless courses ──────────────────────────
    # Courses with a description (hence an NLP score) but no course_instructors
    # rows never enter the instructor-centric blend above, so they'd be dropped.
    # Give each one a course-level raw score equal to its NLP score so the whole
    # catalogue is covered, then everyone is normalized together below. (The NLP
    # score is already on the same 1-10 difficulty scale as the blend.)
    # These are text-only by definition, so they are the lowest-confidence tier.
    scored_ids = set(course_df["course_id"])
    fallback = nlp_series[~nlp_series.index.isin(scored_ids)]
    if len(fallback):
        fb_df = pd.DataFrame({
            "course_id": fallback.index,
            "difficulty_score": fallback.values,
            "confidence": "low",
        })
        course_df = pd.concat([course_df, fb_df], ignore_index=True)
        print(f"  NLP-only fallback: added {len(fallback)} instructorless courses")

    # ── Percentile rank-normalize course-level scores across the catalogue ─────
    # Last transform before the score is served. The raw weighted-average score is
    # preserved in difficulty_score_raw; the served difficulty_score becomes the
    # percentile rank mapped to 1-10 (hardest course → 10, easiest → 1). Ties get
    # the average rank so identical raw scores receive identical normalized values.
    course_df["difficulty_score_raw"] = course_df["difficulty_score"]
    valid = course_df["difficulty_score"].notna()
    n_valid = int(valid.sum())
    if n_valid > 1:
        avg_rank = course_df.loc[valid, "difficulty_score"].rank(method="average") - 1.0
        course_df.loc[valid, "difficulty_score"] = 1.0 + 9.0 * (avg_rank / (n_valid - 1))

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    # Raw signals are kept for the API's per-signal breakdown; the *_std columns are
    # what difficulty_score is actually a weighted mean of, so the blend stays
    # auditable from the CSV alone. scripts/load_features.py pushes only the
    # whitelisted Supabase columns, so the extra ones are local-only.
    prof_out = base[["course_id", "ucinetid", "nlp_score", "gpa_score",
                      "rmp_score", "nlp_score_std", "gpa_score_std", "rmp_score_std",
                      "difficulty_score", "confidence", "signals_present",
                      "sections_taught"]].rename(
        columns={"ucinetid": "instructor_id"}
    )
    prof_path = os.path.join(DATA_DIR, "prof_course_features.csv")
    course_path = os.path.join(DATA_DIR, "course_features.csv")
    prof_out.to_csv(prof_path, index=False)
    course_df.to_csv(course_path, index=False)
    print(f"\nSaved {len(prof_out)} rows → {prof_path}")
    print(f"Saved {len(course_df)} rows → {course_path}")

    # ── Signal coverage summary ───────────────────────────────────────────────
    def signal_count(row):
        return sum(pd.notna(row[c]) for c in signal_cols)

    base["n_signals"] = base.apply(signal_count, axis=1)
    course_signals = base.groupby("course_id")["n_signals"].max()

    print(f"\nTotal (course, instructor) pairs: {len(base)}")
    for n in [3, 2, 1, 0]:
        count = (course_signals == n).sum()
        print(f"  Courses with {n} signal{'s' if n != 1 else ''}: {count}")

    # ── Confidence distribution AS SERVED (courses, not feature rows) ──────────
    print("\n  Confidence of the difficulty score AS SERVED (per course):")
    total = len(course_df)
    for tier in ("high", "medium", "low"):
        n = int((course_df["confidence"] == tier).sum())
        print(f"    {tier:<7} {n:>5} courses  ({n / total * 100:5.1f}%)")
    unk = int(course_df["confidence"].isna().sum())
    if unk:
        print(f"    {'none':<7} {unk:>5} courses  ({unk / total * 100:5.1f}%)")


if __name__ == "__main__":
    main()
