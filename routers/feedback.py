import os
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from multicam_pipeline.auth import principal_id_from_claims, require_auth
from multicam_pipeline.config import AWS_REGION

router = APIRouter(prefix="/feedback", tags=["feedback"])

FEEDBACK_DYNAMODB_TABLE = os.environ.get("FEEDBACK_DYNAMODB_TABLE", "multicam-feedback").strip()


class FeedbackRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    activity: str = Field(min_length=3, max_length=500)
    worked_well: str = Field(min_length=3, max_length=4000)
    issue: str | None = Field(default=None, max_length=4000)
    suggested_contexts: str | None = Field(default=None, max_length=1000)
    follow_up_allowed: bool = False


@router.post("", status_code=201)
async def submit_feedback(request: FeedbackRequest, claims: dict = Depends(require_auth)):
    if not FEEDBACK_DYNAMODB_TABLE:
        raise HTTPException(status_code=500, detail="Feedback storage is not configured.")

    feedback_id = str(uuid.uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()
    item = {
        "feedback_id": feedback_id,
        "submitted_at": submitted_at,
        "user_id": principal_id_from_claims(claims),
        **request.model_dump(),
    }

    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(FEEDBACK_DYNAMODB_TABLE)
    table.put_item(Item=item)
    return {"feedback_id": feedback_id, "submitted_at": submitted_at}