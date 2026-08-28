"""
pipeline.py
-----------
Single orchestration layer for the multicam backend pipeline.

This module centralizes the end-to-end workflow so both local async jobs
and AWS Lambda jobs execute identical business logic:

    1) Validate + ingest video metadata
    2) Audio-align all angles (GCC-PHAT)
    3) Build multicam cut list (interval or AI strategy)
    4) Render final MP4 via FFmpeg
"""

from __future__ import annotations

from typing import Dict, List

from multicam_pipeline.audio_sync import align_videos_with_reference
from multicam_pipeline.ingestion import VideoMetadata, validate_and_ingest
from multicam_pipeline.job_schema import CuttingStrategy, MulticamJob
from multicam_pipeline.multicam_cutter import CutSegment, build_cut_list
from multicam_pipeline.rendering import render


def _resolve_durations(metadata: List[VideoMetadata]) -> Dict[str, float]:
    """Build a path->duration map from ingestion metadata."""
    return {m.path: float(m.duration) for m in metadata}


def _build_segments(
    video_paths: List[str],
    offsets: Dict[str, float],
    job: MulticamJob,
    durations: Dict[str, float],
) -> List[CutSegment]:
    """Build timeline segments according to the job's cutting strategy."""
    if job.cutting_strategy == CuttingStrategy.INTERVAL:
        return build_cut_list(
            video_paths=video_paths,
            offsets=offsets,
            cut_interval=job.cut_interval,
            durations=durations,
        )

    # AI strategies use the event-aware cutter that combines audio/video signals.
    from multicam_pipeline.ai_cutter import build_ai_cut_list

    return build_ai_cut_list(
        video_paths=video_paths,
        offsets=offsets,
        job=job,
    )


def run_multicam_job(job: MulticamJob) -> str:
    """
    Execute a full multicam render job synchronously.

    This function is intentionally synchronous so it can be executed in:
    - a ProcessPoolExecutor worker (local async runner)
    - a Lambda invocation worker thread (AWS handler)

    Args:
        job: Job configuration and output target.

    Returns:
        Rendered output path.
    """
    # 1) Input validation + metadata extraction.
    metadata = validate_and_ingest(job.video_paths)
    durations = _resolve_durations(metadata)

    # 2) Audio-based alignment to compute per-angle master timeline offsets.
    offsets, reference_audio_path, sync_diagnostics = align_videos_with_reference(
        job.video_paths,
        preferred_reference_path=job.audio_source_override,
        event_type=job.event_type.value,
        cutting_strategy=job.cutting_strategy.value,
        include_diagnostics=True,
    )
    job.sync_diagnostics = sync_diagnostics

    # Alignment is relative to the reference, so offsets can be negative. Shift
    # the whole timeline to start at 0 or the cutters drop the earliest footage.
    earliest_offset = min(offsets.values())
    offsets = {path: value - earliest_offset for path, value in offsets.items()}

    # 3) Build the multicam cut timeline based on job strategy.
    segments = _build_segments(
        video_paths=job.video_paths,
        offsets=offsets,
        job=job,
        durations=durations,
    )

    # 4) Render final output using FFmpeg filtergraph concat.
    output_path = render(
        segments=segments,
        output_path=job.output_path,
        target_width=job.target_width,
        target_height=job.target_height,
        target_fps=job.target_fps,
        event_profile=job.event_type.value,
        effect_intensity=job.effect_intensity.value,
        transition_style=job.transition_style,
        audio_source_path=reference_audio_path,
        audio_source_offset=offsets.get(reference_audio_path, 0.0),
        audio_source_duration=durations.get(reference_audio_path),
    )

    if job.project_id:
        try:
            from multicam_pipeline.routers.projects import record_render_contributions

            record_render_contributions(
                project_id=job.project_id,
                render_id=job.job_id,
                segments=segments,
            )
        except Exception as exc:
            # Contribution analytics must never block successful render completion.
            print(f"[pipeline] Warning: failed to persist contributions for job {job.job_id}: {exc}")

    job.selected_audio_source_path = reference_audio_path
    return output_path
