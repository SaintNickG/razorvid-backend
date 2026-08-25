"""
multicam_pipeline
-----------------
Async-ready backend pipeline for multi-angle video synchronization
and automated multicam timeline rendering.

Local dev:   job_runner  (asyncio + ProcessPoolExecutor)
Production:  aws_handler (SQS + Lambda + S3 + DynamoDB)
"""

from .config import IS_LOCAL
from .job_schema import MulticamJob, JobStatus

if IS_LOCAL:
    from .audio_sync import align_videos
    from .multicam_cutter import build_cut_list, CutSegment, summarize_cut_list
    from .pipeline import run_multicam_job
    from .job_runner import submit_job, get_job_status, list_jobs

__all__ = [
    # Job schema
    "MulticamJob",
    "JobStatus",
]

if IS_LOCAL:
    __all__.extend([
        "align_videos",
        "build_cut_list",
        "CutSegment",
        "summarize_cut_list",
        "run_multicam_job",
        "submit_job",
        "get_job_status",
        "list_jobs",
    ])
