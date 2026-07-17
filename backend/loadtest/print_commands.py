"""Print the exact headless locust commands for the baseline runs.

Run from backend/:  venv/bin/python loadtest/print_commands.py
"""

LOCUST = "venv/bin/locust -f loadtest/locustfile.py"
HOST = "http://localhost:8001"

COLD = [(10, 2, "3m"), (50, 5, "3m"), (100, 10, "3m")]
WARM = [(50, 5, "2m")]

print("# START THE SERVER WITH THE WRITE KILL-SWITCH FIRST:")
print("#   LOAD_TEST_MODE=1 venv/bin/uvicorn app.main:app --port 8001")
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
