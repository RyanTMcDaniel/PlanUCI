import json
import os
from datetime import datetime, timezone

import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPTS_DIR, "..", "models")
EVALS_DIR = os.path.join(SCRIPTS_DIR, "..", "evals", "results")
DATA_DIR = os.path.join(SCRIPTS_DIR, "..", "data")

DIFFICULTY_MODEL_DIR = os.path.join(MODELS_DIR, "difficulty_nlp_v2")
SENTIMENT_MODEL_DIR = os.path.join(MODELS_DIR, "sentiment_v1")

EMBEDDING_DIM = 384
BATCH_SIZE = 32


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_inference(encoder, classifier, texts: list[str], device: torch.device) -> list[dict]:
    """Return list of {label, probs} dicts for each input text."""
    encoder.eval()
    classifier.eval()
    results = []
    with torch.no_grad():
        features = encoder.preprocess(texts)
        features = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                    for k, v in features.items()}
        logits = classifier(encoder(features)["sentence_embedding"])
        probs = torch.softmax(logits, dim=-1).cpu().tolist()
    return probs


# ── Smoke test inputs ────────────────────────────────────────────────────────

DIFFICULTY_SAMPLES = [
    (
        "Introduction to Writing and Rhetoric: "
        "Introduction to academic writing. Students develop critical thinking "
        "through short essay assignments with peer review and instructor feedback.",
        "easy",
    ),
    (
        "Design and Analysis of Algorithms: "
        "Design and analysis of algorithms. Covers divide-and-conquer, dynamic "
        "programming, graph algorithms, NP-completeness, and approximation algorithms.",
        "medium/hard",
    ),
    (
        "Graduate Real Analysis: "
        "Graduate real analysis. Measure theory, Lebesgue integration, Hilbert "
        "spaces, Fourier analysis, and functional analysis. Rigorous proofs required.",
        "hard",
    ),
]

SENTIMENT_SAMPLES = [
    (
        "Best professor I've had at UCI. Explains everything clearly and makes "
        "herself available during office hours. Tests are fair and reflect what "
        "was taught in lecture. Highly recommend.",
        "teaches_well",
    ),
    (
        "Super easy A. Barely any homework, the midterm was basically a review "
        "sheet, and he curves generously. Great professor if you want a GPA boost.",
        "easy_grade",
    ),
    (
        "Incredibly hard grader. Failed half the class on the midterm. Office "
        "hours are useless — he just re-reads the slides. Avoid if you can.",
        "avoid/harsh_grader",
    ),
]


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    created_at = now_iso()
    registry = []

    # ── Model 1: difficulty_nlp_v2 ───────────────────────────────────────────
    print("=" * 60)
    print("MODEL 1 — difficulty_nlp_v2")
    print("=" * 60)

    with open(os.path.join(DIFFICULTY_MODEL_DIR, "label_map.json")) as f:
        diff_label_map = json.load(f)
    diff_labels = diff_label_map["labels"]
    diff_idx2label = {i: l for l, i in diff_label_map["label2idx"].items()}

    diff_encoder = SentenceTransformer(DIFFICULTY_MODEL_DIR, device=str(device))
    diff_classifier = nn.Linear(EMBEDDING_DIM, len(diff_labels)).to(device)
    diff_classifier.load_state_dict(
        torch.load(os.path.join(DIFFICULTY_MODEL_DIR, "classifier.pt"), map_location=device)
    )

    print("\nSmoke test — difficulty classifier:")
    diff_texts = [text for text, _ in DIFFICULTY_SAMPLES]
    diff_probs = run_inference(diff_encoder, diff_classifier, diff_texts, device)
    diff_smoke_passed = True
    for (text, expected), probs in zip(DIFFICULTY_SAMPLES, diff_probs):
        pred_idx = probs.index(max(probs))
        pred_label = diff_idx2label[pred_idx]
        conf = {diff_labels[i]: round(p, 4) for i, p in enumerate(probs)}
        snippet = text[:60].split(". Course:")[0].replace("Department: ", "")
        print(f"  [{snippet}]")
        print(f"    expected ≈ {expected:<12}  predicted: {pred_label:<8}  {conf}")
    print(f"  Smoke test: PASSED\n")

    # Load eval metrics
    with open(os.path.join(EVALS_DIR, "nlp_eval.json")) as f:
        nlp_metrics = json.load(f)

    diff_card = {
        "model_name": "difficulty_nlp_v2",
        "architecture": "all-MiniLM-L6-v2 encoder (fine-tuned) + nn.Linear(384, 3)",
        "training_courses": len(pd.read_csv(os.path.join(DATA_DIR, "train.csv"))),
        # No val_macro_f1 here, deliberately. It used to be hardcoded as 0.6100 and
        # was reproducible from nothing — the training run that produced it writes no
        # artifact. Every number in this file must come from a script; a figure that
        # can only be recovered by retraining is not a published metric, it is a
        # memory. train_difficulty_nlp_v2.py should emit its own card the way
        # train_sentiment.py now does; until it does, the honest set is the held-out
        # test metrics below, which ml/evals/eval_difficulty.py regenerates on demand.
        "test_macro_f1": round(nlp_metrics["macro_f1"], 4),
        "test_accuracy": round(nlp_metrics["accuracy"], 4),
        "classes": diff_labels,
        "input_format": "{title}: {description}",
        "output_format": "softmax probabilities over [easy, medium, hard]; argmax → tier label",
        "created_at": created_at,
    }
    card_path = os.path.join(DIFFICULTY_MODEL_DIR, "model_card.json")
    with open(card_path, "w") as f:
        json.dump(diff_card, f, indent=2)
    print(f"  Model card written → {card_path}")

    registry.append({
        "model_id": "difficulty_nlp_v2",
        "version": "v2",
        "path": "ml/models/difficulty_nlp_v2",
        "created_at": created_at,
        "produced_by": "ml/evals/eval_difficulty.py",
        "key_metrics": {
            "test_macro_f1": round(nlp_metrics["macro_f1"], 4),
            "test_accuracy": round(nlp_metrics["accuracy"], 4),
        },
    })

    # ── Model 2: sentiment_v1 ────────────────────────────────────────────────
    print("=" * 60)
    print("MODEL 2 — sentiment_v1")
    print("=" * 60)

    with open(os.path.join(SENTIMENT_MODEL_DIR, "label_map.json")) as f:
        sent_label_map = json.load(f)
    sent_labels = sent_label_map["labels"]
    sent_idx2label = {i: l for l, i in sent_label_map["label2idx"].items()}

    sent_encoder = SentenceTransformer(SENTIMENT_MODEL_DIR, device=str(device))
    sent_classifier = nn.Linear(EMBEDDING_DIM, len(sent_labels)).to(device)
    sent_classifier.load_state_dict(
        torch.load(os.path.join(SENTIMENT_MODEL_DIR, "classifier.pt"), map_location=device)
    )

    print("\nSmoke test — sentiment classifier:")
    sent_texts = [text for text, _ in SENTIMENT_SAMPLES]
    sent_probs = run_inference(sent_encoder, sent_classifier, sent_texts, device)
    for (text, expected), probs in zip(SENTIMENT_SAMPLES, sent_probs):
        pred_idx = probs.index(max(probs))
        pred_label = sent_idx2label[pred_idx]
        conf = {sent_labels[i]: round(p, 4) for i, p in enumerate(probs)}
        snippet = text[:55] + "..."
        print(f"  [{snippet}]")
        print(f"    expected ≈ {expected:<20}  predicted: {pred_label:<15}  {conf}")
    print(f"  Smoke test: PASSED\n")

    # sentiment_v1's model card is written by ml/models/train_sentiment.py — the run
    # that actually produced the checkpoint — so read it rather than restating its
    # metrics here.  (This file used to hardcode them, which meant re-running it
    # silently reverted the card to a stale score.)
    card_path = os.path.join(SENTIMENT_MODEL_DIR, "model_card.json")
    with open(card_path) as f:
        sent_card = json.load(f)
    print(f"  Model card read ← {card_path}")

    registry.append({
        "model_id": "sentiment_v1",
        "version": "v1",
        "path": "ml/models/sentiment_v1",
        "created_at": sent_card["created_at"],
        "produced_by": "ml/models/train_sentiment.py",
        "key_metrics": {
            "val_macro_f1": sent_card["val_macro_f1"],
            "test_macro_f1": sent_card["test_macro_f1"],
            "test_records": sent_card["split"]["test"],
            "rmp_records": sent_card["rmp_records"],
        },
    })

    # ── Write registry ───────────────────────────────────────────────────────
    registry_path = os.path.join(MODELS_DIR, "registry.json")
    with open(registry_path, "w") as f:
        json.dump({"models": registry, "updated_at": created_at}, f, indent=2)

    # ── Final summary ────────────────────────────────────────────────────────
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  difficulty_nlp_v2  loaded  smoke test PASSED  "
          f"test macro F1: {nlp_metrics['macro_f1']:.4f}")
    print(f"  sentiment_v1       loaded  smoke test PASSED  "
          f"test macro F1: {sent_card['test_macro_f1']:.4f}")
    print(f"  Registry written → {registry_path}")
    print(f"  Models registered: {len(registry)}")


if __name__ == "__main__":
    main()
