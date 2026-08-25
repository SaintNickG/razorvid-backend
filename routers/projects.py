"""
routers/projects.py
-------------------
Project management endpoints.

A Project groups multiple uploads (from different users/cameras) under one
shoot. Any user with the invite code can add their angle. The project owner
triggers the final render.

Endpoints:
    POST /projects                        — Create a new project
    GET  /projects                        — List all projects (dev)
    GET  /projects/{project_id}           — Get a single project
    POST /projects/join                   — Join a project by invite code
    POST /projects/{project_id}/uploads   — Register an upload to a project
"""

import json
import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import boto3
import qrcode
from qrcode.image.svg import SvgImage

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel

from multicam_pipeline.auth import principal_id_from_claims, require_auth, resolve_actor_id
from multicam_pipeline.config import AWS_REGION, EVENT_TYPES, IS_AWS
from multicam_pipeline.routers.upload import _save_local, _save_s3, _validate_extension

router = APIRouter(prefix="/projects", tags=["projects"])

# ---------------------------------------------------------------------------
# Persistent project store.
#
# The previous in-memory dict disappeared on every restart. This keeps a
# JSON-backed copy on disk so ECS redeploys and local restarts preserve
# projects until the backing file is explicitly removed.
# ---------------------------------------------------------------------------

PROJECT_STORE_PATH = Path(os.environ.get("PROJECT_STORE_PATH", "/tmp/multicam/projects.json"))
PROJECTS_DYNAMODB_TABLE = os.environ.get("PROJECTS_DYNAMODB_TABLE", "").strip()


def _use_dynamodb() -> bool:
    return bool(IS_AWS and PROJECTS_DYNAMODB_TABLE)


def _ddb_table():
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return dynamodb.Table(PROJECTS_DYNAMODB_TABLE)


def _ensure_store_file() -> None:
    if _use_dynamodb():
        return
    PROJECT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PROJECT_STORE_PATH.exists():
        PROJECT_STORE_PATH.write_text("{\"projects\": {}, \"invite_index\": {}}", encoding="utf-8")


def _load_store() -> tuple[Dict[str, dict], Dict[str, str]]:
    if _use_dynamodb():
        projects: Dict[str, dict] = {}
        invite_index: Dict[str, str] = {}

        table = _ddb_table()
        response = table.scan()
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        for item in items:
            item_type = item.get("item_type")
            if item_type == "project":
                project = item.get("project") or {}
                project_id = project.get("project_id")
                invite_code = str(project.get("invite_code") or "").upper()
                if project_id:
                    projects[project_id] = project
                if project_id and invite_code:
                    invite_index[invite_code] = project_id
            elif item_type == "invite_map":
                invite_code = str(item.get("invite_code") or "").upper()
                project_id = item.get("project_id")
                if invite_code and project_id:
                    invite_index[invite_code] = project_id

        return projects, invite_index

    _ensure_store_file()
    with PROJECT_STORE_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    projects = payload.get("projects", {}) or {}
    invite_index = payload.get("invite_index", {}) or {}
    return projects, invite_index


def _save_store(projects: Dict[str, dict], invite_index: Dict[str, str]) -> None:
    if _use_dynamodb():
        table = _ddb_table()
        with table.batch_writer() as batch:
            for project_id, project in projects.items():
                batch.put_item(
                    Item={
                        "pk": f"PROJECT#{project_id}",
                        "sk": "PROFILE",
                        "item_type": "project",
                        "project_id": project_id,
                        "invite_code": str(project.get("invite_code", "")).upper(),
                        "updated_at": project.get("updated_at") or _now(),
                        "project": project,
                    }
                )

            for invite_code, project_id in invite_index.items():
                batch.put_item(
                    Item={
                        "pk": f"INVITE#{invite_code.upper()}",
                        "sk": "MAP",
                        "item_type": "invite_map",
                        "invite_code": invite_code.upper(),
                        "project_id": project_id,
                    }
                )
        return

    _ensure_store_file()
    with PROJECT_STORE_PATH.open("w", encoding="utf-8") as handle:
        json.dump({"projects": projects, "invite_index": invite_index}, handle, indent=2)


_projects: Dict[str, dict]
_invite_index: Dict[str, str]
_projects, _invite_index = _load_store()


def _refresh_store() -> None:
    """Reload shared project state so every ECS task sees current DynamoDB data."""
    global _projects, _invite_index
    _projects, _invite_index = _load_store()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name:       str
    event_type: str = "cheer"
    owner_id:   Optional[str] = None


class JoinProjectRequest(BaseModel):
    invite_code: str
    user_id:     Optional[str] = None


class AddUploadRequest(BaseModel):
    upload_id: str
    user_id:   Optional[str] = None
    user_name: Optional[str] = None
    files:     List[str]
    terms_accepted: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_code() -> str:
    """6-char uppercase invite code."""
    return uuid.uuid4().hex[:6].upper()


def _persist_store() -> None:
    _save_store(_projects, _invite_index)


def _base_url(request: Request) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _invite_url(invite_code: str, request: Request) -> str:
    return f"{_base_url(request)}/?invite={invite_code.upper()}"


def _project_response(project: dict, request: Request) -> dict:
    result = dict(project)
    result["invite_url"] = _invite_url(project["invite_code"], request)
    return result


def _ensure_owner(project: dict, actor_id: str) -> None:
    if actor_id != project.get("owner_id"):
        raise HTTPException(status_code=403, detail="Only the project owner can access invite QR.")


def _ensure_member(project: dict, actor_id: str) -> None:
    if actor_id not in (project.get("members") or []):
        raise HTTPException(status_code=403, detail="You are not a member of this project.")


def get_project_record(project_id: str) -> Optional[dict]:
    """Shared helper for non-router modules to access project data."""
    return _projects.get(project_id)


def record_render_contributions(project_id: str, render_id: str, segments: List[Any]) -> None:
    """
    Persist contribution durations for a completed render.

    Since this codebase currently uses a JSON-backed project store instead of an
    ORM table layer, we compute a per-uploader duration summary from the timeline
    segments and store it under the project record.
    """
    project = _projects.get(project_id)
    if not project:
        return

    uploads = project.get("uploads", []) or []
    contributor_names = project.setdefault("contributor_names", {})

    file_to_user: Dict[str, str] = {}
    filename_to_user: Dict[str, str] = {}
    for entry in uploads:
        user_id = entry.get("user_id")
        if not user_id:
            continue
        for file_ref in entry.get("files", []) or []:
            file_to_user[file_ref] = user_id
            filename_to_user[os.path.basename(file_ref)] = user_id

    duration_by_user: Dict[str, float] = {}
    total_duration = 0.0

    for seg in segments:
        start_time = float(getattr(seg, "start_time", 0.0))
        end_time = float(getattr(seg, "end_time", 0.0))
        source_video_path = getattr(seg, "source_video_path", "")

        seg_duration = max(0.0, end_time - start_time)
        if seg_duration <= 0.0:
            continue

        user_id = file_to_user.get(
            source_video_path,
            filename_to_user.get(os.path.basename(source_video_path), "unknown"),
        )
        duration_by_user[user_id] = duration_by_user.get(user_id, 0.0) + seg_duration
        total_duration += seg_duration

    if total_duration <= 0.0:
        return

    render_store = project.setdefault("render_contributions", {})
    render_store[render_id] = {
        "total_duration": total_duration,
        "duration_by_user": duration_by_user,
        "contributor_names": contributor_names,
        "updated_at": _now(),
    }
    project["updated_at"] = _now()
    _persist_store()


def get_render_contributions(project_id: str, render_id: str) -> Optional[dict]:
    project = _projects.get(project_id)
    if not project:
        return None
    render_store = project.get("render_contributions", {}) or {}
    return render_store.get(render_id)


def register_project_job(project_id: str, job_id: str) -> None:
    """Attach a submitted render job to a project and persist the store."""
    project = _projects.get(project_id)
    if not project:
        return
    job_ids = project.setdefault("job_ids", [])
    if job_id not in job_ids:
        job_ids.append(job_id)
        project["updated_at"] = _now()
        _persist_store()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_project(req: CreateProjectRequest, request: Request, _claims: dict = Depends(require_auth)):
    """Create a new project and return its invite code."""
    _refresh_store()
    if req.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported event type. Choose one of: {', '.join(EVENT_TYPES)}")
    owner_id = resolve_actor_id(_claims, req.owner_id)
    project_id  = str(uuid.uuid4())
    invite_code = _short_code()

    project = {
        "project_id":  project_id,
        "name":        req.name,
        "event_type":  req.event_type,
        "owner_id":    owner_id,
        "invite_code": invite_code,
        "members":     [owner_id],
        "uploads":     [],          # list of {upload_id, user_id, files}
        "contributor_names": {owner_id: owner_id},
        "render_contributions": {},
        "job_ids":     [],
        "created_at":  _now(),
        "updated_at":  _now(),
    }

    _projects[project_id]      = project
    _invite_index[invite_code] = project_id
    _persist_store()

    return _project_response(project, request)


@router.get("")
async def list_projects(request: Request, _claims: dict = Depends(require_auth)):
    _refresh_store()
    actor_id = principal_id_from_claims(_claims)
    if actor_id:
        return [_project_response(p, request) for p in _projects.values() if actor_id in (p.get("members") or [])]
    return [_project_response(p, request) for p in _projects.values()]


def _guest_project_response(project: dict) -> dict:
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "event_type": project["event_type"],
        "member_count": len(project.get("members") or []),
        "angle_count": sum(len(upload.get("files") or []) for upload in project.get("uploads") or []),
    }


def _project_for_invite(invite_code: str) -> dict:
    project_id = _invite_index.get(invite_code.strip().upper())
    if not project_id:
        raise HTTPException(status_code=404, detail="Invalid invite code.")
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.get("/share/{invite_code}")
async def get_guest_project(invite_code: str):
    """Return limited project details for a public guest upload link."""
    _refresh_store()
    return _guest_project_response(_project_for_invite(invite_code))


@router.post("/share/{invite_code}/uploads")
async def add_guest_upload(
    invite_code: str,
    file: UploadFile = File(...),
    guest_name: Optional[str] = Form(None),
    terms_accepted: bool = Form(False),
):
    """Accept one guest angle using the invite code as the upload capability."""
    _refresh_store()
    if not terms_accepted:
        raise HTTPException(status_code=400, detail="Terms acceptance is required before uploading.")
    _validate_extension(file.filename or "")

    project = _project_for_invite(invite_code)
    guest_id = f"guest:{uuid.uuid4()}"
    upload_id = str(uuid.uuid4())
    if IS_AWS:
        file_ref = await _save_s3(file, upload_id)
    else:
        file_ref = await _save_local(file, upload_id)

    project.setdefault("members", []).append(guest_id)
    project.setdefault("contributor_names", {})[guest_id] = guest_name or "Guest"
    project.setdefault("uploads", []).append({
        "upload_id": upload_id,
        "user_id": guest_id,
        "user_name": guest_name or "Guest",
        "files": [file_ref],
        "added_at": _now(),
    })
    project["updated_at"] = _now()
    _persist_store()
    return _guest_project_response(project)


@router.get("/invite/{invite_code}")
async def resolve_invite(invite_code: str, request: Request, _claims: dict = Depends(require_auth)):
    _refresh_store()
    project_id = _invite_index.get(invite_code.upper())
    if not project_id:
        raise HTTPException(status_code=404, detail="Invalid invite code.")

    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    return _project_response(project, request)


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request, _claims: dict = Depends(require_auth)):
    _refresh_store()
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    actor_id = principal_id_from_claims(_claims)
    if actor_id:
        _ensure_member(project, actor_id)
    return _project_response(project, request)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: str, _claims: dict = Depends(require_auth)):
    """Delete a project and all of its project metadata."""
    _refresh_store()
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    actor_id = resolve_actor_id(_claims, project.get("owner_id"))
    _ensure_owner(project, actor_id)
    _projects.pop(project_id, None)
    invite_code = str(project.get("invite_code", "")).upper()
    _invite_index.pop(invite_code, None)

    if _use_dynamodb():
        table = _ddb_table()
        table.delete_item(Key={"pk": f"PROJECT#{project_id}", "sk": "PROFILE"})
        if invite_code:
            table.delete_item(Key={"pk": f"INVITE#{invite_code}", "sk": "MAP"})

    _persist_store()


@router.get("/{project_id}/invite")
async def get_project_invite(project_id: str, request: Request, owner_id: Optional[str] = None, _claims: dict = Depends(require_auth)):
    _refresh_store()
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    actor_id = resolve_actor_id(_claims, owner_id)
    _ensure_owner(project, actor_id)

    invite_url = _invite_url(project["invite_code"], request)
    return {
        "project_id": project_id,
        "invite_code": project["invite_code"],
        "invite_url": invite_url,
        "invite_qr_url": f"{_base_url(request)}/projects/{project_id}/invite-qr",
    }


@router.get("/{project_id}/invite-qr")
async def get_project_invite_qr(project_id: str, request: Request, owner_id: Optional[str] = None, _claims: dict = Depends(require_auth)):
    _refresh_store()
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    actor_id = resolve_actor_id(_claims, owner_id)
    _ensure_owner(project, actor_id)

    invite_url = _invite_url(project["invite_code"], request)
    image = qrcode.make(invite_url, image_factory=SvgImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(content=buffer.getvalue(), media_type="image/svg+xml")


@router.post("/join")
async def join_project(req: JoinProjectRequest, request: Request, _claims: dict = Depends(require_auth)):
    """Join a project using its 6-char invite code."""
    _refresh_store()
    actor_id = resolve_actor_id(_claims, req.user_id)
    project_id = _invite_index.get(req.invite_code.upper())
    if not project_id:
        raise HTTPException(status_code=404, detail="Invalid invite code.")

    project = _projects[project_id]
    if actor_id not in project["members"]:
        project["members"].append(actor_id)
        contributor_names = project.setdefault("contributor_names", {})
        contributor_names.setdefault(actor_id, actor_id)
        project["updated_at"] = _now()

    _persist_store()

    return _project_response(project, request)


@router.post("/{project_id}/uploads", status_code=201)
async def add_upload(project_id: str, req: AddUploadRequest, request: Request, _claims: dict = Depends(require_auth)):
    """Register a user's uploaded files against a project."""
    _refresh_store()
    if not req.terms_accepted:
        raise HTTPException(status_code=400, detail="Terms acceptance is required before uploading.")
    actor_id = resolve_actor_id(_claims, req.user_id)
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    _ensure_member(project, actor_id)

    entry = {
        "upload_id": req.upload_id,
        "user_id":   actor_id,
        "user_name": req.user_name,
        "files":     req.files,
        "added_at":  _now(),
    }
    project["uploads"].append(entry)
    contributor_names = project.setdefault("contributor_names", {})
    contributor_names[actor_id] = req.user_name or actor_id
    project["updated_at"] = _now()
    _persist_store()

    return _project_response(project, request)
