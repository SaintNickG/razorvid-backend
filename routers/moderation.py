"""Project safety reports and scoped member blocking."""

import os
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from multicam_pipeline.auth import principal_id_from_claims, require_admin, require_auth
from multicam_pipeline.config import AWS_REGION, IS_AWS
from multicam_pipeline.routers import projects as project_store

router = APIRouter(tags=["moderation"])
SAFETY_REPORTS_TABLE = os.environ.get("SAFETY_REPORTS_TABLE", "multicam-safety-reports").strip()
_local_reports: list[dict] = []
REPORT_REASONS = {"inappropriate", "harassment", "spam", "rights_violation", "other"}


class SafetyReportRequest(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=256)
    upload_id: str | None = Field(default=None, max_length=128)
    reason: str
    details: str | None = Field(default=None, max_length=2000)


class SafetyReportStatusRequest(BaseModel):
    status: str


def _save_report(report: dict) -> None:
    if IS_AWS:
        if not SAFETY_REPORTS_TABLE:
            raise HTTPException(status_code=503, detail="Safety reporting is not configured.")
        boto3.resource("dynamodb", region_name=AWS_REGION).Table(SAFETY_REPORTS_TABLE).put_item(Item=report)
        return
    _local_reports.append(report)


@router.post("/projects/{project_id}/reports", status_code=201)
async def report_project_content(project_id: str, request: SafetyReportRequest, claims: dict = Depends(require_auth)):
    project_store._refresh_store()
    reporter_id = principal_id_from_claims(claims)
    if not reporter_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required.")
    if request.reason not in REPORT_REASONS:
        raise HTTPException(status_code=422, detail="Choose a valid report reason.")

    project = project_store._projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if reporter_id not in (project.get("members") or []):
        raise HTTPException(status_code=403, detail="Only project members can report project content.")
    if request.target_user_id == reporter_id:
        raise HTTPException(status_code=422, detail="You cannot report your own contribution.")

    target_uploads = [upload for upload in (project.get("uploads") or []) if upload.get("user_id") == request.target_user_id]
    if not target_uploads:
        raise HTTPException(status_code=404, detail="Contributor not found in this project.")
    if request.upload_id and not any(upload.get("upload_id") == request.upload_id for upload in target_uploads):
        raise HTTPException(status_code=404, detail="Upload not found for this contributor.")

    report = {
        "report_id": str(uuid.uuid4()),
        "project_id": project_id,
        "project_name": str(project.get("name") or "")[:200],
        "reporter_id": reporter_id,
        "target_user_id": request.target_user_id,
        "upload_id": request.upload_id or "",
        "reason": request.reason,
        "details": (request.details or "").strip(),
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_report(report)
    return {"report_id": report["report_id"], "status": report["status"]}


@router.post("/projects/{project_id}/blocks/{target_user_id}", status_code=204)
async def block_project_member(project_id: str, target_user_id: str, claims: dict = Depends(require_auth)):
    project_store._refresh_store()
    actor_id = principal_id_from_claims(claims)
    if not actor_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required.")
    project = project_store._projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if actor_id not in (project.get("members") or []):
        raise HTTPException(status_code=403, detail="Only project members can block another member.")
    if target_user_id == actor_id or target_user_id == project.get("owner_id"):
        raise HTTPException(status_code=422, detail="You cannot block yourself or the project owner.")
    if target_user_id not in (project.get("members") or []):
        raise HTTPException(status_code=404, detail="Member not found in this project.")

    blocked_users = project.setdefault("blocked_users", [])
    if target_user_id not in blocked_users:
        blocked_users.append(target_user_id)
    project["members"] = [member for member in project.get("members", []) if member != target_user_id]
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    if project_store._use_dynamodb():
        project_store._ddb_table().put_item(Item={
            "pk": f"PROJECT#{project_id}",
            "sk": "PROFILE",
            "item_type": "project",
            "project_id": project_id,
            "invite_code": str(project.get("invite_code", "")).upper(),
            "updated_at": project["updated_at"],
            "project": project,
        })
    else:
        project_store._persist_store()


@router.get("/admin/safety-reports")
async def list_safety_reports(limit: int = Query(default=100, ge=1, le=500), _claims: dict = Depends(require_admin)):
    if IS_AWS:
        if not SAFETY_REPORTS_TABLE:
            raise HTTPException(status_code=503, detail="Safety reporting is not configured.")
        response = boto3.resource("dynamodb", region_name=AWS_REGION).Table(SAFETY_REPORTS_TABLE).scan(Limit=limit)
        reports = response.get("Items", [])
    else:
        reports = list(_local_reports)
    reports.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"reports": reports[:limit]}


@router.patch("/admin/safety-reports/{report_id}")
async def update_safety_report_status(
    report_id: str,
    request: SafetyReportStatusRequest,
    _claims: dict = Depends(require_admin),
):
    if request.status not in {"open", "reviewed", "actioned"}:
        raise HTTPException(status_code=422, detail="Status must be open, reviewed, or actioned.")
    updated_at = datetime.now(timezone.utc).isoformat()
    if IS_AWS:
        if not SAFETY_REPORTS_TABLE:
            raise HTTPException(status_code=503, detail="Safety reporting is not configured.")
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(SAFETY_REPORTS_TABLE)
        response = table.update_item(
            Key={"report_id": report_id},
            UpdateExpression="SET #status = :status, updated_at = :updated_at",
            ConditionExpression="attribute_exists(report_id)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": request.status, ":updated_at": updated_at},
            ReturnValues="ALL_NEW",
        )
        return {"report": response["Attributes"]}

    report = next((item for item in _local_reports if item.get("report_id") == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Safety report not found.")
    report["status"] = request.status
    report["updated_at"] = updated_at
    return {"report": report}