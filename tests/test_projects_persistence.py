import asyncio
import importlib
from starlette.requests import Request


def test_projects_store_is_initialized_and_persisted_across_module_reload():
    import multicam_pipeline.routers.projects as projects_router

    projects_router._projects.clear()
    projects_router._invite_index.clear()

    req = Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/projects",
            "headers": [],
        }
    )

    created = asyncio.run(
        projects_router.create_project(
            type(
                "Req",
                (),
                {"name": "Demo", "event_type": "cheer", "owner_id": "u1"},
            )(),
            req,
            {"sub": "u1"},
        )
    )

    assert created["project_id"] in projects_router._projects
    assert projects_router._invite_index[created["invite_code"]] == created["project_id"]

    importlib.reload(projects_router)

    assert created["project_id"] in projects_router._projects
    assert projects_router._invite_index[created["invite_code"]] == created["project_id"]


def test_guest_contact_email_is_only_returned_to_project_owner():
    import multicam_pipeline.routers.projects as projects_router

    project = {
        "project_id": "project-1",
        "owner_id": "owner-1",
        "invite_code": "ABC123",
        "members": ["owner-1", "guest-1", "member-1"],
        "uploads": [{
            "upload_id": "upload-1",
            "user_id": "guest-1",
            "user_name": "Guest",
            "guest_contact_email": "guest@example.com",
            "files": ["angle.mp4"],
        }],
    }
    request = Request({
        "type": "http",
        "scheme": "https",
        "server": ("example.com", 443),
        "path": "/projects/project-1",
        "headers": [],
    })

    owner_view = projects_router._project_response(project, request, "owner-1")
    member_view = projects_router._project_response(project, request, "member-1")
    anonymous_view = projects_router._project_response(project, request)

    assert owner_view["uploads"][0]["guest_contact_email"] == "guest@example.com"
    assert "guest_contact_email" not in member_view["uploads"][0]
    assert "guest_contact_email" not in anonymous_view["uploads"][0]


def test_guest_angle_filenames_are_numbered_per_contributor():
    import multicam_pipeline.routers.projects as projects_router

    project = {
        "uploads": [
            {"user_name": "Camera Operator", "files": ["Camera Operator 1.mp4", "Camera Operator 2.mov"]},
            {"user_name": "Another Person", "files": ["Another Person 1.mp4"]},
        ],
    }

    next_number = projects_router._next_contributor_angle_number(project, " Camera   Operator ")
    filename = projects_router._contributor_filename("original.mov", "Camera/Operator", next_number)

    assert next_number == 3
    assert filename == "Camera_Operator 3.mov"
