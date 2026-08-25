"""
routers/admin.py
----------------
Admin-only observability endpoints for failed job analysis and retries.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException

from multicam_pipeline.auth import require_admin
from multicam_pipeline.billing_store import billing_backend_health
from multicam_pipeline.config import AWS_REGION, DYNAMODB_TABLE, IS_AWS
from multicam_pipeline.job_schema import CuttingStrategy, EffectIntensity, EventType, MulticamJob

router = APIRouter(prefix="/admin", tags=["admin"])

REVIEW_URL_EXPIRY_SECONDS = 3600


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _presigned_review_url(uri: str) -> str | None:
    if not uri.startswith("s3://"):
        return None
    try:
        bucket, key = _parse_s3_uri(uri)
        return boto3.client("s3", region_name=AWS_REGION).generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=REVIEW_URL_EXPIRY_SECONDS,
        )
    except Exception:
        return None


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


def _list_projects_aws() -> List[Dict[str, Any]]:
    table_name = os.environ.get("PROJECTS_DYNAMODB_TABLE", "").strip()
    if not table_name:
        return []

    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)
    response = table.scan()
    items = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return [item["project"] for item in items if item.get("item_type") == "project" and item.get("project")]


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


@router.get("/review")
async def review_media(_claims: dict = Depends(require_admin)):
    """List all beta projects with temporary links for reviewing source angles and renders."""
    if not IS_AWS:
        raise HTTPException(status_code=501, detail="Media review is available in AWS mode only.")

    jobs_by_project: Dict[str, List[Dict[str, Any]]] = {}
    for job in _scan_jobs_aws():
        project_id = job.get("project_id")
        if project_id:
            jobs_by_project.setdefault(project_id, []).append(job)

    projects = []
    for project in _list_projects_aws():
        uploads = []
        for upload in project.get("uploads", []):
            uploads.append({
                "upload_id": upload.get("upload_id"),
                "user_name": upload.get("user_name") or upload.get("user_id") or "Unknown contributor",
                "added_at": upload.get("added_at"),
                "files": [
                    {"source": file_uri, "review_url": _presigned_review_url(file_uri)}
                    for file_uri in upload.get("files", [])
                ],
            })

        renders = []
        for job in sorted(jobs_by_project.get(project.get("project_id"), []), key=lambda item: item.get("created_at", ""), reverse=True):
            renders.append({
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "output_url": _presigned_review_url(job.get("output_path", "")) if job.get("status") == "COMPLETE" else None,
                "sync_diagnostics": job.get("sync_diagnostics"),
            })

        projects.append({
            "project_id": project.get("project_id"),
            "name": project.get("name"),
            "event_type": project.get("event_type"),
            "updated_at": project.get("updated_at"),
            "uploads": uploads,
            "renders": renders,
        })

    projects.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return {"expires_in_seconds": REVIEW_URL_EXPIRY_SECONDS, "projects": projects}


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
