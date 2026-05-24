import os

import pandas as pd
from sklearn.model_selection import train_test_split

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def assign_tier(score: pd.Series, p20: float, p80: float) -> pd.Series:
    return pd.cut(
        score,
        bins=[-0.001, p20, p80, 10.001],
        labels=["easy", "medium", "hard"],
        right=True,
    )


def print_distribution(name: str, df: pd.DataFrame) -> None:
    counts = df["difficulty_tier"].value_counts().reindex(["easy", "medium", "hard"])
    print(f"\n  {name} ({len(df)} courses):")
    for tier, count in counts.items():
        pct = count / len(df) * 100
        print(f"    {tier:<8} {count:>4}  ({pct:.1f}%)")


def main() -> None:
    df = pd.read_csv(os.path.join(DATA_DIR, "courses_with_gpa.csv"))
    print(f"Loaded {len(df)} courses")
    print(f"  GPA range: {df['course_gpa'].min():.4f} – {df['course_gpa'].max():.4f}")

    gpa_min = df["course_gpa"].min()
    gpa_max = df["course_gpa"].max()
    df["difficulty_score"] = 1 + (gpa_max - df["course_gpa"]) / (gpa_max - gpa_min) * 9
    print(f"  difficulty_score range: {df['difficulty_score'].min():.2f} – {df['difficulty_score'].max():.2f}")

    # Initial split without stratification — tiers not yet defined
    train, temp = train_test_split(df, test_size=0.30, random_state=42)
    val, test = train_test_split(temp, test_size=0.50, random_state=42)

    # Compute percentile cutoffs from training set only
    p20 = train["difficulty_score"].quantile(0.20)
    p80 = train["difficulty_score"].quantile(0.80)
    print(f"\nPercentile cutoffs (from training set):")
    print(f"  p20 (easy/medium boundary): {p20:.4f}")
    print(f"  p80 (medium/hard boundary): {p80:.4f}")

    # Apply same cutoffs to all three splits
    for split in (train, val, test):
        split["difficulty_tier"] = assign_tier(split["difficulty_score"], p20, p80)

    train.to_csv(os.path.join(DATA_DIR, "train.csv"), index=False)
    val.to_csv(os.path.join(DATA_DIR, "val.csv"), index=False)
    test.to_csv(os.path.join(DATA_DIR, "test.csv"), index=False)

    print("\nSplit sizes and difficulty distributions:")
    print_distribution("train", train)
    print_distribution("val  ", val)
    print_distribution("test ", test)

    train_ids = set(train["course_id"])
    val_ids = set(val["course_id"])
    test_ids = set(test["course_id"])
    assert not (train_ids & val_ids), "train/val overlap"
    assert not (train_ids & test_ids), "train/test overlap"
    assert not (val_ids & test_ids), "val/test overlap"
    print("\nNo overlap between splits — OK")


if __name__ == "__main__":
    main()
