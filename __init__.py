"""
multicam_pipeline
-----------------
Async-ready backend pipeline for multi-angle video synchronization
and automated multicam timeline rendering.

Local dev:   job_runner  (asyncio + ProcessPoolExecutor)
Production:  aws_handler (SQS + Lambda + S3 + DynamoDB)
"""

from .audio_sync import align_videos
from .multicam_cutter import build_cut_list, CutSegment, summarize_cut_list
from .pipeline import run_multicam_job
from .job_schema import MulticamJob, JobStatus
from .job_runner import submit_job, get_job_status, list_jobs

try:
    from .video_processing import (
        apply_basic_enhancements,
        cleanup_render_artifacts,
        extract_audio_from_video,
        find_audio_offset,
        RenderPipelineArtifacts,
        render_video_pipeline,
        run_ffmpeg_command,
        standardize_video_format,
        stabilize_single_video,
        synchronize_video_clips,
    )
    _HAS_VIDEO_PROCESSING = True
except Exception:
    # Optional video-processing stack (moviepy/vidstab) may be absent in some
    # minimal environments; keep core package imports functional.
    _HAS_VIDEO_PROCESSING = False

__all__ = [
    # Pipeline core
    "align_videos",
    "build_cut_list",
    "CutSegment",
    "summarize_cut_list",
    "run_multicam_job",
    # Job management
    "MulticamJob",
    "JobStatus",
    "submit_job",
    "get_job_status",
    "list_jobs",
]

if _HAS_VIDEO_PROCESSING:
    __all__.extend(
        [
            "run_ffmpeg_command",
            "standardize_video_format",
            "apply_basic_enhancements",
            "stabilize_single_video",
            "extract_audio_from_video",
            "find_audio_offset",
            "synchronize_video_clips",
            "RenderPipelineArtifacts",
            "render_video_pipeline",
            "cleanup_render_artifacts",
        ]
    )
