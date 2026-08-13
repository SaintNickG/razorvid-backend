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
