"""
aws_handler.py
--------------
AWS production job handler for the multicam pipeline.

Triggered by SQS messages (via Lambda event source mapping). Each message
carries a serialized MulticamJob. The handler:
    1. Downloads input videos from S3 to /tmp (Lambda ephemeral storage)
    2. Runs the full pipeline synchronously (Lambda is single-threaded per invocation)
    3. Uploads the rendered MP4 back to S3
    4. Tracks job status in DynamoDB throughout the lifecycle

Environment variables (set in Lambda configuration):
    DYNAMODB_TABLE   — DynamoDB table name for job status tracking
    S3_OUTPUT_BUCKET — S3 bucket for rendered output files
    AWS_REGION       — AWS region (auto-set by Lambda runtime)

IAM permissions required:
    - s3:GetObject        on input video bucket
    - s3:PutObject        on output bucket
    - dynamodb:PutItem    on the jobs table
    - dynamodb:UpdateItem on the jobs table
    - sqs:DeleteMessage   (handled automatically by Lambda SQS trigger)

Dependencies:
    pip install boto3
"""

import json
import os
import tempfile
import traceback
from typing import Any
from urllib.parse import urlparse

import boto3

from multicam_pipeline.job_schema import MulticamJob, JobStatus


# ---------------------------------------------------------------------------
# AWS client initialization
# ---------------------------------------------------------------------------
# Clients are initialized at module level so they are reused across Lambda
# warm invocations (avoids re-establishing connections on every event).

_region        = os.environ.get("AWS_REGION", "us-east-1")
_dynamodb_table = os.environ.get("DYNAMODB_TABLE", "multicam-jobs")
_output_bucket  = os.environ.get("S3_OUTPUT_BUCKET", "multicam-output")

s3         = boto3.client("s3", region_name=_region)
dynamodb   = boto3.resource("dynamodb", region_name=_region)
_ddb_table = dynamodb.Table(_dynamodb_table)


# ---------------------------------------------------------------------------
# DynamoDB status tracking
# ---------------------------------------------------------------------------

def _upsert_job_status(job: MulticamJob) -> None:
    """
    Write or update the job record in DynamoDB.

    Uses put_item so it works for both initial creation and status updates.
    The job_id is the DynamoDB partition key.

    Args:
        job: The MulticamJob whose current state should be persisted.
    """
    _ddb_table.put_item(Item=job.to_dict())
    print(f"[aws_handler] DynamoDB updated: job={job.job_id} status={job.status}")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """
    Parse an S3 URI (s3://bucket/key) into (bucket, key).

    Args:
        s3_uri: S3 URI string.

    Returns:
        Tuple of (bucket_name, object_key).

    Raises:
        ValueError: If the URI is not a valid s3:// URI.
    """
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got: {s3_uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _download_from_s3(s3_uri: str, local_dir: str) -> str:
    """
    Download a file from S3 to a local directory.

    Args:
        s3_uri:    S3 URI of the source file (s3://bucket/key).
        local_dir: Local directory to download into.

    Returns:
        Local file path of the downloaded file.
    """
    bucket, key = _parse_s3_uri(s3_uri)
    filename    = os.path.basename(key)
    local_path  = os.path.join(local_dir, filename)

    print(f"[aws_handler] Downloading s3://{bucket}/{key} → {local_path}")
    s3.download_file(bucket, key, local_path)
    return local_path


def _upload_to_s3(local_path: str, output_s3_uri: str) -> str:
    """
    Upload a local file to S3.

    Args:
        local_path:    Path to the local file to upload.
        output_s3_uri: Destination S3 URI (s3://bucket/key).

    Returns:
        The output S3 URI on success.
    """
    bucket, key = _parse_s3_uri(output_s3_uri)

    print(f"[aws_handler] Uploading {local_path} → s3://{bucket}/{key}")
    s3.upload_file(
        local_path, bucket, key,
        ExtraArgs={"ContentType": "video/mp4"},
    )
    return output_s3_uri


# ---------------------------------------------------------------------------
# Core pipeline runner
# ---------------------------------------------------------------------------

def _run_pipeline(job: MulticamJob, local_video_paths: list, local_output_path: str) -> tuple[str, str]:
    """
    Execute the full multicam pipeline against locally downloaded files.

    This is the same logical pipeline as the asyncio runner but operates
    on local /tmp paths rather than S3 URIs.

    Args:
        job:               The MulticamJob being processed (for config values).
        local_video_paths: List of local /tmp paths to downloaded input videos.
        local_output_path: Local /tmp path for the rendered output MP4.
    """
    from multicam_pipeline.pipeline import run_multicam_job

    # Use the shared orchestrator so local and AWS runners stay behaviorally identical.
    local_job = MulticamJob(
        job_id=job.job_id,
        video_paths=local_video_paths,
        output_path=local_output_path,
        cut_interval=job.cut_interval,
        target_width=job.target_width,
        target_height=job.target_height,
        target_fps=job.target_fps,
        cutting_strategy=job.cutting_strategy,
        event_type=job.event_type,
        effect_intensity=job.effect_intensity,
        rekognition_sample_rate=job.rekognition_sample_rate,
        audio_source_override=job.audio_source_override,
        selected_audio_source_path=job.selected_audio_source_path,
        project_id=job.project_id,
        status=job.status,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
    output_path = run_multicam_job(local_job)
    return output_path, (local_job.selected_audio_source_path or "")


def _map_local_source_to_original_uri(
    selected_local_path: str,
    local_video_paths: list[str],
    source_video_uris: list[str],
) -> str:
    """Map selected local temp file path back to its original source URI when possible."""
    if not selected_local_path:
        return ""
    try:
        idx = local_video_paths.index(selected_local_path)
        return source_video_uris[idx]
    except Exception:
        return selected_local_path


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    """
    AWS Lambda entry point — triggered by SQS event source mapping.

    Each SQS message body must be a JSON-serialized MulticamJob dict.
    Lambda's SQS trigger passes a batch of records; this handler processes
    each record independently so partial batch failures are handled correctly.

    SQS message body example:
        {
            "job_id": "abc-123",
            "video_paths": ["s3://my-bucket/cam1.mp4", "s3://my-bucket/cam2.mp4"],
            "output_path": "s3://multicam-output/abc-123/output.mp4",
            "cut_interval": 5.0,
            "target_width": 1920,
            "target_height": 1080,
            "target_fps": 30
        }

    Args:
        event:   Lambda event dict containing SQS records.
        context: Lambda context object (unused but required by signature).

    Returns:
        Dict with batchItemFailures list for partial batch failure reporting.
        Returning failed message IDs tells SQS to retry only those messages.
    """
    batch_failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        job = None

        try:
            # --- Parse the SQS message body into a MulticamJob ---
            body = json.loads(record["body"])
            job  = MulticamJob.from_dict(body)

            print(f"[aws_handler] Processing job {job.job_id} from SQS message {message_id}")

            # --- Mark job as PROCESSING in DynamoDB ---
            job.mark_processing()
            _upsert_job_status(job)

            # --- Use a temp directory scoped to this invocation ---
            # Lambda /tmp has 512MB–10GB depending on config. All files are
            # cleaned up automatically when the temp dir context exits.
            with tempfile.TemporaryDirectory(prefix=f"job_{job.job_id}_") as tmp_dir:

                # Step 1: Download all input videos from S3 to /tmp
                local_video_paths = [
                    _download_from_s3(s3_uri, tmp_dir)
                    for s3_uri in job.video_paths
                ]

                # Step 2: Define local output path in /tmp
                local_output_path = os.path.join(tmp_dir, "output.mp4")

                # Step 3: Run the full pipeline against local files
                local_output_result, selected_audio_source_path = _run_pipeline(
                    job,
                    local_video_paths,
                    local_output_path,
                )
                job.selected_audio_source_path = _map_local_source_to_original_uri(
                    selected_audio_source_path,
                    local_video_paths,
                    job.video_paths,
                )

                # Step 4: Upload rendered MP4 back to S3
                _upload_to_s3(local_output_result, job.output_path)

            # --- Mark job as COMPLETE in DynamoDB ---
            job.mark_complete()
            _upsert_job_status(job)

            print(f"[aws_handler] Job {job.job_id} complete → {job.output_path}")

        except Exception as exc:
            error_detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            print(f"[aws_handler] Job failed (SQS message {message_id}):\n{error_detail}")

            # Update DynamoDB with failure details if we have a job object
            if job:
                job.mark_failed(error_detail)
                _upsert_job_status(job)

            # Report this message as a batch failure so SQS retries it
            batch_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_failures}
