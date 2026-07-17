"""Validated major_ids for the COLD load-test scenario.

Each id was confirmed (LOAD_TEST_MODE server, so no writes) to return HTTP 200
with a non-empty `variants` array from POST /optimizer/generate — i.e. it makes
the optimizer do real work rather than short-circuiting on an empty/infeasible
plan.  Regenerate with backend/loadtest/validate_pool.py if the catalogue changes.
"""

MAJOR_POOL = [
    "BS-201G",    # B.S. Informatics (ICS)
    "BA-014",
    "BA-0DB",
    "BA-192D",
    "BA-579J",
    "BA-762T",
    "BA-882",
    "BMUS-582B",
]
