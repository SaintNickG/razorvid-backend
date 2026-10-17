import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from multicam_pipeline.routers import moderation, projects


def _request() -> Request:
    return Request({"type": "http", "scheme": "http", "server": ("testserver", 80), "path": "/projects", "headers": []})


def _seed_project(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "PROJECT_STORE_PATH", tmp_path / "projects.json")
    projects._projects.clear()
    projects._invite_index.clear()
    moderation._local_reports.clear()
    return asyncio.run(projects.create_project(
        type("Create", (), {"name": "Safety test", "event_type": "concert", "owner_id": "owner"})(),
        _request(),
        {"sub": "owner"},
    ))


def test_report_is_private_and_requires_project_membership(monkeypatch, tmp_path):
    project = _seed_project(monkeypatch, tmp_path)
    record = projects._projects[project["project_id"]]
    record["members"].append("contributor")
    record["uploads"].append({
        "upload_id": "upload-1",
        "user_id": "contributor",
        "user_name": "Contributor",
        "files": ["s3://input/upload-1/video.mp4"],
    })
    projects._persist_store()

    result = asyncio.run(moderation.report_project_content(
        project["project_id"],
        moderation.SafetyReportRequest(
            target_user_id="contributor",
            upload_id="upload-1",
            reason="rights_violation",
            details="I do not recognize consent for this clip.",
        ),
        {"sub": "owner"},
    ))

    assert result["status"] == "open"
    assert len(moderation._local_reports) == 1
    assert "safety_reports" not in record
    with pytest.raises(HTTPException) as exc:
        asyncio.run(moderation.report_project_content(
            project["project_id"],
            moderation.SafetyReportRequest(target_user_id="contributor", upload_id="upload-1", reason="spam"),
            {"sub": "outsider"},
        ))
    assert exc.value.status_code == 403


def test_block_removes_project_access_and_prevents_rejoin(monkeypatch, tmp_path):
    project = _seed_project(monkeypatch, tmp_path)
    record = projects._projects[project["project_id"]]
    record["members"].append("contributor")
    record["uploads"].append({
        "upload_id": "upload-1",
        "user_id": "contributor",
        "user_name": "Contributor",
        "files": ["s3://input/upload-1/video.mp4"],
    })
    projects._persist_store()

    asyncio.run(moderation.block_project_member(project["project_id"], "contributor", {"sub": "owner"}))

    record = projects._projects[project["project_id"]]
    assert "contributor" not in record["members"]
    assert record["blocked_users"] == ["contributor"]
    visible = projects._project_response(record, _request(), "owner")
    assert visible["uploads"] == []
    assert "blocked_users" not in visible

    with pytest.raises(HTTPException) as exc:
        asyncio.run(projects.join_project(
            type("Join", (), {"invite_code": project["invite_code"], "user_id": "contributor"})(),
            _request(),
            {"sub": "contributor"},
        ))
    assert exc.value.status_code == 403


def test_members_cannot_block_themselves_or_project_owner(monkeypatch, tmp_path):
    project = _seed_project(monkeypatch, tmp_path)
    record = projects._projects[project["project_id"]]
    record["members"].append("contributor")
    projects._persist_store()

    for target in ("owner", "contributor"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(moderation.block_project_member(project["project_id"], target, {"sub": "contributor"}))
        assert exc.value.status_code == 422


def test_admin_can_review_private_safety_report(monkeypatch, tmp_path):
    project = _seed_project(monkeypatch, tmp_path)
    record = projects._projects[project["project_id"]]
    record["members"].append("contributor")
    record["uploads"].append({"upload_id": "upload-1", "user_id": "contributor", "files": ["local-video.mp4"]})
    projects._persist_store()
    report = asyncio.run(moderation.report_project_content(
        project["project_id"],
        moderation.SafetyReportRequest(target_user_id="contributor", upload_id="upload-1", reason="spam"),
        {"sub": "owner"},
    ))

    listed = asyncio.run(moderation.list_safety_reports(limit=100, _claims={"sub": "admin"}))
    assert listed["reports"][0]["report_id"] == report["report_id"]
    updated = asyncio.run(moderation.update_safety_report_status(
        report["report_id"],
        moderation.SafetyReportStatusRequest(status="reviewed"),
        _claims={"sub": "admin"},
    ))
    assert updated["report"]["status"] == "reviewed"
