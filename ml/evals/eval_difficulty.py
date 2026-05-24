import json
import os

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, f1_score

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(EVALS_DIR, "..", "data")
MODELS_DIR = os.path.join(EVALS_DIR, "..", "models")
RESULTS_DIR = os.path.join(EVALS_DIR, "results")

NLP_MODEL_DIR = os.path.join(MODELS_DIR, "difficulty_nlp_v2")
ENSEMBLE_WEIGHTS_PATH = os.path.join(MODELS_DIR, "ensemble_weights.json")

BATCH_SIZE = 32
EMBEDDING_DIM = 384


def format_text(df: pd.DataFrame) -> list[str]:
    return (
        "Department: " + df["department"] + ". "
        "Course: " + df["course_id"] + ". "
        + df["description"]
    ).tolist()


def run_nlp_inference(encoder, classifier, texts: list[str], device: torch.device) -> list[int]:
    encoder.eval()
    classifier.eval()
    preds = []
    with torch.no_grad():
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            features = encoder.preprocess(batch)
            features = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                        for k, v in features.items()}
            embeddings = encoder(features)["sentence_embedding"]
            preds.extend(classifier(embeddings).argmax(dim=-1).cpu().tolist())
    return preds


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )

    train_df = pd.read_csv(os.path.join(DATA_DIR, "train.csv"))
    test_df = pd.read_csv(os.path.join(DATA_DIR, "test.csv"))

    # ── Part 1: NLP classifier eval ─────────────────────────────────────────
    print("=" * 60)
    print("PART 1 — NLP Classifier (difficulty_nlp_v2)")
    print("=" * 60)

    with open(os.path.join(NLP_MODEL_DIR, "label_map.json")) as f:
        label_map = json.load(f)
    labels = label_map["labels"]
    idx2label = {i: l for l, i in label_map["label2idx"].items()}

    encoder = SentenceTransformer(NLP_MODEL_DIR, device=str(device))
    classifier = nn.Linear(EMBEDDING_DIM, len(labels)).to(device)
    classifier.load_state_dict(
        torch.load(os.path.join(NLP_MODEL_DIR, "classifier.pt"), map_location=device)
    )

    texts = format_text(test_df)
    pred_idxs = run_nlp_inference(encoder, classifier, texts, device)

    y_pred = [idx2label[i] for i in pred_idxs]
    y_true = test_df["difficulty_tier"].tolist()

    print(classification_report(y_true, y_pred, labels=labels, digits=4, zero_division=0))
    nlp_macro_f1 = float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0))
    nlp_accuracy = float((pd.Series(y_pred) == pd.Series(y_true)).mean())
    print(f"Macro F1: {nlp_macro_f1:.4f}  |  Accuracy: {nlp_accuracy:.4f}")

    nlp_results = {
        "macro_f1": nlp_macro_f1,
        "accuracy": nlp_accuracy,
        "per_tier": {},
    }
    for tier in labels:
        tier_true = [1 if t == tier else 0 for t in y_true]
        tier_pred = [1 if p == tier else 0 for p in y_pred]
        tp = sum(a and b for a, b in zip(tier_true, tier_pred))
        fp = sum(not a and b for a, b in zip(tier_true, tier_pred))
        fn = sum(a and not b for a, b in zip(tier_true, tier_pred))
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec  = tp / (tp + fn) if (tp + fn) else 0.0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        nlp_results["per_tier"][tier] = {"precision": prec, "recall": rec, "f1": f1}

    with open(os.path.join(RESULTS_DIR, "nlp_eval.json"), "w") as f:
        json.dump(nlp_results, f, indent=2)
    print(f"Saved → results/nlp_eval.json\n")

    # ── Part 2: Ensemble eval ────────────────────────────────────────────────
    print("=" * 60)
    print("PART 2 — Ensemble Signal Comparison")
    print("=" * 60)

    feat_df = pd.read_csv(os.path.join(DATA_DIR, "prof_course_features.csv"))
    with open(ENSEMBLE_WEIGHTS_PATH) as f:
        ew = json.load(f)

    coef = ew["coefficients"]
    intercept = ew["intercept"]
    w_nlp, w_gpa, w_rmp = coef["nlp_score"], coef["gpa_score"], coef["rmp_score"]

    # Only rows with all 3 signals
    all3 = feat_df.dropna(subset=["nlp_score", "gpa_score", "rmp_score", "difficulty_score"]).copy()
    print(f"Rows with all 3 signals: {len(all3)}")

    # Ground truth: difficulty_score — the Ridge ensemble target
    gt = all3["difficulty_score"]

    print(f"""
Ground truth column : difficulty_score
Source              : prof_course_features.csv — built in ml/data/build_features.py
Derivation          : equal-weight mean of (nlp_score, gpa_score, rmp_score),
                      averaging only signals present per row
Note                : this is the Ridge ensemble training target combining all
                      three signals, not raw GPA alone
Expected range      : [1.0, 10.0]""")

    # Ensemble prediction using learned Ridge weights
    all3["ensemble_pred"] = (
        w_nlp * all3["nlp_score"]
        + w_gpa * all3["gpa_score"]
        + w_rmp * all3["rmp_score"]
        + intercept
    )

    print(f"\n{'Statistic':<12} {'ground truth (difficulty_score)':>31} {'ensemble_pred':>15}")
    print(f"{'-'*12} {'-'*31} {'-'*15}")
    for stat, gv, pv in [
        ("min",  gt.min(),  all3["ensemble_pred"].min()),
        ("max",  gt.max(),  all3["ensemble_pred"].max()),
        ("mean", gt.mean(), all3["ensemble_pred"].mean()),
        ("std",  gt.std(),  all3["ensemble_pred"].std()),
    ]:
        print(f"{stat:<12} {gv:>31.4f} {pv:>15.4f}")

    mae_ensemble = float((all3["ensemble_pred"] - gt).abs().mean())
    mae_nlp      = float((all3["nlp_score"]      - gt).abs().mean())
    mae_rmp      = float((all3["rmp_score"]       - gt).abs().mean())

    print(f"\n{'Signal':<25} {'MAE vs difficulty_score':>24}")
    print(f"{'-'*25} {'-'*24}")
    print(f"{'Ensemble (all 3)':<25} {mae_ensemble:>24.4f}")
    print(f"{'NLP alone':<25} {mae_nlp:>24.4f}")
    print(f"{'RMP alone':<25} {mae_rmp:>24.4f}")

    ensemble_results = {
        "ground_truth": "difficulty_score (equal-weight mean of nlp_score, gpa_score, rmp_score)",
        "n_rows_all_signals": len(all3),
        "mae_ensemble": mae_ensemble,
        "mae_nlp_alone": mae_nlp,
        "mae_rmp_alone": mae_rmp,
        "ensemble_weights": coef,
        "intercept": intercept,
    }
    with open(os.path.join(RESULTS_DIR, "ensemble_eval.json"), "w") as f:
        json.dump(ensemble_results, f, indent=2)
    print(f"\nSaved → results/ensemble_eval.json\n")

    # ── Part 3: Summary block ────────────────────────────────────────────────
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"NLP classifier macro F1:  {nlp_macro_f1:.3f}")
    print(f"Ensemble MAE:             {mae_ensemble:.3f}  "
          f"(vs NLP-alone MAE: {mae_nlp:.3f}, RMP-alone MAE: {mae_rmp:.3f})")
    print(f"Training courses:         {len(train_df)}  |  Test courses: {len(test_df)}")
    print(f"Signals:                  NLP embedding + GPA distribution + RMP difficulty rating")


if __name__ == "__main__":
    main()
