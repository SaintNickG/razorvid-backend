"""
reaper.py
---------
Stuck-job reaper — Lambda function triggered by EventBridge on a schedule.

Scans DynamoDB for jobs that have been in PROCESSING status longer than
STUCK_THRESHOLD_MINUTES and marks them FAILED with a descriptive error.

This recovers jobs killed by Lambda hard-kills (OOM, timeout) where the
Python exception handler never ran and DynamoDB was never updated.

Environment variables:
    DYNAMODB_TABLE          — DynamoDB table name (default: multicam-jobs)
    AWS_REGION              — AWS region (auto-set by Lambda runtime)
    STUCK_THRESHOLD_MINUTES — Minutes before a PROCESSING job is considered
                              stuck (default: 20)
"""

import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

_region    = os.environ.get("AWS_REGION", "us-east-1")
_table_name = os.environ.get("DYNAMODB_TABLE", "multicam-jobs")
_threshold  = int(os.environ.get("STUCK_THRESHOLD_MINUTES", "20"))

_dynamodb  = boto3.resource("dynamodb", region_name=_region)
_table     = _dynamodb.Table(_table_name)

STUCK_ERROR = (
    "Job was interrupted before completion (Lambda hard-kill: OOM or timeout). "
    "Please resubmit your render."
)


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
    return {"recovered": recovered, "count": len(recovered)}
