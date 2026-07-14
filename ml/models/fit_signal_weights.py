"""
Derive the blend weights for the difficulty scoring heuristic.

THIS IS NOT MODEL TRAINING, AND THE OUTPUT CANNOT BE EVALUATED.

`difficulty_score` — the column this script regresses onto — is itself built in
ml/data/build_features.py as a renormalized weighted mean of the same three
columns used as features here (nlp_score, gpa_score, rmp_score), using the very
weights this script writes out.  The target is therefore a deterministic linear
function of the inputs: a closed-form weighted mean reconstructs it with
MAE ~2e-8 and r = 1.0.  The Ridge fit is a fixed point of that construction, not
an estimate of anything, and ANY error metric computed against this target
(MAE, R², cross-validated or not) measures internal consistency only.  No such
metric is reported here, deliberately — an earlier version of this script printed
a cross-validated MAE, and that number was misread as model performance.

What the three weights actually are: a **prior** over how much each signal should
count toward a course's difficulty.  They are reasonable and they are stable, but
they are not validated, because no exogenous ground truth for course difficulty
exists in this project yet.  See DECISIONS.md for what a real label would be.

The one honest thing this script can report is how much the raw signals disagree
with each other — that is a property of the data, not of a model, and it is
printed below as a diagnostic.

Usage:
    python -m ml.models.fit_signal_weights
"""

import json
import os
import sys

import pandas as pd
from sklearn.linear_model import Ridge

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

FEATURES = ["nlp_score", "gpa_score", "rmp_score"]
TARGET = "difficulty_score"
ALPHA = 1.0


def main() -> None:
    df = pd.read_csv(os.path.join(DATA_DIR, "prof_course_features.csv"))
    print(f"Loaded {len(df)} (course, instructor) rows")

    df["n_missing"] = df[FEATURES].isna().sum(axis=1)
    print(f"  0 signals missing : {(df['n_missing'] == 0).sum()}")
    print(f"  1 signal  missing : {(df['n_missing'] == 1).sum()}")
    print(f"  2+ signals missing: {(df['n_missing'] >= 2).sum()}")

    all3 = df[df["n_missing"] == 0].copy()

    df = df[df["n_missing"] <= 1].copy()
    for col in FEATURES:
        df[col] = df[col].fillna(df[col].mean())
    df = df.dropna(subset=[TARGET])
    print(f"Rows used for the fit: {len(df)}\n")

    # ── Signal disagreement — a real property of the data, not a model metric ──
    print(f"Pairwise correlation between the raw signals "
          f"({len(all3)} rows with all three present):")
    corr = all3[FEATURES].corr()
    for i, a in enumerate(FEATURES):
        for b in FEATURES[i + 1:]:
            print(f"  {a:<10} vs {b:<10}  r = {corr.loc[a, b]:+.3f}")
    print("\n  The three signals are correlated but far from redundant, which is why\n"
          "  all three are kept — and why OLS coefficients would swing under\n"
          "  resampling, so Ridge (L2) is used to damp them.\n")

    # ── Re-fit, and show how far it drifts from the shipped prior ─────────────
    model = Ridge(alpha=ALPHA)
    model.fit(df[FEATURES].values, df[TARGET].values)
    refit = dict(zip(FEATURES, (float(c) for c in model.coef_)))

    out_path = os.path.join(MODEL_DIR, "signal_weights.json")
    with open(out_path) as f:
        shipped = json.load(f)
    prior = shipped["coefficients"]

    print("Re-fitting the blend on features that were themselves built from the")
    print("SHIPPED weights.  If this were estimation, the fit would land back on")
    print("the prior.  It does not — the drift below IS the circularity:\n")
    print(f"  {'signal':<10} {'shipped prior':>14} {'re-fit':>10} {'drift':>9}")
    print(f"  {'-'*10} {'-'*14} {'-'*10} {'-'*9}")
    for k in FEATURES:
        d = refit[k] - prior[k]
        print(f"  {k:<10} {prior[k]:>14.4f} {refit[k]:>10.4f} {d:>+9.4f}")

    print("\n  Feed the re-fit weights back through build_features.py and the target")
    print("  moves, so the next fit lands somewhere else again.  A fixed point that")
    print("  shifts every iteration is not an estimate of anything.")
    print("\n  The shipped weights are therefore FROZEN as a documented prior — this")
    print("  script does not overwrite them.  It exists to demonstrate why no accuracy")
    print("  claim is attached to the blend.  Pass --write to overwrite anyway (this")
    print("  changes every difficulty score in the product; rebuild features after).")

    if "--write" in sys.argv:
        shipped["coefficients"] = refit
        shipped["intercept"] = float(model.intercept_)
        with open(out_path, "w") as f:
            json.dump(shipped, f, indent=2)
        print(f"\n--write given: overwrote {out_path}")
    else:
        print(f"\nLeft {out_path} unchanged.")
    print("No performance metric reported — see the module docstring for why.")


if __name__ == "__main__":
    main()
