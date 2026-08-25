"""
routers/jobs.py
---------------
Job submission and status polling endpoints.

Local dev: dispatches via asyncio job_runner (in-memory status store).
AWS prod:  dispatches via SQS, polls status from DynamoDB.

Endpoints:
    POST /render          — Submit a new multicam render job
    GET  /status/{job_id} — Poll the status of a job
    GET  /jobs            — List all jobs (dev only)
"""

import json
import boto3
import os
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from multicam_pipeline.auth import require_auth
from multicam_pipeline.auth import principal_id_from_claims
from pydantic import BaseModel, Field

from multicam_pipeline.job_schema import MulticamJob, CuttingStrategy, EventType, EffectIntensity
from multicam_pipeline.billing_store import has_paid_tier_access
from multicam_pipeline.config import (
    IS_LOCAL, IS_AWS,
    AWS_REGION, SQS_QUEUE_URL, DYNAMODB_TABLE, LOCAL_OUTPUT_DIR,
    DEFAULT_CUT_INTERVAL, DEFAULT_TARGET_WIDTH,
    DEFAULT_TARGET_HEIGHT, DEFAULT_TARGET_FPS,
    S3_OUTPUT_BUCKET,
)
from multicam_pipeline.routers.projects import get_project_record, register_project_job

router = APIRouter(prefix="", tags=["jobs"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RenderRequest(BaseModel):
    """
    Request body for POST /render.

    Attributes:
        files:                   List of file paths or S3 URIs from POST /upload.
        upload_id:               The upload_id from POST /upload.
        cut_interval:            Seconds between angle switches (interval mode).
        target_width:            Output width in pixels.
        target_height:           Output height in pixels.
        target_fps:              Output frame rate.
        cutting_strategy:        local | rekognition | interval.
        event_type:              cheer | sport | concert | dance.
        effect_intensity:        subtle | balanced | cinematic.
        rekognition_sample_rate: Analyze every Nth frame (paid tier).
    """
    files:                   List[str]
    upload_id:               str
    cut_interval:            float = Field(default=DEFAULT_CUT_INTERVAL, gt=0)
    target_width:            int   = Field(default=DEFAULT_TARGET_WIDTH, gt=0)
    target_height:           int   = Field(default=DEFAULT_TARGET_HEIGHT, gt=0)
    target_fps:              int   = Field(default=DEFAULT_TARGET_FPS, gt=0)
    cutting_strategy:        str   = Field(default="local", pattern="^(local|rekognition|interval)$")
    event_type:              str   = Field(default="cheer", pattern="^(cheer|sport|concert|dance)$")
    effect_intensity:        str   = Field(default="balanced", pattern="^(subtle|balanced|cinematic)$")
    transition_style:        str   = Field(default="cut", pattern="^(auto|cut|dissolve|flash|slide|stylized)$")
    rekognition_sample_rate: int   = Field(default=15, ge=1, le=60)
    audio_source_file:       Optional[str] = None
    project_id:              Optional[str] = None


class CostEstimateRequest(BaseModel):
    video_duration_seconds: float = Field(gt=0)
    fps:                    float = Field(default=30.0, gt=0)
    sample_rate:            int   = Field(default=15, ge=1, le=60)
    num_angles:             int   = Field(default=3, ge=2, le=10)


class JobStatusResponse(BaseModel):
    job_id:      str
    status:      str
    output_path: Optional[str] = None
    project_id:  Optional[str] = None
    audio_master_source: Optional[str] = None
    sync_diagnostics: Optional[Dict[str, dict]] = None
    error:       Optional[str] = None
    created_at:  str
    updated_at:  str


# ---------------------------------------------------------------------------
# Local dev dispatch
# ---------------------------------------------------------------------------

async def _submit_local(job: MulticamJob) -> str:
    """Submit job to the asyncio local runner."""
    from multicam_pipeline.job_runner import submit_job
    return await submit_job(job)


def _get_status_local(job_id: str) -> Optional[dict]:
    """Fetch job status from the in-memory store."""
    from multicam_pipeline.job_runner import get_job_status
    return get_job_status(job_id)


def _list_jobs_local() -> list:
    from multicam_pipeline.job_runner import list_jobs
    return list_jobs()


# ---------------------------------------------------------------------------
# AWS prod dispatch
# ---------------------------------------------------------------------------

def _submit_aws(job: MulticamJob) -> str:
    """Send job to SQS queue for Lambda processing."""
    if not SQS_QUEUE_URL:
        raise HTTPException(status_code=500, detail="SQS_QUEUE_URL is not configured.")

    if not DYNAMODB_TABLE:
        raise HTTPException(status_code=500, detail="DYNAMODB_TABLE is not configured.")

    # Persist the pending record before enqueue so status polling works instantly.
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)
    payload = job.to_dict()
    table.put_item(Item=json.loads(json.dumps(payload), parse_float=Decimal))

    sqs = boto3.client("sqs", region_name=AWS_REGION)
    message = {
        "QueueUrl": SQS_QUEUE_URL,
        "MessageBody": json.dumps(payload),
    }

    # FIFO queues require grouping/deduplication, standard queues reject these fields.
    if SQS_QUEUE_URL.lower().endswith(".fifo"):
        message["MessageGroupId"] = os.environ.get("SQS_MESSAGE_GROUP_ID", "multicam")
        message["MessageDeduplicationId"] = job.job_id

    sqs.send_message(**message)
    return job.job_id


def _get_status_aws(job_id: str) -> Optional[dict]:
    """Fetch job status from DynamoDB."""
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table    = dynamodb.Table(DYNAMODB_TABLE)
    response = table.get_item(Key={"job_id": job_id})
    return response.get("Item")


def _list_jobs_aws() -> list:
    """List jobs from DynamoDB so the frontend dashboard keeps working in AWS mode."""
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)
    response = table.scan()
    items = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/render", response_model=JobStatusResponse, status_code=202)
async def submit_render(req: RenderRequest, _claims: dict = Depends(require_auth)):
    """
    Submit a multicam render job.

    Accepts the file list from POST /upload and optional render settings.
    Returns immediately with a job_id and PENDING status — processing
    happens asynchronously in the background.

    Status codes:
        202 Accepted — job submitted successfully
        400 Bad Request — fewer than 2 video files provided
        500 Internal Server Error — dispatch failure
    """
    if len(req.files) < 2:
        raise HTTPException(status_code=400, detail="At least 2 video files are required.")

    if req.audio_source_file and req.audio_source_file not in req.files:
        raise HTTPException(
            status_code=400,
            detail="audio_source_file must match one of the uploaded file references.",
        )

    actor_id = principal_id_from_claims(_claims)

    if req.cutting_strategy == "rekognition":
        if not actor_id or not has_paid_tier_access(actor_id):
            raise HTTPException(
                status_code=402,
                detail="Paid tier required for rekognition strategy. Complete billing checkout first.",
            )

    if req.project_id:
        project = get_project_record(req.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")

        # In production auth mode, only owners can initiate final renders.
        if actor_id and actor_id != project.get("owner_id"):
            raise HTTPException(status_code=403, detail="Only the project owner can start renders.")

        allowed_files = {
            file_ref
            for upload in (project.get("uploads") or [])
            for file_ref in (upload.get("files") or [])
        }
        unknown_files = [f for f in req.files if f not in allowed_files]
        if unknown_files:
            raise HTTPException(
                status_code=400,
                detail="Render files must come from uploads registered to the selected project.",
            )

    # Build output path based on environment
    if IS_LOCAL:
        os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
        output_path = f"{LOCAL_OUTPUT_DIR}/{req.upload_id}/output.mp4"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    else:
        output_path = f"s3://{S3_OUTPUT_BUCKET}/{req.upload_id}/output.mp4"

    job = MulticamJob(
        video_paths              = req.files,
        output_path              = output_path,
        cut_interval             = req.cut_interval,
        target_width             = req.target_width,
        target_height            = req.target_height,
        target_fps               = req.target_fps,
        cutting_strategy         = CuttingStrategy(req.cutting_strategy),
        event_type               = EventType(req.event_type),
        effect_intensity         = EffectIntensity(req.effect_intensity),
        transition_style         = req.transition_style,
        rekognition_sample_rate  = req.rekognition_sample_rate,
        audio_source_override    = req.audio_source_file,
        project_id               = req.project_id,
    )

    if req.project_id:
        register_project_job(req.project_id, job.job_id)


    if IS_LOCAL:
        await _submit_local(job)
    else:
        _submit_aws(job)

    print(f"[jobs] Submitted job {job.job_id} | env={IS_LOCAL and 'local' or 'aws'}")

    return JobStatusResponse(
        job_id      = job.job_id,
        status      = job.status.value,
        output_path = job.output_path,
        project_id  = job.project_id,
        audio_master_source = job.selected_audio_source_path,
        sync_diagnostics = job.sync_diagnostics,
        error       = None,
        created_at  = job.created_at,
        updated_at  = job.updated_at,
    )


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_status(job_id: str, _claims: dict = Depends(require_auth)):
    """
    Poll the current status of a render job.

    Returns the job record including status, output path (when complete),
    and error details (when failed).

    Status codes:
        200 OK        — job found, returns current state
        404 Not Found — job_id does not exist
    """
    data = _get_status_local(job_id) if IS_LOCAL else _get_status_aws(job_id)

    if not data:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    actor_id = principal_id_from_claims(_claims)
    project_id = data.get("project_id")
    if actor_id and project_id:
        project = get_project_record(project_id)
        if not project or actor_id not in (project.get("members") or []):
            raise HTTPException(status_code=403, detail="You are not allowed to view this job.")

    return JobStatusResponse(
        job_id      = data["job_id"],
        status      = data["status"],
        output_path = data.get("output_path"),
        project_id  = data.get("project_id"),
        audio_master_source = data.get("selected_audio_source_path"),
        sync_diagnostics = data.get("sync_diagnostics"),
        error       = data.get("error"),
        created_at  = data["created_at"],
        updated_at  = data["updated_at"],
    )


@router.get("/jobs")
async def list_all_jobs(_claims: dict = Depends(require_auth)):
    """
    List all jobs in the current environment.

    Local dev only — in AWS, use DynamoDB scan or CloudWatch for job history.

    Status codes:
        200 OK                — returns list of job dicts
        501 Not Implemented   — called in AWS mode
    """
    actor_id = principal_id_from_claims(_claims)
    jobs = _list_jobs_aws() if IS_AWS else _list_jobs_local()
    if actor_id:
        visible_jobs = []
        for item in jobs:
            project_id = item.get("project_id")
            if not project_id:
                visible_jobs.append(item)
                continue
            project = get_project_record(project_id)
            if project and actor_id in (project.get("members") or []):
                visible_jobs.append(item)
        jobs = visible_jobs
    return JSONResponse(jobs)


@router.post("/cost-estimate")
async def cost_estimate(req: CostEstimateRequest, _claims: dict = Depends(require_auth)):
    """
    Estimate the AWS Rekognition cost for a given job configuration.

    Powers the frontend cost/quality tradeoff slider. Returns frame counts
    and estimated USD cost so users can make an informed decision before
    submitting a paid-tier render job.

    Status codes:
        200 OK — returns cost breakdown dict
    """
    from multicam_pipeline.ai_cutter import estimate_rekognition_cost
    return estimate_rekognition_cost(
        video_duration_seconds = req.video_duration_seconds,
        fps                    = req.fps,
        sample_rate            = req.sample_rate,
        num_angles             = req.num_angles,
    )
