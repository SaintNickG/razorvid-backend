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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


def _load_aws_job(job_id: str, claims: dict) -> dict | None:
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        item = dynamodb.Table(os.environ.get("DYNAMODB_TABLE", "multicam-jobs")).get_item(
            Key={"job_id": job_id}
        ).get("Item")
        if not item:
            return None
        if item.get("output_purged"):
            raise HTTPException(
                status_code=410,
                detail="This render has passed its 24-hour retention window and is no longer available for download.",
            )
        actor_id = principal_id_from_claims(claims)
        project_id = item.get("project_id")
        if actor_id and project_id:
            project = get_project_record(project_id)
            if not project or actor_id not in (project.get("members") or []):
                raise HTTPException(status_code=403, detail="You are not allowed to download this render.")
        return item
    except HTTPException:
        raise
    except Exception:
        return None


def _authorize_download(job_id: str, claims: dict, part_index: int = 0) -> tuple[str, str, int]:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    bucket = S3_OUTPUT_BUCKET
    key = _get_s3_output_key(job_id)
    item = _load_aws_job(job_id, claims)
    output_parts = (item or {}).get("output_parts") or [(item or {}).get("output_path") or f"s3://{bucket}/{key}"]
    if part_index < 0 or part_index >= len(output_parts):
        raise HTTPException(status_code=404, detail="This render part was not found.")
    output_uri = output_parts[part_index]
    if output_uri:
        bucket, key = _parse_s3_uri(output_uri)

    try:
        s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
            raise HTTPException(status_code=404, detail=f"Output not found for job '{job_id}'. Job may still be processing.")
        raise HTTPException(status_code=500, detail=f"S3 error: {error}")

    return bucket, key, len(output_parts)


def _presigned_download_url(bucket: str, key: str, job_id: str, part_index: int = 0, part_count: int = 1) -> str:
    filename = (
        f"multicam_{job_id}_part_{part_index + 1:02d}_of_{part_count:02d}.mp4"
        if part_count > 1
        else f"multicam_{job_id}.mp4"
    )
    return boto3.client("s3", region_name=AWS_REGION).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
            "ResponseContentType": "video/mp4",
        },
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )


@router.get("/{job_id}/url")
async def get_download_url(
    job_id: str,
    request: Request,
    part: int = Query(default=1, ge=1),
    _claims: dict = Depends(require_auth),
):
    """Return an authenticated, temporary S3 URL for browser video playback or download."""
    if IS_LOCAL:
        from multicam_pipeline.job_runner import get_job_status

        job = get_job_status(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        project_id = job.get("project_id")
        actor_id = principal_id_from_claims(_claims)
        if actor_id and project_id:
            project = get_project_record(project_id)
            if not project or actor_id not in (project.get("members") or []):
                raise HTTPException(status_code=403, detail="You are not allowed to download this render.")
        output_parts = job.get("output_parts") or [job.get("output_path")]
        if part > len(output_parts) or not output_parts[part - 1]:
            raise HTTPException(status_code=404, detail="This render part was not found.")
        if job.get("status") != "COMPLETE":
            raise HTTPException(status_code=409, detail="Render part is not ready yet.")
        base = str(request.base_url).rstrip("/")
        return {
            "url": f"{base}/download/{job_id}?part={part}",
            "part_index": part,
            "total_parts": len(output_parts),
            "expires_in_seconds": PRESIGNED_URL_EXPIRY_SECONDS,
        }

    bucket, key, part_count = _authorize_download(job_id, _claims, part - 1)
    return {
        "url": _presigned_download_url(bucket, key, job_id, part - 1, part_count),
        "part_index": part,
        "total_parts": part_count,
        "expires_in_seconds": PRESIGNED_URL_EXPIRY_SECONDS,
    }


@router.get("/{job_id}/parts")
async def get_download_parts(job_id: str, request: Request, _claims: dict = Depends(require_auth)):
    """Return one authorized download URL per output part, including legacy single-file jobs."""
    if IS_LOCAL:
        from multicam_pipeline.job_runner import get_job_status

        job = get_job_status(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if job.get("status") != "COMPLETE":
            raise HTTPException(status_code=409, detail="Render parts are not ready yet.")
        project_id = job.get("project_id")
        actor_id = principal_id_from_claims(_claims)
        if actor_id and project_id:
            project = get_project_record(project_id)
            if not project or actor_id not in (project.get("members") or []):
                raise HTTPException(status_code=403, detail="You are not allowed to download this render.")
        paths = job.get("output_parts") or [job.get("output_path")]
        base = str(request.base_url).rstrip("/")
        return {"parts": [
            {
                "part_index": index + 1,
                "total_parts": len(paths),
                "filename": os.path.basename(path or "output.mp4"),
                "url": f"{base}/download/{job_id}?part={index + 1}",
            }
            for index, path in enumerate(paths)
            if path
        ]}

    item = _load_aws_job(job_id, _claims)
    if not item:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if str(item.get("status", "")).upper() != "COMPLETE":
        raise HTTPException(status_code=409, detail="Render parts are not ready yet.")
    paths = item.get("output_parts") or [item.get("output_path")]
    parts = []
    for index, uri in enumerate(paths):
        if not uri:
            continue
        bucket, key = _parse_s3_uri(uri)
        try:
            boto3.client("s3", region_name=AWS_REGION).head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Render part {index + 1} is not available yet.") from exc
        parts.append({
            "part_index": index + 1,
            "total_parts": len(paths),
            "filename": os.path.basename(key),
            "url": _presigned_download_url(bucket, key, job_id, index, len(paths)),
        })
    return {"parts": parts}


@router.get("/{job_id}")
async def download_output(
    job_id: str,
    part: int = Query(default=1, ge=1),
    _claims: dict = Depends(require_auth),
):
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

        output_parts = (job or {}).get("output_parts") or [output_path]
        if part > len(output_parts):
            raise HTTPException(status_code=404, detail="This render part was not found.")
        output_path = output_parts[part - 1]

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
            filename=(
                f"multicam_{job_id}_part_{part:02d}_of_{len(output_parts):02d}.mp4"
                if len(output_parts) > 1
                else f"multicam_{job_id}.mp4"
            ),
        )

    else:
        bucket, key, part_count = _authorize_download(job_id, _claims, part - 1)
        return RedirectResponse(
            url=_presigned_download_url(bucket, key, job_id, part - 1, part_count),
            status_code=302,
        )
