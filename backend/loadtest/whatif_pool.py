"""Validated whatif-scenario grids for the load test.

Maps major_id -> {quarter: [course_id]} — a prereq-valid course placement, not a
flat list.  Correct placement needs the prerequisite trees, which the load-test
payload builder does not have, so the grid is baked in here instead.

Each grid was confirmed (LOAD_TEST_MODE server, so no writes) to return HTTP 200
with status="ok" and a non-empty `plans` array from POST /optimizer/whatif — i.e.
the optimizer does real work rather than short-circuiting on an infeasible seed.

Built straight from Supabase major_requirements + transitive prereq closure; it
never calls /optimizer/generate, which has no frontend caller and may be removed.
Regenerate with backend/loadtest/validate_whatif_pool.py if the catalogue changes.

Grid sizes: BS-201G 29 courses, BA-0BI 46, BA-7A1 44, BA-801 45 — all within the
range of a real 4-year student grid.
"""

WHATIF_POOL = {
    "BS-201G": {
        "2026_fall": [
            "I&CSCI6B",
            "I&CSCI6D",
            "I&CSCI139W",
            "I&CSCI31"
        ],
        "2027_winter": [
            "I&CSCI32",
            "IN4MATX131",
            "I&CSCI6N"
        ],
        "2027_spring": [
            "I&CSCI33",
            "IN4MATX43",
            "COMPSCI184A"
        ],
        "2027_fall": [
            "I&CSCI45C",
            "I&CSCI51",
            "IN4MATX113",
            "IN4MATX117"
        ],
        "2028_winter": [
            "I&CSCI46",
            "IN4MATX121",
            "COMPSCI122A",
            "I&CSCI45J"
        ],
        "2028_spring": [
            "COMPSCI122D",
            "COMPSCI141",
            "COMPSCI142A",
            "COMPSCI143A"
        ],
        "2028_fall": [
            "I&CSCI53",
            "IN4MATX115",
            "IN4MATX122",
            "IN4MATX133"
        ],
        "2029_winter": [
            "IN4MATX134",
            "COMPSCI122C",
            "COMPSCI122B"
        ],
        "2029_spring": [],
        "2029_fall": [],
        "2030_winter": [],
        "2030_spring": []
    },
    "BA-0BI": {
        "2026_fall": [
            "GLBLME60A",
            "GLBLME60B",
            "GLBLME60C",
            "GLBLME100W"
        ],
        "2027_winter": [
            "AFAM137",
            "ANTHRO125Z",
            "ANTHRO165A",
            "ARTHIS155A"
        ],
        "2027_spring": [
            "ASIANAM142",
            "ASIANAM151F",
            "HISTORY126B",
            "HISTORY131A"
        ],
        "2027_fall": [
            "HISTORY131C",
            "HISTORY132B",
            "HISTORY132E",
            "HISTORY134E"
        ],
        "2028_winter": [
            "HISTORY170A",
            "INTLST189",
            "INTLST145A",
            "INTLST152A"
        ],
        "2028_spring": [
            "INTLST161A",
            "INTLST165",
            "INTLST112A",
            "INTLST122"
        ],
        "2028_fall": [
            "INTLST151B",
            "INTLST175A",
            "PERSIAN165A",
            "POLSCI141B"
        ],
        "2029_winter": [
            "POLSCI144A",
            "POLSCI153E",
            "POLSCI158D",
            "PUBHLTH168"
        ],
        "2029_spring": [
            "RELSTD115",
            "RELSTD122",
            "RELSTD131A",
            "SOCSCI115D"
        ],
        "2029_fall": [
            "SOCSCI152A",
            "SOCSCI178F",
            "SOCSCI188A",
            "SOCSCI188K"
        ],
        "2030_winter": [
            "SOCIOL177W",
            "UPPP113",
            "POLSCI41A",
            "POLSCI71A"
        ],
        "2030_spring": [
            "POLSCI172A",
            "POLSCI146B"
        ]
    },
    "BA-7A1": {
        "2026_fall": [
            "PUBHLTH1",
            "PUBHLTH144",
            "PUBHLTH122",
            "PUBHLTH7A"
        ],
        "2027_winter": [
            "PUBHLTH2",
            "PUBHLTH5",
            "PUBHLTH170",
            "PUBHLTH7B"
        ],
        "2027_spring": [
            "PUBHLTH195P",
            "PUBHLTH195W",
            "ANTHRO2A",
            "ANTHRO2B"
        ],
        "2027_fall": [
            "ANTHRO2C",
            "ANTHRO2D",
            "ANTHRO41A",
            "ECON1"
        ],
        "2028_winter": [
            "ECON13",
            "ECON20A",
            "INTLST11",
            "POLSCI31A"
        ],
        "2028_spring": [
            "ECON20B",
            "POLSCI51A",
            "PSCI9",
            "COGS7A"
        ],
        "2028_fall": [
            "PSCI11A/COGS9A",
            "PSCI11B/COGS9B",
            "PSCI11C/COGS9C",
            "SOCIOL1"
        ],
        "2029_winter": [
            "SOCIOL2",
            "SOCIOL3",
            "UPPP8",
            "ANTHRO128B"
        ],
        "2029_spring": [
            "ANTHRO134A",
            "ANTHRO134B",
            "ANTHRO134C",
            "ANTHRO134F"
        ],
        "2029_fall": [
            "ANTHRO134N",
            "MGMT107",
            "MGMT165",
            "MGMT166"
        ],
        "2030_winter": [
            "PSCI103H",
            "PSCI136H",
            "PSCI137H",
            "PSCI138H"
        ],
        "2030_spring": []
    },
    "BA-801": {
        "2026_fall": [
            "RELSTD110W",
            "ANTHRO/RELSTD60",
            "ANTHRO125Z/ASIANAM142",
            "ANTHRO129"
        ],
        "2027_winter": [
            "ANTHRO139",
            "ANTHRO149",
            "ANTHRO165A",
            "ANTHRO169"
        ],
        "2027_spring": [
            "ARTHIS40A",
            "ARTHIS40B",
            "ARTHIS42D",
            "ARTHIS100"
        ],
        "2027_fall": [
            "ARTHIS114",
            "ARTHIS125",
            "ARTHIS150",
            "ARTHIS155A/HISTORY170A/RELSTD122"
        ],
        "2028_winter": [
            "ARTHIS155B/HISTORY170B/RELSTD123",
            "ARTHIS155D",
            "ARTHIS198",
            "ASIANAM150"
        ],
        "2028_spring": [
            "CLASSIC45A",
            "CLASSIC45C",
            "CLASSIC150",
            "CLASSIC176"
        ],
        "2028_fall": [
            "COMLIT100A",
            "COMLIT105",
            "EAS55",
            "EAS116"
        ],
        "2029_winter": [
            "EAS150",
            "EAS190",
            "ECON/RELSTD17",
            "ENGLISH10"
        ],
        "2029_spring": [
            "EUROST103",
            "FLM&MDA160",
            "FRENCH171",
            "GEN&SEX60C/RELSTD61"
        ],
        "2029_fall": [
            "GERMAN150",
            "HISTORY10",
            "HISTORY12",
            "HISTORY18A"
        ],
        "2030_winter": [
            "HISTORY70B",
            "HISTORY70E",
            "HISTORY100W",
            "HISTORY114"
        ],
        "2030_spring": [
            "HISTORY130C"
        ]
    }
}
