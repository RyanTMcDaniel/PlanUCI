import json
import os

import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, f1_score
from torch.utils.data import DataLoader, Dataset

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "difficulty_nlp_v2")

BATCH_SIZE = 16
EPOCHS = 40
PATIENCE = 4
LR = 2e-5
MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

LABELS = ["easy", "medium", "hard"]
LABEL2IDX = {l: i for i, l in enumerate(LABELS)}


class TierDataset(Dataset):
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


def format_text(df: pd.DataFrame) -> list[str]:
    title = df["title"].fillna(df["course_id"])
    return (title + ": " + df["description"]).tolist()


def encode(encoder: SentenceTransformer, texts: list[str], device: torch.device) -> torch.Tensor:
    features = encoder.preprocess(texts)
    features = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in features.items()}
    return encoder(features)["sentence_embedding"]


def evaluate(encoder, classifier, loader, device) -> tuple[float, dict]:
    encoder.eval()
    classifier.eval()
    all_preds, all_true = [], []
    with torch.no_grad():
        for texts, labels in loader:
            embeddings = encode(encoder, texts, device)
            logits = classifier(embeddings)
            preds = logits.argmax(dim=-1).cpu().tolist()
            all_preds.extend(preds)
            all_true.extend(labels.tolist())

    pred_labels = [LABELS[p] for p in all_preds]
    true_labels = [LABELS[t] for t in all_true]
    macro_f1 = f1_score(true_labels, pred_labels, labels=LABELS, average="macro", zero_division=0)
    report = classification_report(true_labels, pred_labels, labels=LABELS, digits=4, zero_division=0)
    return macro_f1, report


def oversample(df: pd.DataFrame) -> pd.DataFrame:
    target = df["difficulty_tier"].value_counts()["medium"]
    parts = [df]
    for tier in ["easy", "hard"]:
        tier_df = df[df["difficulty_tier"] == tier]
        repeats = (target // len(tier_df)) - 1
        remainder = target % len(tier_df)
        parts.append(pd.concat([tier_df] * repeats, ignore_index=True))
        parts.append(tier_df.sample(n=remainder, random_state=42, replace=False))
    return pd.concat(parts, ignore_index=True).sample(frac=1, random_state=42).reset_index(drop=True)


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}  |  LR: {LR}")

    train_df = pd.read_csv(os.path.join(DATA_DIR, "train.csv"))
    val_df = pd.read_csv(os.path.join(DATA_DIR, "val.csv"))
    print(f"Train: {len(train_df)} courses  |  Val: {len(val_df)} courses")

    train_df = oversample(train_df)
    print("\nTier distribution after oversampling:")
    counts = train_df["difficulty_tier"].value_counts().reindex(LABELS)
    for tier, count in counts.items():
        print(f"  {tier:<8} {count}")

    train_ds = TierDataset(format_text(train_df), [LABEL2IDX[t] for t in train_df["difficulty_tier"]])
    val_ds = TierDataset(format_text(val_df), [LABEL2IDX[t] for t in val_df["difficulty_tier"]])
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn)
    val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

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

        val_f1, report = evaluate(encoder, classifier, val_loader, device)
        avg_loss = total_loss / len(train_loader)
        marker = ""
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

    with open(os.path.join(SAVE_DIR, "label_map.json"), "w") as f:
        json.dump({"labels": LABELS, "label2idx": LABEL2IDX}, f, indent=2)

    print(f"\nBest val macro F1: {best_val_f1:.4f}")
    print(f"Model saved to {SAVE_DIR}/")


if __name__ == "__main__":
    main()
