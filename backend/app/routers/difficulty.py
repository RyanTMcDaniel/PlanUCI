import re
import torch
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import ml

router = APIRouter()

TIER_MIDPOINTS = {"easy": 2.0, "medium": 5.5, "hard": 9.0}


_SEMINAR_KEYWORDS = {
    "independent study", "independent research", "seminar", "proseminar",
    "dissertation", "thesis", "internship", "special topics", "special studies",
    "directed study", "directed research", "teaching assistant",
}

def apply_level_boost(course_id: str, base_score: float, title: str = "") -> float:
    if title and any(kw in title.lower() for kw in _SEMINAR_KEYWORDS):
        return base_score
    match = re.search(r'(\d+)', course_id)
    if not match:
        return base_score
    num = int(match.group(1))
    if num < 100:
        boost = 0.0
    elif num < 150:
        boost = 0.5
    else:
        boost = 0.8
    return min(9.0, base_score + boost)


def _infer(encoder, classifier, text: str, device) -> list[float]:
    with torch.no_grad():
        features = encoder.preprocess([text])
        features = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                    for k, v in features.items()}
        logits = classifier(encoder(features)["sentence_embedding"])
        return torch.softmax(logits, dim=-1)[0].cpu().tolist()


class CourseRequest(BaseModel):
    course_id: str


class ProfRequest(BaseModel):
    course_id: str
    instructor_id: str


@router.post("/course")
def difficulty_course(req: CourseRequest):
    course = ml["courses"].get(req.course_id)
    if not course or not course.get("description"):
        raise HTTPException(404, f"Course {req.course_id!r} not found or has no description")

    title = course.get("title") or req.course_id
    text = f"{title}: {course['description']}"
    probs = _infer(ml["diff_encoder"], ml["diff_classifier"], text, ml["device"])

    labels = ml["diff_labels"]
    pred_idx = probs.index(max(probs))
    tier = ml["diff_idx2label"][pred_idx]
    confidence = round(probs[pred_idx], 4)
    difficulty_score = round(
        apply_level_boost(
            req.course_id,
            sum(p * TIER_MIDPOINTS[l] for p, l in zip(probs, labels)),
            title,
        ), 2
    )

    return {
        "course_id": req.course_id,
        "difficulty_score": difficulty_score,
        "tier": tier,
        # NOTE: two different things are called "confidence" here, deliberately kept
        # distinct. `confidence` is the classifier's softmax on THIS prediction.
        # `signal_confidence` is how much the SERVED blended score (course_features)
        # is worth trusting, keyed on which signals backed it — high = nlp+gpa+rmp,
        # medium = two signals, low = NLP text alone.
        "confidence": confidence,
        "signal_confidence": ml["course_confidence"].get(req.course_id),
        "probabilities": {l: round(p, 4) for l, p in zip(labels, probs)},
    }


@router.post("/professor")
def difficulty_professor(req: ProfRequest):
    import pandas as pd

    df = ml["prof_features"]
    row = df[
        (df["course_id"] == req.course_id) &
        (df["instructor_id"] == req.instructor_id)
    ]
    if row.empty:
        raise HTTPException(
            404,
            f"No data for instructor {req.instructor_id!r} in course {req.course_id!r}",
        )

    r = row.iloc[0]

    def _val(col):
        v = r[col]
        return round(float(v), 4) if pd.notna(v) else None

    return {
        "course_id": req.course_id,
        "instructor_id": req.instructor_id,
        "difficulty_score": _val("difficulty_score"),
        "nlp_score": _val("nlp_score"),
        "gpa_score": _val("gpa_score"),
        "rmp_score": _val("rmp_score"),
        # Which signals actually backed this row, and how far to trust it.
        "signals_present": r["signals_present"] if pd.notna(r.get("signals_present")) else None,
        "confidence": r["confidence"] if pd.notna(r.get("confidence")) else None,
        "sections_taught": int(r["sections_taught"]),
    }
