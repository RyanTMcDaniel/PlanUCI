"""
Optimizer result cache backed by Supabase.

Table (run once in Supabase SQL editor before using):

    CREATE TABLE IF NOT EXISTS optimizer_cache (
        cache_key   TEXT PRIMARY KEY,
        result_json JSONB NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        hit_count   INTEGER NOT NULL DEFAULT 0
    );

Cache key is a SHA-256 hash of the generate() inputs so identical requests
always hit the same row regardless of call order.

TTL: 7 days — stale entries are deleted on read and regenerated on next call.

All public functions are intentionally non-fatal: a cache miss (including any
Supabase error) returns None so callers always fall back to computing the result.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

_TABLE   = "optimizer_cache"
_TTL     = timedelta(days=7)

MIGRATION_SQL = """\
-- Run once in the Supabase SQL editor:
CREATE TABLE IF NOT EXISTS optimizer_cache (
    cache_key   TEXT PRIMARY KEY,
    result_json JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count   INTEGER NOT NULL DEFAULT 0
);
"""


# ── Key construction ──────────────────────────────────────────────────────────

def make_key(
    major_id:           str,
    completed_courses:  list[str],
    graduation_quarter: str,
    units_per_quarter:  int,
    waived_ges:         list[str] | None = None,
    ap_scores:          dict[str, int] | None = None,
    start_quarter:      str | None = None,
) -> str:
    """Return a deterministic SHA-256 hex key for the given generate() inputs."""
    payload = json.dumps(
        {
            "major_id":           major_id,
            "completed_courses":  sorted(completed_courses),
            "graduation_quarter": graduation_quarter,
            "units_per_quarter":  units_per_quarter,
            "waived_ges":         sorted(waived_ges or []),
            "ap_scores":          dict(sorted((ap_scores or {}).items())),
            "start_quarter":      start_quarter or "",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Read ──────────────────────────────────────────────────────────────────────

def get(client, cache_key: str) -> dict | None:
    """Return cached result dict or None on miss / error / TTL expiry.

    Stale entries (older than TTL) are deleted synchronously so the table
    stays clean without a separate cleanup job.
    """
    try:
        rows = (
            client.table(_TABLE)
            .select("result_json,created_at,hit_count")
            .eq("cache_key", cache_key)
            .execute()
            .data
        )
        if not rows:
            return None

        entry      = rows[0]
        created_at = datetime.fromisoformat(entry["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) - created_at > _TTL:
            # Stale — evict and treat as miss
            client.table(_TABLE).delete().eq("cache_key", cache_key).execute()
            return None

        # Increment hit counter (non-fatal if it fails)
        try:
            client.table(_TABLE).update(
                {"hit_count": entry["hit_count"] + 1}
            ).eq("cache_key", cache_key).execute()
        except Exception:
            pass

        return entry["result_json"]

    except Exception:
        return None


# ── Write ─────────────────────────────────────────────────────────────────────

def set(client, cache_key: str, result: dict) -> None:
    """Upsert result into the cache.  Silently swallows errors."""
    try:
        client.table(_TABLE).upsert(
            {
                "cache_key":   cache_key,
                "result_json": result,
                "created_at":  datetime.now(timezone.utc).isoformat(),
                "hit_count":   0,
            },
            on_conflict="cache_key",
        ).execute()
    except Exception:
        pass

