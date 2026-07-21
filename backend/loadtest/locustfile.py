"""
Load test for the PlanUCI optimizer — POST /optimizer/generate and /optimizer/whatif.

Scenarios are selected with the LOCUST_SCENARIO env var (default: cold).  They are
NEVER blended into one headline number — run them separately with separate --csv
prefixes.

GENERATE — /optimizer/generate.  Note this endpoint has no frontend caller (its
last one was removed in 1d996b5); it is kept for the historical baseline.

  LOCUST_SCENARIO=cold   Every request has a fresh uuid nonce in ap_scores and a
                         random major_id from a validated pool → guaranteed L1+L2
                         cache MISS → the real optimizer solve runs.  This is the
                         scenario that stresses the known bottlenecks
                         (deepcopy hill-climb, per-call CSV re-read, per-call
                         Supabase client).

  LOCUST_SCENARIO=warm   One fixed payload, identical every request → L1 cache HIT
                         after the first.  Measures cached throughput, not the solve.

WHATIF — /optimizer/whatif.  This is the endpoint the UI actually calls, from both
the "Optimize Schedule" button (PlannerClient.tsx:4120) and the GE/minor/auto-fill
path (buildAndOptimizePool, PlannerClient.tsx:3319).  The two shapes are tested
separately because they cache very differently:

  LOCUST_SCENARIO=whatif-autofill-cold   Zero-lock payload (the buildAndOptimizePool
                                         shape) + uuid nonce → guaranteed miss.
  LOCUST_SCENARIO=whatif-autofill-warm   One fixed zero-lock payload → L1+L2 hit.
                                         This is the shape with real cross-user
                                         reuse: two users with the same major /
                                         picks / years / cap send identical bytes.

  LOCUST_SCENARIO=whatif-locked-cold     Lock-bearing, lock sets varied per
                                         simulated user → miss.
  LOCUST_SCENARIO=whatif-locked-warm     One fixed lock-bearing payload.

  !! whatif-locked-warm is EXPECTED not to speed up for status="ok" responses.
  The admission policy in optimizer_whatif deliberately does not cache
  lock-bearing successes (each click's input is the previous click's output, so
  reuse is near zero while the payload is the largest of the three shapes).  Only
  a lock-bearing *infeasible* response is admitted, to L1 only.  Comparing
  whatif-locked-warm against whatif-autofill-warm is what validates that choice.

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
import sys
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
ENDPOINT = "/optimizer/generate"          # kept: existing name used by the generate path
ENDPOINT_WHATIF = "/optimizer/whatif"

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


# whatif payload construction lives in whatif_payload.py (no locust import,
# so validate_whatif_pool.py can reuse it alongside supabase/ssl).
try:
    from loadtest.whatif_payload import (
        _DEFAULT_CAP, _UNITS_PER_COURSE, _grad_quarter, _lock_set,
        _quarter_list, _whatif_body,
    )
except ModuleNotFoundError:  # invoked from inside backend/loadtest/
    from whatif_payload import (
        _DEFAULT_CAP, _UNITS_PER_COURSE, _grad_quarter, _lock_set,
        _quarter_list, _whatif_body,
    )


# Course sets for the whatif grids — a committed fixture, same pattern as
# MAJOR_POOL.  Real catalogue ids matter: an unknown id has no prereq tree and no
# meta, which would make the solve unrepresentatively cheap.  Regenerate with
# loadtest/validate_whatif_pool.py, which sources courses straight from Supabase
# and probes /optimizer/whatif — it never touches /optimizer/generate.
#
# The empty fallback exists only so validate_whatif_pool.py can import this module
# to build the very fixture it is about to write.
try:
    from loadtest.whatif_pool import WHATIF_POOL
except ModuleNotFoundError:
    try:
        from whatif_pool import WHATIF_POOL          # invoked from backend/loadtest/
    except ModuleNotFoundError:
        WHATIF_POOL = {}                             # fixture not generated yet


def _validate_generate(payload):
    """None if the generate body is a real result, else a failure reason."""
    return None if payload.get("variants") else "empty-variants"


def _validate_whatif(payload):
    """None if the whatif body is a real result, else a failure reason.

    whatif returns {"status":"ok","plans":[...]} or
    {"status":"infeasible","conflicts":[...]} — both are legitimate 200s and
    neither is an error.  Infeasible is counted as a success on purpose: it is
    the most expensive path in optimize_around_locks (it returns only after
    exhausting every unit_cap_tier x all 5 seed configs), so dropping it would
    hide the worst-case latency this test exists to measure.
    """
    status = payload.get("status")
    if status == "ok":
        return None if payload.get("plans") else "ok-without-plans"
    if status == "infeasible":
        return None
    return f"bad-status:{status!r}"


def _post(client, body, name, endpoint=ENDPOINT, validate=_validate_generate):
    """POST with catch_response; classify SLOW (latency, still 200) vs CRASHED
    (exception / non-200 / empty body) so the two findings stay distinct."""
    try:
        with client.post(
            endpoint, json=body, name=name, timeout=REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                payload = resp.json()
            except Exception:
                resp.failure("bad-json")
                return
            reason = validate(payload)
            if reason:
                resp.failure(reason)
                return
            resp.success()
    except Exception as exc:  # connection refused/reset, read timeout, pool exhaustion
        # Surfaced in *_exceptions.csv by type — the CRASHED signal.
        raise exc


@events.test_start.add_listener
def _check_whatif_pool(environment, **_kw):
    """Fail fast rather than load-testing an empty grid."""
    if not SCENARIO.startswith("whatif"):
        return
    if not WHATIF_POOL:
        raise RuntimeError(
            "WHATIF_POOL is empty — generate the fixture first:\n"
            "  venv/bin/python loadtest/validate_whatif_pool.py"
        )
    for m, cs in sorted(WHATIF_POOL.items()):
        print(f"[pool] {m}: {len(cs)} courses")


def _pick_major():
    import random
    return random.choice(list(WHATIF_POOL))


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


# ── whatif users ──────────────────────────────────────────────────────────────

class WhatifAutofillColdUser(HttpUser):
    """Zero-lock (autofill) shape, unique per request → guaranteed cache miss."""
    wait_time = constant(0)

    @task
    def whatif_autofill_cold(self):
        major = _pick_major()
        body = _whatif_body(
            major, WHATIF_POOL[major],
            locked=None,
            # ap_scores is a keyed input, so a nonce forces a miss.  An unknown
            # exam name matches no ap_credits row, so it shifts the key without
            # changing the resulting plan — same trick as the generate cold path.
            ap_scores={f"_cold_{uuid.uuid4().hex}": 0},
        )
        _post(self.client, body, name="POST /whatif [autofill cold]",
              endpoint=ENDPOINT_WHATIF, validate=_validate_whatif)


class WhatifAutofillWarmUser(HttpUser):
    """Zero-lock shape, one fixed payload → L1+L2 hit after the first request."""
    wait_time = constant(0)

    @task
    def whatif_autofill_warm(self):
        major = sorted(WHATIF_POOL)[0]
        body = _whatif_body(
            major, WHATIF_POOL[major],
            locked=None, ap_scores={"_warm_fixed": 0},
        )
        _post(self.client, body, name="POST /whatif [autofill warm]",
              endpoint=ENDPOINT_WHATIF, validate=_validate_whatif)


class WhatifLockedColdUser(HttpUser):
    """Lock-bearing, lock sets varied per simulated user → guaranteed miss."""
    wait_time = constant(0)

    def on_start(self):
        # Stable per-user identity so this user's lock configuration is its own,
        # the way a real student's pinned courses are.
        import random
        self._n_locks = random.randint(1, 5)
        self._offset  = random.randrange(64)

    @task
    def whatif_locked_cold(self):
        major = _pick_major()
        grid  = WHATIF_POOL[major]
        body = _whatif_body(
            major, grid,
            locked=_lock_set(grid, self._n_locks, self._offset),
            ap_scores={f"_cold_{uuid.uuid4().hex}": 0},
        )
        _post(self.client, body, name="POST /whatif [locked cold]",
              endpoint=ENDPOINT_WHATIF, validate=_validate_whatif)


class WhatifLockedWarmUser(HttpUser):
    """Lock-bearing, one fixed payload.

    Expected NOT to accelerate for status="ok": the admission policy does not
    cache lock-bearing successes.  Its delta vs [autofill warm] is the evidence
    for that decision, so it is measured rather than assumed.
    """
    wait_time = constant(0)

    @task
    def whatif_locked_warm(self):
        major = sorted(WHATIF_POOL)[0]
        grid  = WHATIF_POOL[major]
        body = _whatif_body(
            major, grid,
            locked=_lock_set(grid, 3, 0),
            ap_scores={"_warm_fixed": 0},
        )
        _post(self.client, body, name="POST /whatif [locked warm]",
              endpoint=ENDPOINT_WHATIF, validate=_validate_whatif)


# Select which user class is active via LOCUST_SCENARIO.  Locust picks up every
# HttpUser subclass in the module by default, so we disable the ones not selected by
# giving them weight 0 (locust honors the `weight` attribute; 0 → never spawned).
# Exactly one shape+mode runs per invocation — shapes are never blended, so each
# gets its own clean latency distribution and its own --csv prefix.
_SCENARIOS = {
    "cold":                  ColdUser,
    "warm":                  WarmUser,
    "whatif-autofill-cold":  WhatifAutofillColdUser,
    "whatif-autofill-warm":  WhatifAutofillWarmUser,
    "whatif-locked-cold":    WhatifLockedColdUser,
    "whatif-locked-warm":    WhatifLockedWarmUser,
}

if SCENARIO not in _SCENARIOS:
    raise ValueError(
        f"unknown LOCUST_SCENARIO {SCENARIO!r} — choose one of: "
        + ", ".join(sorted(_SCENARIOS))
    )

for _name, _cls in _SCENARIOS.items():
    _cls.weight = 1 if _name == SCENARIO else 0


# ── Per-shape latency report ──────────────────────────────────────────────────
# Locust's default CSV percentiles already include 50/95/99, so the schema is left
# alone (the committed baseline in results/ stays diffable).  This prints an
# explicit p50/p95/p99 + throughput table per request name at end of run.

@events.test_stop.add_listener
def _print_percentiles(environment, **_kw):
    entries = [
        e for e in environment.stats.entries.values() if e.num_requests
    ]
    if not entries:
        return
    print()
    print(f"─── {SCENARIO} ─── latency by shape ───")
    print("%-34s %8s %8s %9s %9s %9s %8s" %
          ("name", "reqs", "fails", "p50 ms", "p95 ms", "p99 ms", "req/s"))
    for e in sorted(entries, key=lambda x: x.name):
        print("%-34s %8d %8d %9.0f %9.0f %9.0f %8.2f" % (
            e.name, e.num_requests, e.num_failures,
            e.get_response_time_percentile(0.50),
            e.get_response_time_percentile(0.95),
            e.get_response_time_percentile(0.99),
            e.total_rps,
        ))
    print()


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


# ── Payload preview ───────────────────────────────────────────────────────────
# `python loadtest/locustfile.py` prints one sample payload per whatif shape and
# exits.  Sends no traffic — for eyeballing the wire format against
# PlannerClient.tsx before committing to a run.

if __name__ == "__main__":
    import json

    if not WHATIF_POOL:
        sys.exit("WHATIF_POOL is empty — run loadtest/validate_whatif_pool.py first")
    major = sorted(WHATIF_POOL)[0]
    grid  = WHATIF_POOL[major]
    n_courses = sum(len(v) for v in grid.values())

    print(f"pool: {len(WHATIF_POOL)} majors -> "
          + ", ".join(f"{m}={sum(len(v) for v in g.values())}"
                      for m, g in sorted(WHATIF_POOL.items())))
    print(f"preview major: {major}  ({n_courses} courses, {len(grid)} quarters)")
    print(f"grad_quarter: {_grad_quarter()}  ->  graduation_year: "
          f"{int(_grad_quarter().split('_')[0])}")

    print("\n=== [autofill] zero-lock — buildAndOptimizePool shape (:3319) ===")
    print(json.dumps(
        _whatif_body(major, grid, locked=None, ap_scores={"_warm_fixed": 0}),
        indent=2,
    ))

    print("\n=== [locked] lock-bearing — Optimize Schedule button shape (:4120) ===")
    print(json.dumps(
        _whatif_body(major, grid, locked=_lock_set(grid, 3, 0),
                     ap_scores={"_warm_fixed": 0}),
        indent=2,
    ))

    print("\n=== lock-set variation across simulated users ([locked cold]) ===")
    print("each WhatifLockedColdUser draws n_locks=1..5 and offset=0..63 once at "
          "on_start, so its lock configuration is stable but distinct:")
    seen = {}
    for offset in (0, 7, 23, 41):
        for n in (1, 3, 5):
            locks = _lock_set(grid, n, offset)
            seen[frozenset(locks)] = None
            print(f"  offset={offset:>2} n={n}: {locks}")
    print(f"\n  {len(seen)} distinct lock sets from 12 (offset, n) combinations")

    # locust/gevent leaves a greenlet finalizer that raises noisily at normal
    # interpreter shutdown; the preview has nothing to flush, so exit directly.
    sys.stdout.flush()
    os._exit(0)
