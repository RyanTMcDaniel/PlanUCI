import json
import os

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.model_selection import KFold, cross_val_score

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

FEATURES = ["nlp_score", "gpa_score", "rmp_score"]
TARGET = "difficulty_score"
ALPHA = 1.0


def main() -> None:
    df = pd.read_csv(os.path.join(DATA_DIR, "prof_course_features.csv"))
    print(f"Loaded {len(df)} rows")

    df["n_missing"] = df[FEATURES].isna().sum(axis=1)
    print(f"  Missing 0 signals: {(df['n_missing'] == 0).sum()}")
    print(f"  Missing 1 signal:  {(df['n_missing'] == 1).sum()}")
    print(f"  Missing 2+ signals: {(df['n_missing'] >= 2).sum()}")

    df = df[df["n_missing"] <= 1].copy()
    print(f"After dropping rows missing >1 signal: {len(df)} rows")

    for col in FEATURES:
        col_mean = df[col].mean()
        df[col] = df[col].fillna(col_mean)

    df = df.dropna(subset=[TARGET])
    print(f"After dropping rows with missing target: {len(df)} rows\n")

    X = df[FEATURES].values
    y = df[TARGET].values

    model = Ridge(alpha=ALPHA)
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    neg_mae = cross_val_score(model, X, y, cv=kf, scoring="neg_mean_absolute_error")
    mae_scores = -neg_mae
    print(f"5-fold CV MAE: {mae_scores.mean():.4f} ± {mae_scores.std():.4f}")

    model.fit(X, y)
    w1, w2, w3 = model.coef_
    print(f"\nLearned weights (Ridge alpha={ALPHA}):")
    print(f"  w1 (nlp_score): {w1:.4f}")
    print(f"  w2 (gpa_score): {w2:.4f}")
    print(f"  w3 (rmp_score): {w3:.4f}")
    print(f"  intercept:      {model.intercept_:.4f}")

    weights = {
        "features": FEATURES,
        "alpha": ALPHA,
        "coefficients": {
            "nlp_score": float(w1),
            "gpa_score": float(w2),
            "rmp_score": float(w3),
        },
        "intercept": float(model.intercept_),
    }
    out_path = os.path.join(MODEL_DIR, "ensemble_weights.json")
    with open(out_path, "w") as f:
        json.dump(weights, f, indent=2)
    print(f"\nSaved weights → {out_path}")


if __name__ == "__main__":
    main()
