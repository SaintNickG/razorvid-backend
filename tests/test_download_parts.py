import asyncio
from pathlib import Path
from starlette.requests import Request

import multicam_pipeline.job_runner as job_runner
import multicam_pipeline.routers.download as download_router


def _request() -> Request:
    return Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/download/job-1/parts",
        "headers": [],
    })


def test_local_download_parts_returns_each_numbered_output(monkeypatch):
    monkeypatch.setattr(download_router, "IS_LOCAL", True)
    monkeypatch.setattr(job_runner, "get_job_status", lambda _job_id: {
        "job_id": "job-1",
        "status": "COMPLETE",
        "project_id": None,
        "output_path": "/tmp/output_part_01_of_02.mp4",
        "output_parts": [
            "/tmp/output_part_01_of_02.mp4",
            "/tmp/output_part_02_of_02.mp4",
        ],
    })

    response = asyncio.run(download_router.get_download_parts("job-1", _request(), _claims={}))

    assert response["parts"] == [
        {
            "part_index": 1,
            "total_parts": 2,
            "filename": "output_part_01_of_02.mp4",
            "url": "http://testserver/download/job-1?part=1",
        },
        {
            "part_index": 2,
            "total_parts": 2,
            "filename": "output_part_02_of_02.mp4",
            "url": "http://testserver/download/job-1?part=2",
        },
    ]


def test_local_legacy_job_has_one_download_part(monkeypatch):
    monkeypatch.setattr(download_router, "IS_LOCAL", True)
    monkeypatch.setattr(job_runner, "get_job_status", lambda _job_id: {
        "job_id": "job-1",
        "status": "COMPLETE",
        "project_id": None,
        "output_path": "/tmp/output.mp4",
    })

    response = asyncio.run(download_router.get_download_parts("job-1", _request(), _claims={}))

    assert len(response["parts"]) == 1
    assert response["parts"][0]["filename"] == "output.mp4"
    assert response["parts"][0]["url"] == "http://testserver/download/job-1?part=1"


def test_local_download_url_selects_requested_part(monkeypatch):
    monkeypatch.setattr(download_router, "IS_LOCAL", True)
    monkeypatch.setattr(job_runner, "get_job_status", lambda _job_id: {
        "job_id": "job-1",
        "status": "COMPLETE",
        "project_id": None,
        "output_parts": ["/tmp/part-1.mp4", "/tmp/part-2.mp4"],
    })

    response = asyncio.run(download_router.get_download_url("job-1", _request(), part=2, _claims={}))

    assert response["part_index"] == 2
    assert response["total_parts"] == 2
    assert response["url"] == "http://testserver/download/job-1?part=2"


def test_local_download_streams_requested_part(monkeypatch, tmp_path: Path):
    part_paths = [tmp_path / "part-1.mp4", tmp_path / "part-2.mp4"]
    for path in part_paths:
        path.write_bytes(b"part")
    monkeypatch.setattr(download_router, "IS_LOCAL", True)
    monkeypatch.setattr(job_runner, "get_job_status", lambda _job_id: {
        "job_id": "job-1",
        "status": "COMPLETE",
        "project_id": None,
        "output_path": str(part_paths[0]),
        "output_parts": [str(path) for path in part_paths],
    })

    response = asyncio.run(download_router.download_output("job-1", part=2, _claims={}))

    assert response.path == str(part_paths[1])
    assert response.filename == "multicam_job-1_part_02_of_02.mp4"
