"""
routers/admin.py
----------------
Admin-only observability endpoints for failed job analysis and retries.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import boto3
from fastapi import APIRouter, Depends, HTTPException

from multicam_pipeline.auth import require_admin
from multicam_pipeline.billing_store import billing_backend_health
from multicam_pipeline.config import AWS_REGION, DYNAMODB_TABLE, IS_AWS
from multicam_pipeline.job_schema import CuttingStrategy, EffectIntensity, EventType, MulticamJob

router = APIRouter(prefix="/admin", tags=["admin"])


def _scan_jobs_aws() -> List[Dict[str, Any]]:
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)
    response = table.scan()
    items = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def _list_jobs() -> List[Dict[str, Any]]:
    if IS_AWS:
        return _scan_jobs_aws()
    from multicam_pipeline.job_runner import list_jobs

    return list_jobs()


def _get_job(job_id: str) -> Dict[str, Any] | None:
    if IS_AWS:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table = dynamodb.Table(DYNAMODB_TABLE)
        return table.get_item(Key={"job_id": job_id}).get("Item")

    from multicam_pipeline.job_runner import get_job_status

    return get_job_status(job_id)


def _enqueue_aws_job(job: MulticamJob) -> None:
    queue_url = os.environ.get("SQS_QUEUE_URL", "").strip()
    if not queue_url:
        raise HTTPException(status_code=500, detail="SQS_QUEUE_URL is not configured.")

    ddb = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMODB_TABLE)
    ddb.put_item(Item=job.to_dict())

    sqs = boto3.client("sqs", region_name=AWS_REGION)
    payload = {"QueueUrl": queue_url, "MessageBody": json.dumps(job.to_dict())}
    if queue_url.lower().endswith(".fifo"):
        payload["MessageGroupId"] = os.environ.get("SQS_MESSAGE_GROUP_ID", "multicam")
        payload["MessageDeduplicationId"] = job.job_id
    sqs.send_message(**payload)


@router.get("/observability/summary")
async def get_summary(_claims: dict = Depends(require_admin)):
    """Return global status counts and recent failure density."""
    jobs = _list_jobs()
    counts: Dict[str, int] = {"PENDING": 0, "PROCESSING": 0, "COMPLETE": 0, "FAILED": 0}
    for item in jobs:
        status = str(item.get("status", "")).upper()
        if status in counts:
            counts[status] += 1

    failed = [j for j in jobs if str(j.get("status", "")).upper() == "FAILED"]
    failed_sorted = sorted(failed, key=lambda x: x.get("updated_at", ""), reverse=True)

    return {
        "total_jobs": len(jobs),
        "status_counts": counts,
        "recent_failed_jobs": [
            {
                "job_id": item.get("job_id"),
                "project_id": item.get("project_id"),
                "updated_at": item.get("updated_at"),
                "error": item.get("error"),
            }
            for item in failed_sorted[:20]
        ],
    }


@router.get("/observability/failed-jobs")
async def list_failed_jobs(limit: int = 100, _claims: dict = Depends(require_admin)):
    """List failed jobs with compact failure metadata."""
    bounded_limit = max(1, min(limit, 500))
    jobs = _list_jobs()
    failed = [j for j in jobs if str(j.get("status", "")).upper() == "FAILED"]
    failed.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    trimmed = failed[:bounded_limit]
    return {
        "count": len(trimmed),
        "items": [
            {
                "job_id": item.get("job_id"),
                "project_id": item.get("project_id"),
                "upload_files": item.get("video_paths", []),
                "output_path": item.get("output_path"),
                "updated_at": item.get("updated_at"),
                "error": item.get("error"),
            }
            for item in trimmed
        ],
    }


@router.get("/observability/billing-health")
async def billing_health(_claims: dict = Depends(require_admin)):
    """Report billing backend health and key-schema correctness."""
    status = billing_backend_health()
    return {
        "ok": status.get("ok", False),
        "billing_backend": status.get("backend"),
        "details": status.get("details", {}),
    }


@router.post("/jobs/{job_id}/retry")
async def retry_failed_job(job_id: str, _claims: dict = Depends(require_admin)):
    """Retry a failed job by cloning its configuration into a fresh job ID."""
    existing = _get_job(job_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Job not found.")

    status = str(existing.get("status", "")).upper()
    if status != "FAILED":
        raise HTTPException(status_code=400, detail="Only FAILED jobs can be retried.")

    retry_job = MulticamJob(
        video_paths=existing.get("video_paths") or [],
        output_path=str(existing.get("output_path") or ""),
        cut_interval=float(existing.get("cut_interval", 5.0)),
        target_width=int(existing.get("target_width", 1920)),
        target_height=int(existing.get("target_height", 1080)),
        target_fps=int(existing.get("target_fps", 30)),
        cutting_strategy=CuttingStrategy(existing.get("cutting_strategy", "local")),
        event_type=EventType(existing.get("event_type", "cheer")),
        effect_intensity=EffectIntensity(existing.get("effect_intensity", "balanced")),
        rekognition_sample_rate=int(existing.get("rekognition_sample_rate", 15)),
        audio_source_override=existing.get("audio_source_override"),
        project_id=existing.get("project_id"),
    )

    if IS_AWS:
        _enqueue_aws_job(retry_job)
    else:
        from multicam_pipeline.job_runner import submit_job

        await submit_job(retry_job)

    return {
        "retried_from": job_id,
        "new_job_id": retry_job.job_id,
        "status": retry_job.status.value,
    }
