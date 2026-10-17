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
from multicam_pipeline.rendering import build_audio_spans, render


def split_cut_segments(segments: List[CutSegment], max_duration: float) -> List[List[CutSegment]]:
    """Partition an aligned cut timeline into sequential duration-bounded parts."""
    if max_duration <= 0:
        raise ValueError("max_duration must be greater than zero.")
    if not segments:
        return []

    timeline_start = min(segment.start_time for segment in segments)
    timeline_end = max(segment.end_time for segment in segments)
    parts: List[List[CutSegment]] = []
    part_start = timeline_start

    while part_start < timeline_end - 1e-6:
        part_end = min(part_start + max_duration, timeline_end)
        part_segments = []
        for segment in segments:
            start = max(segment.start_time, part_start)
            end = min(segment.end_time, part_end)
            if end - start < 1e-6:
                continue
            part_segments.append(CutSegment(
                start_time=start,
                end_time=end,
                source_video_path=segment.source_video_path,
                offset=segment.offset,
                transition_theme=(
                    segment.transition_theme
                    if abs(start - segment.start_time) < 1e-6
                    else "hard_cut"
                ),
            ))
        if part_segments:
            parts.append(part_segments)
        part_start = part_end

    return parts


def _resolve_durations(metadata: List[VideoMetadata]) -> Dict[str, float]:
    """Build a path->duration map from ingestion metadata."""
    return {m.path: float(m.video_duration if m.video_duration is not None else m.duration) for m in metadata}


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
    angle_failures: List[dict[str, str]] = []
    try:
        metadata = validate_and_ingest(job.video_paths, failed_angles=angle_failures)
    except Exception:
        job.angle_failures = angle_failures
        raise
    valid_video_paths = [item.path for item in metadata]
    excluded_paths = [p for p in job.video_paths if p not in valid_video_paths]
    low_resolution_names = {
        item["file"] for item in angle_failures if item.get("category") == "low_resolution"
    }
    job.excluded_low_quality_files = [
        os.path.basename(path) for path in excluded_paths
        if os.path.basename(path) in low_resolution_names
    ]
    job.angle_failures = angle_failures
    durations = _resolve_durations(metadata)

    # 2) Audio-based alignment to compute per-angle master timeline offsets.
    try:
        offsets, reference_audio_path, sync_diagnostics = align_videos_with_reference(
            valid_video_paths,
            preferred_reference_path=(
                job.audio_source_override
                if job.audio_source_override in valid_video_paths
                else None
            ),
            event_type=job.event_type.value,
            cutting_strategy=job.cutting_strategy.value,
            include_diagnostics=True,
            metadata_creation_times={item.path: item.creation_time for item in metadata if item.creation_time is not None},
            video_durations=durations,
            force_full_track_sync=job.force_full_track_sync,
            failed_angles=angle_failures,
        )
    except Exception:
        job.angle_failures = angle_failures
        raise
    job.sync_diagnostics = sync_diagnostics
    aligned_paths = set(offsets)
    valid_video_paths = [path for path in valid_video_paths if path in aligned_paths]
    metadata = [item for item in metadata if item.path in aligned_paths]
    durations = _resolve_durations(metadata)
    audio_coverage = {
        item.path: min(durations[item.path], item.audio_duration if item.audio_duration is not None else item.duration)
        for item in metadata
    }
    job.angle_failures = angle_failures

    if len(valid_video_paths) < 2:
        raise ValueError("At least 2 reliable video angles are required to render.")

    selected_orientation = _resolve_output_orientation(metadata, job)
    output_width, output_height = job.target_width, job.target_height
    if selected_orientation == "portrait" and output_width > output_height:
        output_width, output_height = output_height, output_width
    elif selected_orientation == "landscape" and output_height > output_width:
        output_width, output_height = output_height, output_width

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

    # 4) Render the aligned timeline in sequential parts when requested.
    max_duration = float(job.max_duration or 240.0)
    timeline_duration = (
        max(segment.end_time for segment in segments)
        - min(segment.start_time for segment in segments)
    ) if segments else 0.0
    part_segments = (
        split_cut_segments(segments, max_duration)
        if job.auto_split and timeline_duration > max_duration
        else [segments]
    )
    output_stem, output_extension = os.path.splitext(job.output_path)
    if not output_extension:
        output_extension = ".mp4"
    if len(part_segments) > 1:
        output_paths = [
            f"{output_stem}_part_{part_index:02d}_of_{len(part_segments):02d}{output_extension}"
            for part_index in range(1, len(part_segments) + 1)
        ]
    else:
        output_paths = [job.output_path]

    rendered_paths: List[str] = []
    try:
        for output_part_index, (part, output_part_path) in enumerate(zip(part_segments, output_paths), start=1):
            if len(part_segments) > 1:
                print(f"[pipeline] Rendering part {output_part_index}/{len(part_segments)}")
            audio_spans = build_audio_spans(
                part[0].start_time,
                part[-1].end_time,
                reference_audio_path,
                offsets,
                audio_coverage,
                {path: float(values.get("audio_quality_score", 0.0)) for path, values in sync_diagnostics.items()},
            )
            for span in audio_spans:
                if span.source_path != reference_audio_path:
                    source_name = os.path.basename(span.source_path) if span.source_path else "silence (no eligible source)"
                    print(f"[pipeline] Audio fallback {span.start_time:.3f}s-{span.end_time:.3f}s: {source_name}")
            rendered_paths.append(render(
                segments=part,
                output_path=output_part_path,
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
                audio_spans=audio_spans,
            ))
    except Exception:
        for rendered_path in rendered_paths:
            try:
                os.remove(rendered_path)
            except OSError:
                pass
        raise

    job.output_parts = rendered_paths
    job.total_parts = len(rendered_paths)
    job.output_path = rendered_paths[0]
    output_path = job.output_path

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
