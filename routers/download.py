"""
routers/download.py
-------------------
Streams the finished MP4 back to the client.

Local dev: streams directly from LOCAL_OUTPUT_DIR using FileResponse.
AWS prod:  generates a pre-signed S3 URL and redirects the client to it.

Endpoints:
    GET /download/{job_id} — Download or redirect to the rendered MP4
"""

import os
import boto3
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from multicam_pipeline.auth import principal_id_from_claims, require_auth
from multicam_pipeline.config import (
    IS_LOCAL, LOCAL_OUTPUT_DIR,
    AWS_REGION, S3_OUTPUT_BUCKET,
)
from multicam_pipeline.routers.projects import get_project_record

router = APIRouter(prefix="/download", tags=["download"])

# Pre-signed URL expiry — 1 hour is sufficient for a direct download
PRESIGNED_URL_EXPIRY_SECONDS = 3600


def _get_local_output_path(job_id: str) -> str:
    """Resolve the local output MP4 path for a given job_id."""
    return os.path.join(LOCAL_OUTPUT_DIR, job_id, "output.mp4")


def _get_s3_output_key(job_id: str) -> str:
    """Resolve the S3 object key for a given job_id."""
    return f"{job_id}/output.mp4"


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Parse an S3 URI into (bucket, key)."""
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _authorize_download(job_id: str, claims: dict) -> tuple[str, str]:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    bucket = S3_OUTPUT_BUCKET
    key = _get_s3_output_key(job_id)

    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        item = dynamodb.Table(os.environ.get("DYNAMODB_TABLE", "multicam-jobs")).get_item(
            Key={"job_id": job_id}
        ).get("Item")
        if item and item.get("output_path"):
            bucket, key = _parse_s3_uri(item["output_path"])

        actor_id = principal_id_from_claims(claims)
        project_id = item.get("project_id") if item else None
        if actor_id and project_id:
            project = get_project_record(project_id)
            if not project or actor_id not in (project.get("members") or []):
                raise HTTPException(status_code=403, detail="You are not allowed to download this render.")
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
            raise HTTPException(status_code=404, detail=f"Output not found for job '{job_id}'. Job may still be processing.")
        raise HTTPException(status_code=500, detail=f"S3 error: {error}")

    return bucket, key


def _presigned_download_url(bucket: str, key: str, job_id: str) -> str:
    return boto3.client("s3", region_name=AWS_REGION).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="multicam_{job_id}.mp4"',
            "ResponseContentType": "video/mp4",
        },
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )


@router.get("/{job_id}/url")
async def get_download_url(job_id: str, _claims: dict = Depends(require_auth)):
    """Return an authenticated, temporary S3 URL for browser video playback or download."""
    if IS_LOCAL:
        raise HTTPException(status_code=501, detail="Temporary download URLs are available in AWS mode only.")

    bucket, key = _authorize_download(job_id, _claims)
    return {"url": _presigned_download_url(bucket, key, job_id), "expires_in_seconds": PRESIGNED_URL_EXPIRY_SECONDS}


@router.get("/{job_id}")
async def download_output(job_id: str, _claims: dict = Depends(require_auth)):
    """
    Download the rendered multicam MP4 for a completed job.

    Local dev:
        Streams the file directly from disk using FileResponse.
        Returns 404 if the output file does not exist yet (job still processing).

    AWS prod:
        Generates a pre-signed S3 URL valid for 1 hour and issues a 302
        redirect. The client downloads directly from S3 — no data passes
        through the API server.

    Status codes:
        200 OK           — file stream (local dev)
        302 Redirect     — pre-signed S3 URL (AWS prod)
        404 Not Found    — output not ready or job_id invalid
        500 Server Error — S3 pre-sign failure
    """
    if IS_LOCAL:
        # Resolve output path from the tracked job record first. Local jobs store
        # outputs under upload_id folders, which do not match job_id.
        from multicam_pipeline.job_runner import get_job_status

        job = get_job_status(job_id)
        output_path = job.get("output_path") if job else None
        project_id = job.get("project_id") if job else None

        actor_id = principal_id_from_claims(_claims)
        if actor_id and project_id:
            project = get_project_record(project_id)
            if not project or actor_id not in (project.get("members") or []):
                raise HTTPException(status_code=403, detail="You are not allowed to download this render.")

        if not output_path:
            # Backward-compat fallback for legacy folder layout.
            output_path = _get_local_output_path(job_id)

        if not os.path.isfile(output_path):
            raise HTTPException(
                status_code=404,
                detail=f"Output not found for job '{job_id}'. Job may still be processing.",
            )

        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename=f"multicam_{job_id}.mp4",
        )

    else:
        bucket, key = _authorize_download(job_id, _claims)
        return RedirectResponse(url=_presigned_download_url(bucket, key, job_id), status_code=302)
