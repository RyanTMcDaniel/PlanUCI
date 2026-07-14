import datetime
import json
import os

import pandas as pd
import torch
import torch.nn as nn
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split
from supabase import create_client
from torch.utils.data import DataLoader, Dataset

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend", ".env")
load_dotenv(_ENV)

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentiment_v1")

BATCH_SIZE = 16
EPOCHS = 30
PATIENCE = 4
LR = 2e-5
MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

LABELS = ["teaches_well", "easy_grade", "harsh_grader", "avoid"]
LABEL2IDX = {l: i for i, l in enumerate(LABELS)}
PRIORITY = ["avoid", "harsh_grader", "teaches_well", "easy_grade"]


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


def assign_label(row: pd.Series) -> str | None:
    overall = row["overall_rating"]
    difficulty = row["difficulty_rating"]
    wta = row["would_take_again_pct"]

    matches = set()
    if pd.notna(overall) and pd.notna(wta) and overall <= 2.5 and wta <= 40:
        matches.add("avoid")
    if pd.notna(difficulty) and pd.notna(wta) and difficulty >= 3.8 and wta <= 50:
        matches.add("harsh_grader")
    if pd.notna(overall) and pd.notna(difficulty) and pd.notna(wta) and overall >= 4.0 and difficulty >= 3.0 and wta >= 75:
        matches.add("teaches_well")
    if pd.notna(overall) and pd.notna(difficulty) and pd.notna(wta) and overall >= 3.8 and difficulty <= 2.5 and wta >= 70:
        matches.add("easy_grade")

    for label in PRIORITY:
        if label in matches:
            return label
    return None


def oversample(df: pd.DataFrame) -> pd.DataFrame:
    target = df["label"].value_counts().max()
    parts = [df]
    for label in LABELS:
        label_df = df[df["label"] == label]
        if len(label_df) == 0 or len(label_df) >= target:
            continue
        needed = target - len(label_df)
        parts.append(label_df.sample(n=needed, random_state=42, replace=True))
    return pd.concat(parts, ignore_index=True).sample(frac=1, random_state=42).reset_index(drop=True)


class SentimentDataset(Dataset):
    def __init__(self, texts: list[str], labels: list[int]):
        self.texts = texts
        self.labels = torch.tensor(labels, dtype=torch.long)

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        return self.texts[idx], self.labels[idx]


def collate_fn(batch):
    texts, labels = zip(*batch)
    return list(texts), torch.stack(labels)


def encode(encoder: SentenceTransformer, texts: list[str], device: torch.device) -> torch.Tensor:
    features = encoder.preprocess(texts)
    features = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in features.items()}
    return encoder(features)["sentence_embedding"]


def evaluate(encoder, classifier, loader, device) -> tuple[float, str, dict[str, float]]:
    encoder.eval()
    classifier.eval()
    all_preds, all_true = [], []
    with torch.no_grad():
        for texts, labels in loader:
            embeddings = encode(encoder, texts, device)
            logits = classifier(embeddings)
            all_preds.extend(logits.argmax(dim=-1).cpu().tolist())
            all_true.extend(labels.tolist())
    pred_labels = [LABELS[p] for p in all_preds]
    true_labels = [LABELS[t] for t in all_true]
    macro_f1 = f1_score(true_labels, pred_labels, labels=LABELS, average="macro", zero_division=0)
    report = classification_report(true_labels, pred_labels, labels=LABELS, digits=4, zero_division=0)
    per_class = dict(zip(LABELS, f1_score(
        true_labels, pred_labels, labels=LABELS, average=None, zero_division=0
    )))
    return macro_f1, report, {k: float(v) for k, v in per_class.items()}


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}  |  LR: {LR}\n")

    client = get_client()
    print("Fetching rmp_reviews...")
    rows = fetch_all(
        client, "rmp_reviews",
        "ucinetid,review_text,overall_rating,difficulty_rating,would_take_again_pct,num_ratings",
    )
    df = pd.DataFrame(rows)
    print(f"  {len(df)} total rows")

    df = df[df["review_text"].notna() & (df["review_text"].str.len() >= 20)].copy()
    print(f"  {len(df)} rows after filtering short/null review_text")

    # Instructor→RMP name matching maps many ucinetids onto the same RateMyProfessor
    # record, so the same review blob (and the same ratings, hence the same label)
    # is duplicated across up to 80 rows.  A stratified split over the raw rows put
    # 56% of the test set's exact text in train, letting the model score by
    # memorization.  Collapse to distinct review texts so each RMP record is counted
    # once and no text can straddle a split boundary.
    before = len(df)
    df = df.drop_duplicates(subset=["review_text"], keep="first").copy()
    print(f"  {len(df)} rows after de-duplicating review_text "
          f"({before - len(df)} duplicate rows from RMP match collisions)\n")

    # ── Weak labeling ────────────────────────────────────────────────────────
    df["label"] = df.apply(assign_label, axis=1)

    print("Label distribution (including unclassified):")
    for label, count in df["label"].value_counts(dropna=False).items():
        print(f"  {str(label):<15} {count}")

    df = df[df["label"].notna()].copy()
    print(f"\n{len(df)} rows retained after dropping unclassified\n")

    print("Label distribution:")
    counts = df["label"].value_counts().reindex(LABELS)
    for label, count in counts.items():
        print(f"  {label:<15} {count}")

    # ── Stratified 80/10/10 split ────────────────────────────────────────────
    train_df, temp_df = train_test_split(
        df, test_size=0.20, stratify=df["label"], random_state=42
    )
    val_df, test_df = train_test_split(
        temp_df, test_size=0.50, stratify=temp_df["label"], random_state=42
    )
    print(f"\nSplit — train: {len(train_df)}, val: {len(val_df)}, test: {len(test_df)}")

    # No review text may appear in more than one split.  Guards the de-duplication
    # above: if a collision survives, the test score is memorization, not skill.
    splits = {"train": set(train_df["review_text"]),
              "val":   set(val_df["review_text"]),
              "test":  set(test_df["review_text"])}
    for a, b in (("train", "val"), ("train", "test"), ("val", "test")):
        overlap = splits[a] & splits[b]
        if overlap:
            raise AssertionError(
                f"{len(overlap)} review_text value(s) shared between {a} and {b} — "
                f"splits are contaminated, refusing to train."
            )
    print("No review_text overlap between splits — OK")

    # ── Oversample training set ──────────────────────────────────────────────
    n_train_records = len(train_df)   # pre-oversampling, for the model card
    train_df = oversample(train_df)
    print("\nLabel distribution after oversampling:")
    counts = train_df["label"].value_counts().reindex(LABELS)
    for label, count in counts.items():
        print(f"  {label:<15} {count}")

    train_ds = SentimentDataset(
        train_df["review_text"].tolist(), [LABEL2IDX[l] for l in train_df["label"]]
    )
    val_ds = SentimentDataset(
        val_df["review_text"].tolist(), [LABEL2IDX[l] for l in val_df["label"]]
    )
    test_ds = SentimentDataset(
        test_df["review_text"].tolist(), [LABEL2IDX[l] for l in test_df["label"]]
    )
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn)
    val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)
    test_loader = DataLoader(test_ds, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

    encoder = SentenceTransformer(MODEL_NAME, device=str(device))
    classifier = nn.Linear(EMBEDDING_DIM, len(LABELS)).to(device)

    optimizer = torch.optim.AdamW(
        list(encoder.parameters()) + list(classifier.parameters()), lr=LR
    )
    criterion = nn.CrossEntropyLoss()

    os.makedirs(SAVE_DIR, exist_ok=True)
    best_val_f1 = -1.0
    epochs_without_improvement = 0

    print()
    for epoch in range(1, EPOCHS + 1):
        encoder.train()
        classifier.train()
        total_loss = 0.0

        for texts, labels in train_loader:
            optimizer.zero_grad()
            embeddings = encode(encoder, texts, device)
            logits = classifier(embeddings)
            loss = criterion(logits, labels.to(device))
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        val_f1, report, _ = evaluate(encoder, classifier, val_loader, device)
        avg_loss = total_loss / len(train_loader)

        if val_f1 > best_val_f1:
            best_val_f1 = val_f1
            epochs_without_improvement = 0
            encoder.save(SAVE_DIR)
            torch.save(classifier.state_dict(), os.path.join(SAVE_DIR, "classifier.pt"))
            marker = "  ← best"
        else:
            epochs_without_improvement += 1
            marker = f"  (no improvement {epochs_without_improvement}/{PATIENCE})"

        print(f"Epoch {epoch:>2}/{EPOCHS}  train_loss={avg_loss:.4f}  val_macro_F1={val_f1:.4f}{marker}")

        if epoch % 5 == 0:
            print(report)

        if epochs_without_improvement >= PATIENCE:
            print(f"\nEarly stopping: val macro F1 has not improved for {PATIENCE} consecutive epochs.")
            break

    # ── Evaluate best model on test set ─────────────────────────────────────
    best_encoder = SentenceTransformer(SAVE_DIR, device=str(device))
    best_classifier = nn.Linear(EMBEDDING_DIM, len(LABELS)).to(device)
    best_classifier.load_state_dict(
        torch.load(os.path.join(SAVE_DIR, "classifier.pt"), map_location=device)
    )
    test_f1, test_report, test_per_class = evaluate(
        best_encoder, best_classifier, test_loader, device
    )
    print(f"\nTest set classification report:")
    print(test_report)
    print(f"Test macro F1: {test_f1:.4f}")

    with open(os.path.join(SAVE_DIR, "label_map.json"), "w") as f:
        json.dump({"labels": LABELS, "label2idx": LABEL2IDX}, f, indent=2)

    # Model card is written HERE, from the run that produced the checkpoint, so
    # every published number has this script as its producer.
    card = {
        "model_name": "sentiment_v1",
        "architecture": "all-MiniLM-L6-v2 encoder (fine-tuned) + nn.Linear(384, 4)",
        "produced_by": "ml/models/train_sentiment.py",
        "rmp_records": len(df),
        "split": {"train": n_train_records, "val": len(val_df), "test": len(test_df)},
        "train_rows_after_oversampling": len(train_df),
        "val_macro_f1": round(float(best_val_f1), 4),
        "test_macro_f1": round(float(test_f1), 4),
        "test_f1_per_class": {k: round(v, 4) for k, v in test_per_class.items()},
        "classes": LABELS,
        "weak_supervision_rules": {
            "teaches_well": "overall >= 4.0 AND difficulty >= 3.0 AND would_take_again_pct >= 75",
            "easy_grade":   "overall >= 3.8 AND difficulty <= 2.5 AND would_take_again_pct >= 70",
            "harsh_grader": "difficulty >= 3.8 AND would_take_again_pct <= 50",
            "avoid":        "overall <= 2.5 AND would_take_again_pct <= 40",
            "priority":     "avoid > harsh_grader > teaches_well > easy_grade",
        },
        "input_format": (
            "One string per professor: all RateMyProfessor comments for that "
            "instructor concatenated with ' | ' at scrape time "
            "(backend/scripts/fetch_rmp.py). Rows are de-duplicated on review_text "
            "before splitting, because instructor→RMP name matching maps multiple "
            "ucinetids onto the same RMP record."
        ),
        "output_format": (
            "softmax over [teaches_well, easy_grade, harsh_grader, avoid]; argmax → label"
        ),
        "known_limitations": (
            f"Test set is {len(test_df)} records — per-class F1 has wide confidence "
            "intervals. Labels are weak supervision over RMP numeric ratings, so the "
            "model's ceiling is recovering that rule from text; its value is labeling "
            "professors whose numeric ratings are missing."
        ),
        "created_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(SAVE_DIR, "model_card.json"), "w") as f:
        json.dump(card, f, indent=2)

    print(f"\nBest val macro F1: {best_val_f1:.4f}")
    print(f"Model saved to {SAVE_DIR}/")
    print(f"Model card written → {SAVE_DIR}/model_card.json")


if __name__ == "__main__":
    main()
