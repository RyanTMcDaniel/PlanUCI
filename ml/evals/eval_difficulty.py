"""
Held-out evaluation of the course difficulty tier classifier (difficulty_nlp_v2).

Scores the fine-tuned MiniLM + linear head on ml/data/test.csv — courses the model
never saw, with tier cutoffs computed on the training split only (see
ml/data/preprocess.py).  This is the project's one genuinely held-out ML metric.

NOTE ON WHAT IS *NOT* HERE.  This script used to contain a second section that
scored the nlp/gpa/rmp blend against `difficulty_score` and reported an MAE.  That
section was removed: `difficulty_score` is constructed in ml/data/build_features.py
as a weighted mean of those same three signals, so the "model" was being scored
against a deterministic function of its own inputs and the MAE measured nothing.
The blend is a documented prior, not a validated model — see
ml/models/fit_signal_weights.py and DECISIONS.md.

Usage:
    python -m ml.evals.eval_difficulty
"""

import json
import os

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

BATCH_SIZE = 32
EMBEDDING_DIM = 384


def format_text(df: pd.DataFrame) -> list[str]:
    """Format inputs EXACTLY as training and serving do.

    Must stay identical to ml/scripts/train_difficulty_nlp_v2.py::format_text and
    ml/data/build_features.py::nlp_scores.  These three disagreed previously — the
    trainer and the feature builder used "{title}: {description}" while this script
    used "Department: {dept}. Course: {id}. {description}", so the published score
    was measured on an input format the model had never seen.
    """
    title = df["title"].fillna(df["course_id"])
    return (title + ": " + df["description"]).tolist()


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

    print("=" * 62)
    print("Held-out eval — difficulty_nlp_v2 (course difficulty tier)")
    print("=" * 62)

    with open(os.path.join(NLP_MODEL_DIR, "label_map.json")) as f:
        label_map = json.load(f)
    labels = label_map["labels"]
    idx2label = {i: l for l, i in label_map["label2idx"].items()}

    encoder = SentenceTransformer(NLP_MODEL_DIR, device=str(device))
    classifier = nn.Linear(EMBEDDING_DIM, len(labels)).to(device)
    classifier.load_state_dict(
        torch.load(os.path.join(NLP_MODEL_DIR, "classifier.pt"), map_location=device)
    )

    pred_idxs = run_nlp_inference(encoder, classifier, format_text(test_df), device)
    y_pred = [idx2label[i] for i in pred_idxs]
    y_true = test_df["difficulty_tier"].tolist()

    print(classification_report(y_true, y_pred, labels=labels, digits=4, zero_division=0))

    macro_f1 = float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0))
    accuracy = float((pd.Series(y_pred) == pd.Series(y_true)).mean())
    per_tier = dict(zip(labels, (float(v) for v in f1_score(
        y_true, y_pred, labels=labels, average=None, zero_division=0
    ))))

    print(f"Macro F1: {macro_f1:.4f}  |  Accuracy: {accuracy:.4f}")
    print(f"Train courses: {len(train_df)}  |  Test courses: {len(test_df)}")

    results = {
        "model": "difficulty_nlp_v2",
        "produced_by": "ml/evals/eval_difficulty.py",
        "macro_f1": round(macro_f1, 4),
        "accuracy": round(accuracy, 4),
        "f1_per_tier": {k: round(v, 4) for k, v in per_tier.items()},
        "train_courses": len(train_df),
        "test_courses": len(test_df),
        "input_format": "{title}: {description} — identical to training and serving",
        "note": (
            "Held out: tier cutoffs are computed on the training split only and the "
            "splits assert zero course_id overlap (ml/data/preprocess.py). The hard "
            f"tier is a minority of the {len(test_df)}-course test set, so its F1 has a "
            "wide confidence interval."
        ),
    }
    with open(os.path.join(RESULTS_DIR, "nlp_eval.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved → results/nlp_eval.json")


if __name__ == "__main__":
    main()
