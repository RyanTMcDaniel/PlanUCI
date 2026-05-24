import json
import os

import pandas as pd
import torch
import torch.nn as nn
from sentence_transformers import SentenceTransformer
from torch.utils.data import DataLoader, Dataset

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "difficulty_nlp_v1")

BATCH_SIZE = 16
EPOCHS = 40
PATIENCE = 4
LR = 2e-5
MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class DifficultyDataset(Dataset):
    def __init__(self, texts: list[str], scores: list[float]):
        self.texts = texts
        self.scores = torch.tensor(scores, dtype=torch.float32)

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        return self.texts[idx], self.scores[idx]


def collate_fn(batch):
    texts, scores = zip(*batch)
    return list(texts), torch.stack(scores)


def encode(encoder: SentenceTransformer, texts: list[str], device: torch.device) -> torch.Tensor:
    features = encoder.preprocess(texts)
    features = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in features.items()}
    return encoder(features)["sentence_embedding"]


def evaluate(encoder, regressor, loader, device) -> float:
    encoder.eval()
    regressor.eval()
    total_mae, n = 0.0, 0
    with torch.no_grad():
        for texts, scores in loader:
            embeddings = encode(encoder, texts, device)
            preds = regressor(embeddings).squeeze(-1)
            total_mae += torch.abs(preds - scores.to(device)).sum().item()
            n += len(scores)
    return total_mae / n


def main() -> None:
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}")
    print(f"Learning rate: {LR}")

    train_df = pd.read_csv(os.path.join(DATA_DIR, "train.csv"))
    val_df = pd.read_csv(os.path.join(DATA_DIR, "val.csv"))
    print(f"Train: {len(train_df)} courses  |  Val: {len(val_df)} courses")

    p20 = float(train_df["difficulty_score"].quantile(0.20))
    p80 = float(train_df["difficulty_score"].quantile(0.80))
    print(f"Tier cutoffs — easy/medium: {p20:.4f}  medium/hard: {p80:.4f}")

    # Oversample hard and easy tiers to match medium count
    train_df["tier"] = pd.cut(
        train_df["difficulty_score"],
        bins=[-0.001, p20, p80, 10.001],
        labels=["easy", "medium", "hard"],
        right=True,
    )
    target_count = train_df["tier"].value_counts()["medium"]
    oversampled = [train_df]
    for tier in ["easy", "hard"]:
        tier_df = train_df[train_df["tier"] == tier]
        repeats = (target_count // len(tier_df)) - 1
        remainder = target_count % len(tier_df)
        oversampled.append(pd.concat([tier_df] * repeats, ignore_index=True))
        oversampled.append(tier_df.sample(n=remainder, random_state=42, replace=False))
    train_df = pd.concat(oversampled, ignore_index=True).sample(frac=1, random_state=42).reset_index(drop=True)

    print("\nTier distribution after oversampling:")
    counts = train_df["tier"].value_counts().reindex(["easy", "medium", "hard"])
    for tier, count in counts.items():
        print(f"  {tier:<8} {count}")

    def format_text(df):
        return (
            "Department: " + df["department"] + ". "
            "Course: " + df["course_id"] + ". "
            + df["description"]
        ).tolist()

    train_ds = DifficultyDataset(format_text(train_df), train_df["difficulty_score"].tolist())
    val_ds = DifficultyDataset(format_text(val_df), val_df["difficulty_score"].tolist())
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn)
    val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

    encoder = SentenceTransformer(MODEL_NAME, device=str(device))
    regressor = nn.Linear(EMBEDDING_DIM, 1).to(device)

    optimizer = torch.optim.AdamW(
        list(encoder.parameters()) + list(regressor.parameters()), lr=LR
    )
    criterion = nn.MSELoss()

    os.makedirs(SAVE_DIR, exist_ok=True)
    best_val_mae = float("inf")
    epochs_without_improvement = 0

    for epoch in range(1, EPOCHS + 1):
        encoder.train()
        regressor.train()
        total_loss = 0.0

        for texts, scores in train_loader:
            optimizer.zero_grad()
            embeddings = encode(encoder, texts, device)
            preds = regressor(embeddings).squeeze(-1)
            loss = criterion(preds, scores.to(device))
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        val_mae = evaluate(encoder, regressor, val_loader, device)
        avg_loss = total_loss / len(train_loader)
        marker = ""
        if val_mae < best_val_mae:
            best_val_mae = val_mae
            epochs_without_improvement = 0
            encoder.save(SAVE_DIR)
            torch.save(regressor.state_dict(), os.path.join(SAVE_DIR, "regressor.pt"))
            marker = "  ← best"
        else:
            epochs_without_improvement += 1
            marker = f"  (no improvement {epochs_without_improvement}/{PATIENCE})"

        print(f"Epoch {epoch:>2}/{EPOCHS}  train_loss={avg_loss:.4f}  val_MAE={val_mae:.4f}{marker}")

        if epochs_without_improvement >= PATIENCE:
            print(f"\nEarly stopping: val MAE has not improved for {PATIENCE} consecutive epochs.")
            break

    with open(os.path.join(SAVE_DIR, "tier_cutoffs.json"), "w") as f:
        json.dump({"p20": p20, "p80": p80}, f, indent=2)

    print(f"\nBest val MAE: {best_val_mae:.4f}")
    print(f"Model saved to {SAVE_DIR}/")


if __name__ == "__main__":
    main()
