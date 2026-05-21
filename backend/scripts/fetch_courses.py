import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

DEPARTMENTS = [
    "AC ENG",
    "AFAM",
    "ANATOMY",
    "ANTHRO",
    "ARABIC",
    "ARMN",
    "ART",
    "ART HIS",
    "ARTS",
    "ASIANAM",
    "ASL",
    "BANA",
    "BATS",
    "BIO SCI",
    "BIOCHEM",
    "BME",
    "BSEMD",
    "CBE",
    "CBEMS",
    "CHC/LAT",
    "CHEM",
    "CHINESE",
    "CLASSIC",
    "CLT&THY",
    "COGS",
    "COM LIT",
    "COMPSCI",
    "CRITISM",
    "CRM/LAW",
    "CSE",
    "DANCE",
    "DATA",
    "DEV BIO",
    "DRAMA",
    "E ASIAN",
    "EARTHSS",
    "EAS",
    "ECO EVO",
    "ECON",
    "ECPS",
    "EDUC",
    "EECS",
    "EHS",
    "ENGLISH",
    "ENGR",
    "ENGRCEE",
    "ENGRMAE",
    "ENGRMSE",
    "EPIDEM",
    "EURO ST",
    "FIN",
    "FLM&MDA",
    "FRENCH",
    "GDIM",
    "GEN&SEX",
    "GERMAN",
    "GLBL ME",
    "GLBLCLT",
    "GREEK",
    "HEBREW",
    "HISTORY",
    "HUMAN",
    "I&C SCI",
    "IN4MATX",
    "INNO",
    "INTL ST",
    "IRAN",
    "ITALIAN",
    "JAPANSE",
    "KOREAN",
    "LATIN",
    "LINGUIS",
    "LIT JRN",
    "LPS",
    "LSCI",
    "M&MG",
    "MATH",
    "MED HUM",
    "MGMT",
    "MGMT EP",
    "MGMT FE",
    "MGMT HC",
    "MGMTMBA",
    "MGMTPHD",
    "MNGE",
    "MOL BIO",
    "MPAC",
    "MSE",
    "MUSIC",
    "NET SYS",
    "NEURBIO",
    "NUR SCI",
    "PATH",
    "PED GEN",
    "PERSIAN",
    "PHARM",
    "PHILOS",
    "PHMD",
    "PHRMSCI",
    "PHY SCI",
    "PHYSICS",
    "PHYSIO",
    "POL SCI",
    "PORTUG",
    "PP&D",
    "PSCI",
    "PSY BEH",
    "PSYCH",
    "PUB POL",
    "PUBHLTH",
    "REL STD",
    "ROTC",
    "RUSSIAN",
    "SOC SCI",
    "SOCECOL",
    "SOCIOL",
    "SPANISH",
    "SPPS",
    "STATS",
    "SWE",
    "TOX",
    "UCDC",
    "UNI AFF",
    "UNI STU",
    "UPPP",
    "VIETMSE",
    "VIS STD",
    "WOMN ST",
    "WRITING",
]

BASE_URL = "https://anteaterapi.com/v2/rest/courses"


def fetch_courses(department: str) -> dict:
    response = requests.get(BASE_URL, params={"department": department})
    response.raise_for_status()
    return response.json()


def map_course_to_db(course: dict) -> dict:
    return {
        "id": course.get("id"),
        "department": course.get("department"),
        "course_number": course.get("courseNumber"),
        "course_numeric": course.get("courseNumeric"),
        "title": course.get("title"),
        "description": course.get("description"),
        "school": course.get("school"),
        "department_name": course.get("departmentName"),
        "min_units": course.get("minUnits"),
        "max_units": course.get("maxUnits"),
        "course_level": course.get("courseLevel"),
        "restriction": course.get("restriction"),
        "ge_list": course.get("geList", []),
        "ge_text": course.get("geText"),
        "terms": course.get("terms", []),
        "prerequisite_text": course.get("prerequisiteText"),
        "prerequisite_tree": course.get("prerequisiteTree"),
        "repeatability": course.get("repeatability"),
        "grading_option": course.get("gradingOption"),
        "same_as": course.get("sameAs"),
        "corequisites": course.get("corequisites"),
    }


def map_instructor_to_db(instructor: dict) -> dict:
    return {
        "ucinetid": instructor.get("ucinetid"),
        "name": instructor.get("name"),
        "title": instructor.get("title"),
        "email": instructor.get("email"),
        "department": instructor.get("department"),
        "shortened_names": instructor.get("shortenedNames", []),
    }


def map_course_instructor_to_db(course_id: str, ucinetid: str) -> dict:
    return {
        "course_id": course_id,
        "ucinetid": ucinetid,
    }


def upsert_course(course: dict, instructors: list[dict], course_instructors: list[dict]) -> None:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    client = create_client(url, key)

    client.table("courses").upsert(course, on_conflict="id").execute()

    if instructors:
        client.table("instructors").upsert(instructors, on_conflict="ucinetid").execute()

    if course_instructors:
        client.table("course_instructors").upsert(course_instructors, on_conflict="course_id,ucinetid").execute()

    print(f"Upserted course {course['id']}")


def main() -> None:
    total_courses = 0
    total_errors = 0

    for department in DEPARTMENTS:
        print(f"Fetching {department}...")
        try:
            data = fetch_courses(department)
            courses = data.get("data", [])
        except Exception as e:
            print(f"  Error fetching {department}: {e}")
            total_errors += 1
            time.sleep(0.5)
            continue

        for course in courses:
            try:
                course_row = map_course_to_db(course)
                instructor_rows = [
                    map_instructor_to_db(i) for i in course.get("instructors", [])
                ]
                course_instructor_rows = [
                    map_course_instructor_to_db(course_row["id"], i["ucinetid"])
                    for i in instructor_rows
                    if i.get("ucinetid")
                ]
                upsert_course(course_row, instructor_rows, course_instructor_rows)
                total_courses += 1
            except Exception as e:
                print(f"  Error processing course {course.get('id')}: {e}")
                total_errors += 1

        time.sleep(0.5)

    print(f"\nDone. Courses processed: {total_courses}, Errors: {total_errors}")


if __name__ == "__main__":
    main()
