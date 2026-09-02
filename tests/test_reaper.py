"""
tests/test_reaper.py
--------------------
Unit tests for the stuck-job reaper Lambda handler.
Uses moto to mock DynamoDB — no real AWS calls.
"""

import importlib
from datetime import datetime, timezone, timedelta

import boto3
import pytest

TABLE_NAME = "multicam-jobs"


@pytest.fixture()
def ddb_table(monkeypatch):
    """Provide a mocked DynamoDB table pre-populated with test jobs."""
    moto = pytest.importorskip("moto")
    mock_ddb = moto.mock_aws()
    mock_ddb.start()

    monkeypatch.setenv("DYNAMODB_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")

    client = boto3.resource("dynamodb", region_name="us-east-1")
    table = client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    yield table

    mock_ddb.stop()


def _put(table, job_id: str, status: str, updated_at: str) -> None:
    table.put_item(Item={"job_id": job_id, "status": status, "updated_at": updated_at})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_reaper_marks_stuck_processing_job_failed(ddb_table, monkeypatch):
    monkeypatch.setenv("STUCK_THRESHOLD_MINUTES", "20")

    stuck_at = _iso(_now() - timedelta(minutes=25))
    _put(ddb_table, "stuck-job", "PROCESSING", stuck_at)

    import multicam_pipeline.reaper as reaper
    importlib.reload(reaper)

    result = reaper.lambda_handler({}, None)

    assert "stuck-job" in result["recovered"]
    item = ddb_table.get_item(Key={"job_id": "stuck-job"})["Item"]
    assert item["status"] == "FAILED"
    assert "interrupted" in item["error"].lower() or "hard-kill" in item["error"].lower()


def test_reaper_ignores_recently_started_processing_job(ddb_table, monkeypatch):
    monkeypatch.setenv("STUCK_THRESHOLD_MINUTES", "20")

    recent_at = _iso(_now() - timedelta(minutes=5))
    _put(ddb_table, "fresh-job", "PROCESSING", recent_at)

    import multicam_pipeline.reaper as reaper
    importlib.reload(reaper)

    result = reaper.lambda_handler({}, None)

    assert "fresh-job" not in result["recovered"]
    item = ddb_table.get_item(Key={"job_id": "fresh-job"})["Item"]
    assert item["status"] == "PROCESSING"


def test_reaper_ignores_complete_and_failed_jobs(ddb_table, monkeypatch):
    monkeypatch.setenv("STUCK_THRESHOLD_MINUTES", "20")

    old = _iso(_now() - timedelta(hours=2))
    _put(ddb_table, "done-job", "COMPLETE", old)
    _put(ddb_table, "failed-job", "FAILED", old)

    import multicam_pipeline.reaper as reaper
    importlib.reload(reaper)

    result = reaper.lambda_handler({}, None)

    assert result["count"] == 0
    assert ddb_table.get_item(Key={"job_id": "done-job"})["Item"]["status"] == "COMPLETE"
    assert ddb_table.get_item(Key={"job_id": "failed-job"})["Item"]["status"] == "FAILED"


def test_reaper_recovers_multiple_stuck_jobs(ddb_table, monkeypatch):
    monkeypatch.setenv("STUCK_THRESHOLD_MINUTES", "20")

    old = _iso(_now() - timedelta(minutes=30))
    for i in range(3):
        _put(ddb_table, f"stuck-{i}", "PROCESSING", old)

    import multicam_pipeline.reaper as reaper
    importlib.reload(reaper)

    result = reaper.lambda_handler({}, None)

    assert result["count"] == 3
    assert set(result["recovered"]) == {"stuck-0", "stuck-1", "stuck-2"}
