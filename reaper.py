"""
reaper.py
---------
Stuck-job reaper — Lambda function triggered by EventBridge on a schedule.

Scans DynamoDB for jobs that have been in PROCESSING status longer than
STUCK_THRESHOLD_MINUTES and marks them FAILED with a descriptive error.

This recovers jobs killed by Lambda hard-kills (OOM, timeout) where the
Python exception handler never ran and DynamoDB was never updated.

Also purges completed render outputs from S3 once they pass their
`expires_at` retention window (default 24 hours), keeping storage costs
bounded while giving creators a full day to download their videos.

Environment variables:
    DYNAMODB_TABLE          — DynamoDB table name (default: multicam-jobs)
    AWS_REGION              — AWS region (auto-set by Lambda runtime)
    STUCK_THRESHOLD_MINUTES — Minutes before a PROCESSING job is considered
                              stuck (default: 20)
    S3_OUTPUT_BUCKET        — Bucket holding rendered outputs (for purge)
"""

import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Attr

_region    = os.environ.get("AWS_REGION", "us-east-1")
_table_name = os.environ.get("DYNAMODB_TABLE", "multicam-jobs")
_threshold  = int(os.environ.get("STUCK_THRESHOLD_MINUTES", "20"))
_output_bucket = os.environ.get("S3_OUTPUT_BUCKET", "").strip()

_dynamodb  = boto3.resource("dynamodb", region_name=_region)
_table     = _dynamodb.Table(_table_name)

STUCK_ERROR = (
    "Job was interrupted before completion (Lambda hard-kill: OOM or timeout). "
    "Please resubmit your render."
)


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def purge_expired_renders() -> dict:
    """
    Delete rendered outputs for COMPLETE jobs past their expires_at retention
    window (default 24 hours from completion) and mark the record as purged.

    The job record itself is kept for history/audit — only the S3 output
    object is removed, and `output_purged` is set so the dashboard can stop
    offering a download link.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    response = _table.scan(
        FilterExpression=Attr("status").eq("COMPLETE")
        & Attr("expires_at").lt(now_iso)
        & Attr("output_purged").not_exists()
    )
    expired = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = _table.scan(
            FilterExpression=Attr("status").eq("COMPLETE")
            & Attr("expires_at").lt(now_iso)
            & Attr("output_purged").not_exists(),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        expired.extend(response.get("Items", []))

    purged = []
    if expired:
        s3 = boto3.client("s3", region_name=_region)

    for item in expired:
        job_id = item.get("job_id")
        output_paths = [path for path in (item.get("output_parts") or [item.get("output_path")]) if path]
        if not job_id or not output_paths:
            continue

        try:
            for output_path in output_paths:
                try:
                    bucket, key = _parse_s3_uri(output_path)
                except ValueError:
                    bucket, key = _output_bucket, f"{job_id}/output.mp4"
                s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            print(f"[reaper] Failed to delete expired output part for {job_id}: {exc}")
            continue

        _table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET output_purged = :p, output_purged_at = :t",
            ExpressionAttributeValues={":p": True, ":t": now_iso},
        )
        print(f"[reaper] Purged expired render output part(s): {job_id} (expired {item.get('expires_at')})")
        purged.append(job_id)

    print(f"[reaper] Done. Purged {len(purged)} expired render output(s).")
    return {"purged": purged, "count": len(purged)}


def lambda_handler(event: dict, context) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_threshold)
    cutoff_iso = cutoff.isoformat()

    response = _table.scan(
        FilterExpression=Attr("status").eq("PROCESSING") & Attr("updated_at").lt(cutoff_iso)
    )
    stuck = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = _table.scan(
            FilterExpression=Attr("status").eq("PROCESSING") & Attr("updated_at").lt(cutoff_iso),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        stuck.extend(response.get("Items", []))

    now_iso = datetime.now(timezone.utc).isoformat()
    recovered = []

    for item in stuck:
        job_id = item.get("job_id")
        if not job_id:
            continue

        _table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, #e = :e, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": "FAILED",
                ":e": STUCK_ERROR,
                ":u": now_iso,
            },
        )
        print(f"[reaper] Marked stuck job FAILED: {job_id} (last updated {item.get('updated_at')})")
        recovered.append(job_id)

    print(f"[reaper] Done. Recovered {len(recovered)} stuck job(s).")

    purge_result = purge_expired_renders()

    return {
        "recovered": recovered,
        "count": len(recovered),
        "purged": purge_result["purged"],
        "purged_count": purge_result["count"],
    }
