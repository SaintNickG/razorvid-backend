"""
routers/upload.py
-----------------
Handles multipart video file uploads.

Local dev: saves files to LOCAL_UPLOAD_DIR, returns local file paths.
AWS prod:  streams files directly to S3, returns s3:// URIs.

Endpoint:
    POST /upload
        - Accepts one or more video files as multipart/form-data
        - Returns a list of file references (paths or S3 URIs) to pass
          directly into POST /render
"""

import os
import uuid
import boto3
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from multicam_pipeline.auth import require_auth

from multicam_pipeline.config import (
    IS_LOCAL, LOCAL_UPLOAD_DIR,
    S3_INPUT_BUCKET, AWS_REGION,
    MAX_UPLOAD_BYTES,
)

router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _validate_extension(filename: str) -> None:
    ext = os.path.splitext(filename)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
        )


async def _save_local(file: UploadFile, upload_id: str) -> str:
    """
    Save an uploaded file to LOCAL_UPLOAD_DIR.

    Returns the absolute local file path.
    """
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(LOCAL_UPLOAD_DIR, f"{upload_id}_{file.filename}")

    total_bytes = 0
    with open(dest, "wb") as f:
        # Stream in 1MB chunks — never loads the full file into memory
        while chunk := await file.read(1024 * 1024):
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds maximum upload size.")
            f.write(chunk)

    return dest


async def _save_s3(file: UploadFile, upload_id: str) -> str:
    """
    Stream an uploaded file directly to S3.

    Returns the s3:// URI of the uploaded object.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    key = f"uploads/{upload_id}/{file.filename}"

    # Read full file into memory for S3 put — for very large files consider
    # switching to multipart upload via boto3 TransferConfig
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size.")

    s3.put_object(
        Bucket=S3_INPUT_BUCKET,
        Key=key,
        Body=content,
        ContentType=file.content_type or "video/mp4",
    )

    return f"s3://{S3_INPUT_BUCKET}/{key}"


@router.post("")
async def upload_videos(
    files: List[UploadFile] = File(...),
    _claims: dict = Depends(require_auth),
):
    """
    Upload one or more video files for multicam processing.

    Returns a list of file references to pass directly into POST /render.

    - Local dev:  returns absolute local file paths
    - AWS prod:   returns s3:// URIs

    Example response:
        {
            "upload_id": "abc-123",
            "files": [
                "s3://my-bucket/uploads/abc-123/cam1.mp4",
                "s3://my-bucket/uploads/abc-123/cam2.mp4"
            ]
        }
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    upload_id = str(uuid.uuid4())
    saved_refs: List[str] = []

    for file in files:
        _validate_extension(file.filename)

        if IS_LOCAL:
            ref = await _save_local(file, upload_id)
        else:
            ref = await _save_s3(file, upload_id)

        saved_refs.append(ref)
        print(f"[upload] Saved '{file.filename}' → {ref}")

    return JSONResponse({"upload_id": upload_id, "files": saved_refs})
