"""
job_runner.py
-------------
Local asyncio-based job runner for development.

Runs the full multicam pipeline as a non-blocking background task so the
web server (e.g. FastAPI) can return a job_id immediately while processing
continues in the background. Job status is tracked in an in-memory dict.

Usage with FastAPI:
    from multicam_pipeline.job_runner import submit_job, get_job_status
    from multicam_pipeline.job_schema import MulticamJob

    @app.post("/render")
    async def render_endpoint(video_paths: List[str]):
        job = MulticamJob(video_paths=video_paths, output_path="output/out.mp4")
        await submit_job(job)
        return {"job_id": job.job_id}

    @app.get("/status/{job_id}")
    async def status_endpoint(job_id: str):
        return get_job_status(job_id)

Dependencies:
    pip install fastapi uvicorn  # only needed if using with FastAPI
"""

import asyncio
import traceback
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, Optional

from multicam_pipeline.job_schema import MulticamJob, JobStatus


# ---------------------------------------------------------------------------
# In-memory job status store
# ---------------------------------------------------------------------------
# In local dev this is sufficient. In production this is replaced by DynamoDB.
# Keys are job_id strings; values are MulticamJob instances.

_job_store: Dict[str, MulticamJob] = {}

# ProcessPoolExecutor lets us run the CPU-heavy pipeline (FFmpeg, GCC-PHAT)
# in a separate process without blocking the asyncio event loop.
_executor = ProcessPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Core pipeline runner (runs in a subprocess worker)
# ---------------------------------------------------------------------------

def _run_pipeline_sync(job_dict: dict) -> tuple[str, str]:
    """
    Execute the full multicam pipeline synchronously.

    This function runs inside a ProcessPoolExecutor worker so it must be
    a plain function (not async) and must not reference any asyncio state.
    It receives and returns plain dicts to stay picklable across processes.

    Pipeline steps:
        1. Validate and ingest video files
        2. Compute audio sync offsets via GCC-PHAT
        3. Build the multicam cut list
        4. Render to MP4 via FFmpeg

    Args:
        job_dict: Serialized MulticamJob dict from MulticamJob.to_dict().

    Returns:
        The output file path string on success.

    Raises:
        Any exception from the pipeline propagates back to the asyncio caller.
    """
    # Re-import inside worker process — necessary for ProcessPoolExecutor
    from multicam_pipeline.job_schema import MulticamJob
    from multicam_pipeline.pipeline import run_multicam_job

    job = MulticamJob.from_dict(job_dict)

    print(f"[job_runner] Starting job {job.job_id}")

    output = run_multicam_job(job)
    selected_audio_source_path = job.selected_audio_source_path or ""

    print(f"[job_runner] Job {job.job_id} complete → {output}")
    return output, selected_audio_source_path


# ---------------------------------------------------------------------------
# Async job lifecycle management
# ---------------------------------------------------------------------------

async def _execute_job(job: MulticamJob) -> None:
    """
    Async wrapper that runs the pipeline in a process pool and updates
    job status in the in-memory store throughout the lifecycle.

    Args:
        job: The MulticamJob to execute.
    """
    job.mark_processing()
    _job_store[job.job_id] = job

    loop = asyncio.get_running_loop()

    try:
        # Offload the blocking pipeline to a process pool worker.
        # run_in_executor bridges asyncio and the ProcessPoolExecutor.
        output_path, selected_audio_source_path = await loop.run_in_executor(
            _executor,
            _run_pipeline_sync,
            job.to_dict(),  # pass as plain dict — must be picklable
        )
        job.output_path = output_path
        job.selected_audio_source_path = selected_audio_source_path
        job.mark_complete()

    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        job.mark_failed(error_detail)
        print(f"[job_runner] Job {job.job_id} FAILED:\n{error_detail}")

    finally:
        # Always persist the final state back to the store
        _job_store[job.job_id] = job


async def submit_job(job: MulticamJob) -> str:
    """
    Submit a MulticamJob for async background processing.

    Returns immediately with the job_id. The pipeline runs in the background
    via asyncio.create_task so the calling web handler is not blocked.

    Args:
        job: A MulticamJob instance to enqueue.

    Returns:
        The job_id string.

    Example:
        job = MulticamJob(video_paths=["a.mp4", "b.mp4"], output_path="out.mp4")
        job_id = await submit_job(job)
        # Returns immediately — pipeline runs in background
    """
    # Register the job as PENDING before the task starts
    _job_store[job.job_id] = job

    # Fire-and-forget: create_task schedules the coroutine without awaiting it
    asyncio.create_task(_execute_job(job))

    print(f"[job_runner] Submitted job {job.job_id} ({len(job.video_paths)} videos)")
    return job.job_id


def get_job_status(job_id: str) -> Optional[dict]:
    """
    Retrieve the current status of a job from the in-memory store.

    Args:
        job_id: The UUID string returned by submit_job().

    Returns:
        Serialized job dict, or None if the job_id is not found.
    """
    job = _job_store.get(job_id)
    return job.to_dict() if job else None


def list_jobs() -> list:
    """
    Return a summary list of all jobs in the local store.
    Useful for a dev dashboard or /jobs debug endpoint.

    Returns:
        List of serialized job dicts ordered by created_at descending.
    """
    return sorted(
        [j.to_dict() for j in _job_store.values()],
        key=lambda j: j["created_at"],
        reverse=True,
    )
