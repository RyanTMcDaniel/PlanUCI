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
import threading
from datetime import datetime, timedelta, timezone

from cachetools import TTLCache
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV)

_TABLE   = "optimizer_cache"
_TTL     = timedelta(days=7)


# ── L1: in-process memory cache ───────────────────────────────────────────────
# Sits in front of the Supabase L2 cache. Same key space; shorter TTL (1 h)
# since the process may restart between requests.  Thread-safe via RLock.

_L1_TTL = 3600  # seconds — evict after 1 hour even if the process stays up
_L1_MAX = 256   # max entries before LRU eviction kicks in

_l1: TTLCache = TTLCache(maxsize=_L1_MAX, ttl=_L1_TTL)
_l1_lock = threading.RLock()


def get_l1(cache_key: str) -> dict | None:
    """Return the in-memory cached result, or None on miss."""
    with _l1_lock:
        return _l1.get(cache_key)


def set_l1(cache_key: str, result: dict) -> None:
    """Store result in the in-memory cache."""
    with _l1_lock:
        _l1[cache_key] = result


def invalidate_l1(cache_key: str | None = None) -> None:
    """Evict one entry (or everything) from the in-memory cache.

    Call this whenever the underlying course/requirement data changes so the
    next request re-runs the optimizer instead of returning a stale snapshot.
    Passing None clears the entire L1 cache.
    """
    with _l1_lock:
        if cache_key is None:
            _l1.clear()
        else:
            _l1.pop(cache_key, None)

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

