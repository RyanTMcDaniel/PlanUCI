import json
import os
from contextlib import asynccontextmanager

import pandas as pd
import torch
import torch.nn as nn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentence_transformers import SentenceTransformer
from supabase import create_client

from .state import ml

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_ENV)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_DIR = os.path.join(_APP_DIR, "..", "..", "ml")

DIFFICULTY_MODEL_DIR = os.path.join(_ML_DIR, "models", "difficulty_nlp_v2")
SENTIMENT_MODEL_DIR = os.path.join(_ML_DIR, "models", "sentiment_v1")
PROF_FEATURES_CSV = os.path.join(_ML_DIR, "data", "prof_course_features.csv")
EMBEDDING_DIM = 384


def _fetch_all(client, table: str, columns: str) -> list[dict]:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    ml["device"] = device

    # ── Difficulty NLP model ─────────────────────────────────────────────────
    with open(os.path.join(DIFFICULTY_MODEL_DIR, "label_map.json")) as f:
        diff_map = json.load(f)
    ml["diff_labels"] = diff_map["labels"]
    ml["diff_idx2label"] = {i: l for l, i in diff_map["label2idx"].items()}

    diff_enc = SentenceTransformer(DIFFICULTY_MODEL_DIR, device=str(device))
    diff_clf = nn.Linear(EMBEDDING_DIM, len(ml["diff_labels"])).to(device)
    diff_clf.load_state_dict(
        torch.load(os.path.join(DIFFICULTY_MODEL_DIR, "classifier.pt"), map_location=device)
    )
    diff_enc.eval()
    diff_clf.eval()
    ml["diff_encoder"] = diff_enc
    ml["diff_classifier"] = diff_clf

    # ── Sentiment model ──────────────────────────────────────────────────────
    with open(os.path.join(SENTIMENT_MODEL_DIR, "label_map.json")) as f:
        sent_map = json.load(f)
    ml["sent_labels"] = sent_map["labels"]
    ml["sent_idx2label"] = {i: l for l, i in sent_map["label2idx"].items()}

    sent_enc = SentenceTransformer(SENTIMENT_MODEL_DIR, device=str(device))
    sent_clf = nn.Linear(EMBEDDING_DIM, len(ml["sent_labels"])).to(device)
    sent_clf.load_state_dict(
        torch.load(os.path.join(SENTIMENT_MODEL_DIR, "classifier.pt"), map_location=device)
    )
    sent_enc.eval()
    sent_clf.eval()
    ml["sent_encoder"] = sent_enc
    ml["sent_classifier"] = sent_clf

    # ── Static data ──────────────────────────────────────────────────────────
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    courses_raw = _fetch_all(client, "courses", "id,title,description,department")
    ml["courses"] = {r["id"]: r for r in courses_raw}

    prof_df = pd.read_csv(PROF_FEATURES_CSV)
    ml["prof_features"] = prof_df

    reviews_raw = _fetch_all(client, "rmp_reviews", "ucinetid,review_text")
    ml["reviews"] = {
        r["ucinetid"]: r["review_text"]
        for r in reviews_raw
        if r.get("review_text") and len(r["review_text"]) >= 20
    }

    print(f"Startup complete — device: {device} | "
          f"courses: {len(ml['courses'])} | "
          f"prof pairs: {len(prof_df)} | "
          f"reviews: {len(ml['reviews'])}")
    yield
    ml.clear()


app = FastAPI(title="PlanUCI ML API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://plan-uci-three.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .routers import difficulty, sentiment, optimizer  # noqa: E402
app.include_router(difficulty.router, prefix="/difficulty", tags=["difficulty"])
app.include_router(sentiment.router, prefix="/sentiment", tags=["sentiment"])
app.include_router(optimizer.router, prefix="/optimizer", tags=["optimizer"])


@app.get("/health")
def health():
    return {"status": "ok", "models_loaded": len(ml) > 0}
