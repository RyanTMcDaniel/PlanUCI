import json
import os

import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, f1_score, precision_score, recall_score

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(EVALS_DIR, "..", "data")
MODEL_DIR = os.path.join(EVALS_DIR, "..", "models", "difficulty_nlp_v1")

BATCH_SIZE = 32
EMBEDDING_DIM = 384


def assign_tiers(scores: pd.Series, p20: float, p80: float) -> pd.Series:
    return pd.cut(
        scores,
        bins=[-0.001, p20, p80, 10.001],
        labels=["easy", "medium", "hard"],
        right=True,
    ).astype(str)


def run_inference(encoder, regressor, texts: list[str], device: torch.device) -> list[float]:
    encoder.eval()
    regressor.eval()
    preds = []
    with torch.no_grad():
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            features = encoder.preprocess(batch)
            features = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in features.items()}
            embeddings = encoder(features)["sentence_embedding"]
            batch_preds = regressor(embeddings).squeeze(-1).cpu().tolist()
            if isinstance(batch_preds, float):
                batch_preds = [batch_preds]
            preds.extend(batch_preds)
    return preds


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}")

    with open(os.path.join(MODEL_DIR, "tier_cutoffs.json")) as f:
        cutoffs = json.load(f)
    p20, p80 = cutoffs["p20"], cutoffs["p80"]
    print(f"Tier cutoffs — easy/medium: {p20:.4f}  medium/hard: {p80:.4f}")

    encoder = SentenceTransformer(MODEL_DIR, device=str(device))
    regressor = nn.Linear(EMBEDDING_DIM, 1).to(device)
    regressor.load_state_dict(torch.load(os.path.join(MODEL_DIR, "regressor.pt"), map_location=device))

    test_df = pd.read_csv(os.path.join(DATA_DIR, "test.csv"))
    print(f"\nTest set: {len(test_df)} courses")

    texts = test_df["description"].tolist()
    pred_scores = run_inference(encoder, regressor, texts, device)

    test_df["pred_score"] = pred_scores
    test_df["pred_tier"] = assign_tiers(pd.Series(pred_scores), p20, p80)
    test_df["true_tier"] = assign_tiers(test_df["difficulty_score"], p20, p80)

    mae = (test_df["pred_score"] - test_df["difficulty_score"]).abs().mean()
    print(f"\nMAE (difficulty_score): {mae:.4f}")

    labels = ["easy", "medium", "hard"]
    y_true = test_df["true_tier"]
    y_pred = test_df["pred_tier"]

    report = classification_report(y_true, y_pred, labels=labels, digits=4)
    print(f"\nPer-tier classification metrics:\n{report}")

    per_tier = {}
    for tier in labels:
        per_tier[tier] = {
            "precision": float(precision_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
            "recall":    float(recall_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
            "f1":        float(f1_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
        }

    metrics = {
        "mae": float(mae),
        "tier_cutoffs": {"p20": p20, "p80": p80},
        "per_tier": per_tier,
        "overall": {
            "macro_f1":    float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)),
            "weighted_f1": float(f1_score(y_true, y_pred, labels=labels, average="weighted", zero_division=0)),
        },
    }

    out_path = os.path.join(EVALS_DIR, "difficulty_nlp_v1_metrics.json")
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to {out_path}")


if __name__ == "__main__":
    main()
