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
EMBEDDING_DIM = 384
BATCH_SIZE = 32
MIN_GRADED = 15  # minimum graded students per section


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
    courses_df = pd.DataFrame(fetch_all(client, "courses", "id,description,department"))
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
    base["rmp_score"] = (base["rmp_difficulty"] - 1) / 4.0 * 9.0 + 1.0

    # ── Convert GPA → difficulty score 1-10 ─────────────────────────────────
    base["gpa_score"] = ((4.0 - base["weighted_gpa"]) / 3.0 * 10.0).clip(1.0, 10.0)

    # ── Weighted combo: equal weights, normalised for missing signals ─────────
    signal_cols = ["nlp_score", "gpa_score", "rmp_score"]

    def combined_score(row):
        vals = [row[c] for c in signal_cols if pd.notna(row[c])]
        return np.mean(vals) if vals else np.nan

    base["difficulty_score"] = base.apply(combined_score, axis=1)

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

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    prof_out = base[["course_id", "ucinetid", "nlp_score", "gpa_score",
                      "rmp_score", "difficulty_score", "sections_taught"]].rename(
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


if __name__ == "__main__":
    main()
