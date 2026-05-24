import os

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend", ".env")
load_dotenv(_ENV)


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_all(client, table: str, columns: str) -> list[dict]:
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            client.table(table)
            .select(columns)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def main() -> None:
    client = get_client()

    print("Fetching grade_distributions...")
    grades = fetch_all(
        client, "grade_distributions",
        "course_id,grade_a_count,grade_b_count,grade_c_count,"
        "grade_d_count,grade_f_count,grade_w_count",
    )
    print(f"  {len(grades)} rows")

    print("Fetching courses...")
    courses = fetch_all(client, "courses", "id,title,description,department")
    print(f"  {len(courses)} rows")

    grades_df = pd.DataFrame(grades)
    courses_df = pd.DataFrame(courses).rename(columns={"id": "course_id"})

    grade_cols = ["grade_a_count", "grade_b_count", "grade_c_count",
                  "grade_d_count", "grade_f_count"]
    grades_df[grade_cols] = grades_df[grade_cols].fillna(0).astype(int)

    # Graded students = A+B+C+D+F only; W excluded from both numerator and denominator
    grades_df["graded"] = grades_df[grade_cols].sum(axis=1)
    grades_df["weighted_points"] = (
        grades_df["grade_a_count"] * 4
        + grades_df["grade_b_count"] * 3
        + grades_df["grade_c_count"] * 2
        + grades_df["grade_d_count"] * 1
        # F contributes 0
    )
    grades_df["section_gpa"] = grades_df["weighted_points"] / grades_df["graded"]

    # Drop sections with fewer than 15 graded students
    valid = grades_df[grades_df["graded"] >= 15].copy()
    print(f"\n  {len(valid)} sections after dropping graded < 15 "
          f"(removed {len(grades_df) - len(valid)})")

    # Course-level GPA: weighted average across sections by graded count
    agg = (
        valid.groupby("course_id")
        .apply(
            lambda g: pd.Series({
                "course_gpa": (g["section_gpa"] * g["graded"]).sum() / g["graded"].sum(),
                "valid_sections": len(g),
                "total_graded": g["graded"].sum(),
            }),
            include_groups=False,
        )
        .reset_index()
    )

    # Drop courses with fewer than 3 valid sections
    before = len(agg)
    agg = agg[agg["valid_sections"] >= 3]
    print(f"  {len(agg)} courses after dropping < 3 valid sections "
          f"(removed {before - len(agg)})")

    merged = agg.merge(courses_df, on="course_id", how="inner")
    merged = merged.dropna(subset=["description", "course_gpa"])

    # Drop graduate courses (course number >= 200)
    merged["_course_num"] = (
        merged["course_id"].str.replace(r"[^0-9]", "", regex=True).astype(int)
    )
    before_grad = len(merged)
    merged = merged[merged["_course_num"] < 200].drop(columns=["_course_num"])
    print(f"\nRemoved {before_grad - len(merged)} graduate courses (num >= 200), "
          f"{len(merged)} remain")

    # Drop non-academic departments
    excluded_depts = {"ROTC", "NUR SCI", "PHRMSCI", "PHMD", "LSCI"}
    before_dept = len(merged)
    merged = merged[~merged["department"].isin(excluded_depts)]
    print(f"Removed {before_dept - len(merged)} courses from excluded departments, "
          f"{len(merged)} remain")

    # Drop participation-graded courses (perfect 4.0)
    before_perfect = len(merged)
    merged = merged[merged["course_gpa"] != 4.0]
    print(f"Removed {before_perfect - len(merged)} courses with GPA == 4.0, "
          f"{len(merged)} remain")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "courses_with_gpa.csv")
    merged.to_csv(out_path, index=False)
    print(f"\nSaved {len(merged)} courses to {out_path}")

    cols = ["course_id", "department", "course_gpa", "valid_sections", "total_graded"]

    print("\n10 lowest GPA courses:")
    print(merged[cols].sort_values("course_gpa").head(10).to_string(index=False))

    print("\n10 highest GPA courses:")
    print(merged[cols].sort_values("course_gpa", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    main()
