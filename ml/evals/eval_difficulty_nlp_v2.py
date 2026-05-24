import json
import os

import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, f1_score, precision_score, recall_score

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(EVALS_DIR, "..", "data")
MODEL_DIR = os.path.join(EVALS_DIR, "..", "models", "difficulty_nlp_v2")

BATCH_SIZE = 32
EMBEDDING_DIM = 384


def format_text(df: pd.DataFrame) -> list[str]:
    return (
        "Department: " + df["department"] + ". "
        "Course: " + df["course_id"] + ". "
        + df["description"]
    ).tolist()


def run_inference(encoder, classifier, texts: list[str], device: torch.device) -> list[int]:
    encoder.eval()
    classifier.eval()
    preds = []
    with torch.no_grad():
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            features = encoder.preprocess(batch)
            features = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in features.items()}
            embeddings = encoder(features)["sentence_embedding"]
            logits = classifier(embeddings)
            preds.extend(logits.argmax(dim=-1).cpu().tolist())
    return preds


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}")

    with open(os.path.join(MODEL_DIR, "label_map.json")) as f:
        label_map = json.load(f)
    labels = label_map["labels"]
    idx2label = {i: l for l, i in label_map["label2idx"].items()}
    print(f"Labels: {labels}")

    encoder = SentenceTransformer(MODEL_DIR, device=str(device))
    classifier = nn.Linear(EMBEDDING_DIM, len(labels)).to(device)
    classifier.load_state_dict(torch.load(os.path.join(MODEL_DIR, "classifier.pt"), map_location=device))

    test_df = pd.read_csv(os.path.join(DATA_DIR, "test.csv"))
    print(f"Test set: {len(test_df)} courses\n")

    texts = format_text(test_df)
    pred_idxs = run_inference(encoder, classifier, texts, device)

    y_pred = [idx2label[i] for i in pred_idxs]
    y_true = test_df["difficulty_tier"].tolist()

    print("Per-tier classification metrics:")
    print(classification_report(y_true, y_pred, labels=labels, digits=4, zero_division=0))

    per_tier = {}
    for tier in labels:
        per_tier[tier] = {
            "precision": float(precision_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
            "recall":    float(recall_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
            "f1":        float(f1_score(y_true, y_pred, labels=[tier], average="macro", zero_division=0)),
        }

    metrics = {
        "per_tier": per_tier,
        "overall": {
            "accuracy":    float((pd.Series(y_pred) == pd.Series(y_true)).mean()),
            "macro_f1":    float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)),
            "weighted_f1": float(f1_score(y_true, y_pred, labels=labels, average="weighted", zero_division=0)),
        },
    }

    out_path = os.path.join(EVALS_DIR, "difficulty_nlp_v2_metrics.json")
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to {out_path}")


if __name__ == "__main__":
    main()
