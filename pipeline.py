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

import os
from typing import Dict, List

from multicam_pipeline.audio_sync import align_videos_with_reference
from multicam_pipeline.ingestion import VideoMetadata, validate_and_ingest
from multicam_pipeline.job_schema import CuttingStrategy, MulticamJob
from multicam_pipeline.multicam_cutter import CutSegment, build_cut_list
from multicam_pipeline.rendering import render


def _resolve_durations(metadata: List[VideoMetadata]) -> Dict[str, float]:
    """Build a path->duration map from ingestion metadata."""
    return {m.path: float(m.duration) for m in metadata}


def _resolve_output_orientation(metadata: List[VideoMetadata], job: MulticamJob) -> str:
    if job.orientation_mode.value != "auto":
        return job.orientation_mode.value
    portrait_paths = {item.path for item in metadata if item.height > item.width}
    creator_is_portrait = any(path in portrait_paths for path in job.creator_video_paths)
    return "portrait" if creator_is_portrait and len(portrait_paths) * 2 >= len(metadata) else "landscape"


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
    # 1) Input validation + metadata extraction. Files with unusable resolution
    # (e.g. an iPhone thumbnail/companion track) are dropped by validate_and_ingest;
    # everything downstream must use the surviving paths, not job.video_paths.
    metadata = validate_and_ingest(job.video_paths)
    valid_video_paths = [item.path for item in metadata]
    excluded_paths = [p for p in job.video_paths if p not in valid_video_paths]
    if excluded_paths:
        job.excluded_low_quality_files = [os.path.basename(p) for p in excluded_paths]
        print(f"[pipeline] Excluded {len(excluded_paths)} low-resolution companion file(s): {job.excluded_low_quality_files}")
    durations = _resolve_durations(metadata)

    selected_orientation = _resolve_output_orientation(metadata, job)
    output_width, output_height = job.target_width, job.target_height
    if selected_orientation == "portrait" and output_width > output_height:
        output_width, output_height = output_height, output_width
    elif selected_orientation == "landscape" and output_height > output_width:
        output_width, output_height = output_height, output_width

    # 2) Audio-based alignment to compute per-angle master timeline offsets.
    offsets, reference_audio_path, sync_diagnostics = align_videos_with_reference(
        valid_video_paths,
        preferred_reference_path=job.audio_source_override,
        event_type=job.event_type.value,
        cutting_strategy=job.cutting_strategy.value,
        include_diagnostics=True,
        metadata_creation_times={item.path: item.creation_time for item in metadata if item.creation_time is not None},
        video_durations=durations,
        force_full_track_sync=job.force_full_track_sync,
    )
    job.sync_diagnostics = sync_diagnostics

    # Alignment is relative to the reference, so offsets can be negative. Shift
    # the whole timeline to start at 0 or the cutters drop the earliest footage.
    earliest_offset = min(offsets.values())
    offsets = {path: value - earliest_offset for path, value in offsets.items()}

    # 3) Build the multicam cut timeline based on job strategy.
    segments = _build_segments(
        video_paths=valid_video_paths,
        offsets=offsets,
        job=job,
        durations=durations,
    )

    # 4) Render final output using FFmpeg filtergraph concat.
    output_path = render(
        segments=segments,
        output_path=job.output_path,
        target_width=output_width,
        target_height=output_height,
        target_fps=job.target_fps,
        event_profile=job.event_type.value,
        effect_intensity=job.effect_intensity.value,
        transition_style=job.transition_style,
        audio_source_path=reference_audio_path,
        audio_source_offset=offsets.get(reference_audio_path, 0.0),
        audio_source_duration=durations.get(reference_audio_path),
        orientation_mode=selected_orientation,
        orientation_crop_depth=job.orientation_crop_depth,
        source_dimensions={item.path: (item.width, item.height) for item in metadata},
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
