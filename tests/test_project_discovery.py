import asyncio

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from multicam_pipeline.routers import projects


def _request() -> Request:
    return Request({"type": "http", "scheme": "http", "server": ("testserver", 80), "path": "/projects", "headers": []})


def test_discovery_returns_only_safe_match_fields_and_owner_can_approve(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCOVERY_TOKEN_SECRET", "test-discovery-secret")
    monkeypatch.setattr(projects, "PROJECT_STORE_PATH", tmp_path / "projects.json")
    projects._projects.clear()
    projects._invite_index.clear()
    request = _request()
    now = datetime.now(timezone.utc)

    created = asyncio.run(projects.create_project(
        type("Create", (), {"name": "Private owner project", "event_type": "concert", "owner_id": "owner"})(),
        request,
        {"sub": "owner"},
    ))
    project_id = created["project_id"]
    asyncio.run(projects.update_discovery_settings(
        project_id,
        projects.DiscoverySettingsRequest(
            discoverable=True,
            public_label="Lincoln High Spring Concert",
            event_starts_at=now,
            latitude=42.3601,
            longitude=-71.0589,
            join_request_notifications=False,
        ),
        request,
        {"sub": "owner"},
    ))

    results = asyncio.run(projects.discover_projects(
        projects.DiscoverySearchRequest(recorded_at=now + timedelta(minutes=10), latitude=42.3602, longitude=-71.0589),
        {"sub": "requester"},
    ))
    assert len(results["matches"]) == 1
    match = results["matches"][0]
    assert set(match) == {"match_token", "label", "event_type"}
    assert match["label"] == "Lincoln High Spring Concert"
    assert project_id not in str(match)

    created_request = asyncio.run(projects.create_join_request(
        projects.JoinRequestCreateRequest(match_token=match["match_token"], display_name="Camera Two"),
        {"sub": "requester"},
    ))
    assert created_request["status"] == "pending"

    asyncio.run(projects.decide_join_request(
        project_id,
        created_request["request_id"],
        projects.JoinRequestDecisionRequest(decision="approved"),
        request,
        {"sub": "owner"},
    ))
    assert "requester" in projects._projects[project_id]["members"]


def test_discovery_excludes_out_of_window_and_unauthorized_owner_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCOVERY_TOKEN_SECRET", "test-discovery-secret")
    monkeypatch.setattr(projects, "PROJECT_STORE_PATH", tmp_path / "projects.json")
    projects._projects.clear()
    projects._invite_index.clear()
    request = _request()
    now = datetime.now(timezone.utc)
    created = asyncio.run(projects.create_project(
        type("Create", (), {"name": "Private owner project", "event_type": "concert", "owner_id": "owner"})(),
        request,
        {"sub": "owner"},
    ))

    with pytest.raises(HTTPException, match="Only the project owner"):
        asyncio.run(projects.update_discovery_settings(
            created["project_id"],
            projects.DiscoverySettingsRequest(discoverable=False),
            request,
            {"sub": "other-user"},
        ))

    results = asyncio.run(projects.discover_projects(
        projects.DiscoverySearchRequest(recorded_at=now, latitude=42.3601, longitude=-71.0589),
        {"sub": "requester"},
    ))
    assert results == {"matches": []}