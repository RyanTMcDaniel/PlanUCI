"""Print the exact headless locust commands for the baseline runs.

Run from backend/:  venv/bin/python loadtest/print_commands.py
"""

LOCUST = "venv/bin/locust -f loadtest/locustfile.py"
HOST = "http://localhost:8001"

COLD = [(10, 2, "3m"), (50, 5, "3m"), (100, 10, "3m")]
WARM = [(50, 5, "2m")]

# whatif is the endpoint the UI actually calls.  Shorter runs than the generate
# baseline: four shapes to cover, each measured separately.
WHATIF_COLD = [(10, 2, "2m"), (50, 5, "2m")]
WHATIF_WARM = [(50, 5, "2m")]

print("# START THE SERVER WITH THE WRITE KILL-SWITCH FIRST:")
print("#   LOAD_TEST_MODE=1 venv/bin/uvicorn app.main:app --port 8001")
print()
print("# Regenerate the whatif grid fixture if the catalogue changed:")
print("#   venv/bin/python loadtest/validate_whatif_pool.py")
print()

print("# ── /optimizer/generate — historical baseline (no frontend caller) ──")
print()
print("# COLD (real solve — nonce-forced miss, random real major):")
for u, r, t in COLD:
    print(f"LOCUST_SCENARIO=cold {LOCUST} ColdUser --headless --host {HOST} "
          f"-u {u} -r {r} -t {t} --csv loadtest/results/cold_{u}")
print()
print("# WARM (L1 cache hit — cached throughput, for contrast):")
for u, r, t in WARM:
    print(f"LOCUST_SCENARIO=warm {LOCUST} WarmUser --headless --host {HOST} "
          f"-u {u} -r {r} -t {t} --csv loadtest/results/warm_{u}")
print()

print("# ── /optimizer/whatif — the endpoint the UI actually calls ──")
print()
print("# AUTOFILL COLD (zero-lock, nonce-forced miss — the real solve):")
for u, r, t in WHATIF_COLD:
    print(f"LOCUST_SCENARIO=whatif-autofill-cold {LOCUST} WhatifAutofillColdUser "
          f"--headless --host {HOST} -u {u} -r {r} -t {t} "
          f"--csv loadtest/results/whatif_autofill_cold_{u}")
print()
print("# AUTOFILL WARM (zero-lock, fixed payload -> cache hit; the shape with")
print("# genuine cross-user reuse):")
for u, r, t in WHATIF_WARM:
    print(f"LOCUST_SCENARIO=whatif-autofill-warm {LOCUST} WhatifAutofillWarmUser "
          f"--headless --host {HOST} -u {u} -r {r} -t {t} "
          f"--csv loadtest/results/whatif_autofill_warm_{u}")
print()
print("# LOCKED COLD (lock sets varied per simulated user -> miss):")
for u, r, t in WHATIF_COLD:
    print(f"LOCUST_SCENARIO=whatif-locked-cold {LOCUST} WhatifLockedColdUser "
          f"--headless --host {HOST} -u {u} -r {r} -t {t} "
          f"--csv loadtest/results/whatif_locked_cold_{u}")
print()
print("# LOCKED WARM (fixed lock-bearing payload).  EXPECTED to show no speedup:")
print("# the admission policy deliberately does not cache lock-bearing successes.")
print("# Its delta vs autofill-warm is the evidence for that choice.")
for u, r, t in WHATIF_WARM:
    print(f"LOCUST_SCENARIO=whatif-locked-warm {LOCUST} WhatifLockedWarmUser "
          f"--headless --host {HOST} -u {u} -r {r} -t {t} "
          f"--csv loadtest/results/whatif_locked_warm_{u}")
