"""
Load test for the PlanUCI optimizer — POST /optimizer/generate (the expensive path).

Two scenarios, selected with the LOCUST_SCENARIO env var (default: cold).  They are
NEVER blended into one headline number — run them separately with separate --csv
prefixes.

  LOCUST_SCENARIO=cold   Every request has a fresh uuid nonce in ap_scores and a
                         random major_id from a validated pool → guaranteed L1+L2
                         cache MISS → the real optimizer solve runs.  This is the
                         scenario that stresses the known bottlenecks
                         (deepcopy hill-climb, per-call CSV re-read, per-call
                         Supabase client).

  LOCUST_SCENARIO=warm   One fixed payload, identical every request → L1 cache HIT
                         after the first.  Measures cached throughput, not the solve.

SAFETY
------
  Point this ONLY at a backend started with LOAD_TEST_MODE=1 so the schedules_saved
  counter and optimizer_cache L2 table are not polluted.  The locustfile refuses to
  start unless it can confirm the target is reachable; it cannot confirm
  LOAD_TEST_MODE remotely, so that remains the operator's responsibility (a banner
  prints in the server log on startup).

SLOW vs CRASHED
---------------
  A request counts as a FAILURE (CRASHED signal) when it times out, the connection
  is refused/reset, the status is not 200, or the body has no `variants`.  These
  land in *_failures.csv / *_exceptions.csv grouped by cause — connection-pool
  exhaustion from the per-call Supabase client shows up here, distinct from mere
  latency growth (SLOW), which shows up as rising p95 in *_stats.csv.

Usage: see the commands printed by `python loadtest/print_commands.py`.
"""

import os
import uuid

import gevent
from locust import HttpUser, task, constant, events
from locust.runners import STATE_STOPPING, STATE_STOPPED

# Import the validated cold-scenario pool.  Works whether locust is invoked from
# backend/ or backend/loadtest/.
try:
    from loadtest.major_pool import MAJOR_POOL
except ModuleNotFoundError:  # invoked from inside backend/loadtest/
    from major_pool import MAJOR_POOL

SCENARIO = os.getenv("LOCUST_SCENARIO", "cold").strip().lower()
ENDPOINT = "/optimizer/generate"

# Per-request client-side timeout (seconds).  A cold solve is ~5 s single-user;
# under load it grows.  We give generous headroom so we measure real latency, but
# cap it so a hung/queued request becomes a CRASHED failure instead of blocking a
# worker forever.
REQUEST_TIMEOUT = float(os.getenv("LOADTEST_TIMEOUT", "60"))

_BASE_BODY = {
    "completed_courses":  ["I&CSCI31", "I&CSCI32", "I&CSCI33", "MATH2A"],
    "graduation_quarter": "2029_spring",
    "units_per_quarter":  16,
    "waived_ges":         [],
    "start_quarter":      "2026_fall",
    "seed_courses":       [],
    "seed_only":          False,
}

# Fixed payload for the WARM scenario — identical every request so it hits L1.
_WARM_BODY = {**_BASE_BODY, "major_id": "BS-201G", "ap_scores": {"_warm_fixed": 0}}


def _post(client, body, name):
    """POST with catch_response; classify SLOW (latency, still 200) vs CRASHED
    (exception / non-200 / empty body) so the two findings stay distinct."""
    try:
        with client.post(
            ENDPOINT, json=body, name=name, timeout=REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                variants = resp.json().get("variants", [])
            except Exception:
                resp.failure("bad-json")
                return
            if not variants:
                resp.failure("empty-variants")
                return
            resp.success()
    except Exception as exc:  # connection refused/reset, read timeout, pool exhaustion
        # Surfaced in *_exceptions.csv by type — the CRASHED signal.
        raise exc


class ColdUser(HttpUser):
    """COLD: fresh nonce + random real major → real solve every request."""
    wait_time = constant(0)

    @task
    def generate_cold(self):
        import random
        body = {
            **_BASE_BODY,
            "major_id":  random.choice(MAJOR_POOL),
            # uuid nonce → unique cache key → guaranteed miss → real optimizer run
            "ap_scores": {f"_cold_{uuid.uuid4().hex}": 0},
        }
        _post(self.client, body, name="POST /generate [cold]")


class WarmUser(HttpUser):
    """WARM: one fixed payload → L1 cache hit after the first request."""
    wait_time = constant(0)

    @task
    def generate_warm(self):
        _post(self.client, dict(_WARM_BODY), name="POST /generate [warm]")


# Select which user class is active via LOCUST_SCENARIO.  Locust picks up every
# HttpUser subclass in the module by default, so we disable the one not selected by
# giving it weight 0 (locust honors the `weight` attribute; 0 → never spawned).
if SCENARIO == "warm":
    ColdUser.weight = 0
    WarmUser.weight = 1
else:
    ColdUser.weight = 1
    WarmUser.weight = 0


# ── Abort-early guard ─────────────────────────────────────────────────────────
# Because the baseline runs against reads on the PRODUCTION Supabase, a run that
# starts failing (e.g. connection-pool exhaustion at 100 users) should stop
# hammering rather than push the live DB harder.  Once we have a minimum sample,
# if the running failure ratio crosses the threshold we quit the run early.  The
# partial *_stats.csv / *_failures.csv still capture the CRASHED finding.

_ABORT_MIN   = int(os.getenv("LOADTEST_ABORT_MIN", "40"))       # min requests before arming
_ABORT_RATIO = float(os.getenv("LOADTEST_ABORT_RATIO", "0.5"))  # fail-ratio that triggers abort
_counts = {"ok": 0, "fail": 0}
_runner_ref = {}


@events.init.add_listener
def _capture_runner(environment, **_kw):
    _runner_ref["r"] = environment.runner


@events.request.add_listener
def _watch_failures(request_type, name, response_time, response_length,
                    exception, context, **_kw):
    if exception is not None:
        _counts["fail"] += 1
    else:
        _counts["ok"] += 1
    total = _counts["ok"] + _counts["fail"]
    if total >= _ABORT_MIN and (_counts["fail"] / total) >= _ABORT_RATIO:
        runner = _runner_ref.get("r")
        if runner is not None and runner.state not in (STATE_STOPPING, STATE_STOPPED):
            print(f"[ABORT] failure ratio {_counts['fail']}/{total} "
                  f"≥ {_ABORT_RATIO:.0%} — stopping early to protect the prod DB.")
            gevent.spawn_later(0, runner.quit)
