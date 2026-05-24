import torch
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import ml

router = APIRouter()


class SentimentRequest(BaseModel):
    ucinetid: str


@router.post("/professor")
def sentiment_professor(req: SentimentRequest):
    review_text = ml["reviews"].get(req.ucinetid)
    if not review_text:
        raise HTTPException(
            404,
            f"No review text found for professor {req.ucinetid!r}",
        )

    with torch.no_grad():
        features = ml["sent_encoder"].preprocess([review_text])
        features = {k: v.to(ml["device"]) if isinstance(v, torch.Tensor) else v
                    for k, v in features.items()}
        logits = ml["sent_classifier"](ml["sent_encoder"](features)["sentence_embedding"])
        probs = torch.softmax(logits, dim=-1)[0].cpu().tolist()

    labels = ml["sent_labels"]
    pred_idx = probs.index(max(probs))
    label = ml["sent_idx2label"][pred_idx]
    confidence = round(probs[pred_idx], 4)

    return {
        "ucinetid": req.ucinetid,
        "label": label,
        "confidence": confidence,
        "probabilities": {l: round(p, 4) for l, p in zip(labels, probs)},
    }
