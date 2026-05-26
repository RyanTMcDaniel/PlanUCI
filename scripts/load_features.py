"""
Load course_features and prof_course_features CSVs into Supabase.
Run AFTER executing supabase_setup.sql in the Supabase SQL editor.

Usage (from repo root):
  cd /Users/ryanmcdaniel/PlanUCI
  source backend/venv/bin/activate
  python3 scripts/load_features.py
"""
import os, sys
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

load_dotenv("backend/.env")
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_KEY")
if not url or not key:
    sys.exit("SUPABASE_URL / SUPABASE_SERVICE_KEY not set in backend/.env")

client = create_client(url, key)

def nan_to_none(rows: list[dict]) -> list[dict]:
    """Replace all float NaN values with None for JSON-safe serialization."""
    import math
    return [
        {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in row.items()}
        for row in rows
    ]

def upsert_batch(table: str, rows: list[dict], batch: int = 500) -> int:
    total = 0
    for i in range(0, len(rows), batch):
        client.table(table).upsert(rows[i : i + batch]).execute()
        total = min(i + batch, len(rows))
        print(f"  {table}: {total}/{len(rows)}")
    return total

# ── course_features ────────────────────────────────────────────────────────
print("Loading course_features …")
df_cf = (
    pd.read_csv("ml/data/course_features.csv")
    .dropna(subset=["difficulty_score"])[["course_id", "difficulty_score"]]
)
rows_cf = nan_to_none(df_cf.to_dict("records"))
inserted_cf = upsert_batch("course_features", rows_cf)
print(f"  Done — {inserted_cf} rows successfully inserted\n")

# ── prof_course_features ────────────────────────────────────────────────────
print("Loading prof_course_features …")
df_pcf = pd.read_csv("ml/data/prof_course_features.csv")
keep = [c for c in ["course_id", "instructor_id", "nlp_score", "gpa_score",
                     "rmp_score", "difficulty_score", "sections_taught"]
        if c in df_pcf.columns]
df_pcf = df_pcf[keep].dropna(subset=["difficulty_score"])
rows_pcf = nan_to_none(df_pcf.to_dict("records"))
inserted_pcf = upsert_batch("prof_course_features", rows_pcf)
print(f"  Done — {inserted_pcf} rows successfully inserted\n")

print("All done.")
