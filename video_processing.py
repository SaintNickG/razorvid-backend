"""
video_processing.py
-------------------
Utilities for automated multicam preparation and final-render enhancement.

Required system dependency:
- FFmpeg available on PATH (for example: brew install ffmpeg, sudo apt install ffmpeg)

Python dependencies:
- moviepy
- opencv-python
- vidstab
- librosa
- numpy
- scipy
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

import cv2
import librosa
import numpy as np
from moviepy.editor import VideoFileClip, concatenate_videoclips, vfx
from scipy.signal import correlate, correlation_lags
from vidstab import VidStab


class FFmpegCommandError(RuntimeError):
    """Raised when an FFmpeg command cannot be executed successfully."""


def run_ffmpeg_command(command_args: List[str]) -> None:
    """
    Execute an FFmpeg command with robust error handling.

    Args:
        command_args: FFmpeg CLI arguments without the leading `ffmpeg` token.

    Raises:
        FileNotFoundError: FFmpeg executable is not available on PATH.
        FFmpegCommandError: FFmpeg exits with a non-zero status.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise FileNotFoundError(
            "FFmpeg binary not found on PATH. Install it first "
            "(macOS: brew install ffmpeg, Ubuntu/Debian: sudo apt install ffmpeg)."
        )

    command = [ffmpeg_bin, *command_args]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise FFmpegCommandError(f"Unable to execute FFmpeg command: {exc}") from exc

    if result.returncode != 0:
        raise FFmpegCommandError(
            "FFmpeg command failed with non-zero exit code "
            f"{result.returncode}: {' '.join(command)}\n"
            f"stderr:\n{result.stderr.strip()}"
        )


def standardize_video_format(input_path: str, output_path: str) -> str:
    """
    Convert a source video into a standardized MP4/H.264/AAC format.

    Args:
        input_path: Source video file path.
        output_path: Destination standardized video path.

    Returns:
        The output path.
    """
    run_ffmpeg_command(
        [
            "-y",
            "-i",
            input_path,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )
    return output_path


def apply_basic_enhancements(video_clips: List[str], output_path: str) -> str:
    """
    Apply lightweight color enhancement and cross-fade transitions, then concatenate.

    Args:
        video_clips: Ordered list of input clip paths.
        output_path: Destination path for the enhanced composite video.

    Returns:
        The output path.
    """
    if not video_clips:
        raise ValueError("apply_basic_enhancements requires at least one clip path.")

    clips: List[VideoFileClip] = []
    processed: List[VideoFileClip] = []
    transition_seconds = 0.75

    try:
        for clip_path in video_clips:
            clip = VideoFileClip(clip_path)
            clips.append(clip)

            # Keep enhancement subtle to avoid over-processing user footage.
            enhanced = clip.fx(vfx.lum_contrast, lum=8, contrast=18, contrast_thr=127)
            enhanced = enhanced.fx(vfx.colorx, 1.03)
            processed.append(enhanced)

        transitioned: List[VideoFileClip] = []
        for index, clip in enumerate(processed):
            if index == 0:
                transitioned.append(clip)
            else:
                transitioned.append(clip.crossfadein(transition_seconds))

        final = concatenate_videoclips(
            transitioned,
            method="compose",
            padding=-transition_seconds,
        )
        try:
            final.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                preset="medium",
                threads=4,
                logger=None,
            )
        finally:
            final.close()
    finally:
        for clip in processed:
            clip.close()
        for clip in clips:
            clip.close()

    return output_path


def stabilize_single_video(input_path: str, output_path: str, min_motion_threshold_px: float = 0.20) -> str:
    """
    Stabilize a single input video with vidstab, with graceful fallback.

    Args:
        input_path: Source video path.
        output_path: Destination stabilized path.
        min_motion_threshold_px: Mean transform magnitude threshold below which
            stabilization is skipped and the original is copied.

    Returns:
        The output path (stabilized clip or copied source).
    """
    try:
        cap = cv2.VideoCapture(input_path)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if frame_count <= 1:
            shutil.copy2(input_path, output_path)
            return output_path

        stabilizer = VidStab()
        stabilizer.gen_transforms(input_path=input_path)

        transforms = stabilizer.transforms
        if transforms is None or len(transforms) == 0:
            shutil.copy2(input_path, output_path)
            return output_path

        mean_motion = float(np.mean(np.linalg.norm(transforms[:, :2], axis=1)))
        if mean_motion < min_motion_threshold_px:
            shutil.copy2(input_path, output_path)
            return output_path

        stabilizer.stabilize(
            input_path=input_path,
            output_path=output_path,
            border_type="reflect",
            use_stored_transforms=True,
        )
        return output_path
    except Exception:
        # Never hard-fail a full render because stabilization had an issue.
        shutil.copy2(input_path, output_path)
        return output_path


def extract_audio_from_video(video_path: str, audio_output_path: str) -> str:
    """
    Extract mono WAV audio from a video file using FFmpeg.

    Args:
        video_path: Source video path.
        audio_output_path: Target WAV path.

    Returns:
        The extracted audio path.
    """
    run_ffmpeg_command(
        [
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            audio_output_path,
        ]
    )
    return audio_output_path


def find_audio_offset(reference_audio_path: str, target_audio_path: str) -> float:
    """
    Estimate target audio offset versus reference using cross-correlation.

    Positive return value means target starts later than reference by that many
    seconds. Negative value means target starts earlier.

    Args:
        reference_audio_path: WAV path used as timing reference.
        target_audio_path: WAV path to align against reference.

    Returns:
        Estimated offset in seconds.
    """
    ref, sample_rate = librosa.load(reference_audio_path, sr=16000, mono=True)
    target, _ = librosa.load(target_audio_path, sr=sample_rate, mono=True)

    if ref.size == 0 or target.size == 0:
        return 0.0

    ref = ref.astype(np.float32) - float(np.mean(ref))
    target = target.astype(np.float32) - float(np.mean(target))

    ref_norm = float(np.linalg.norm(ref))
    target_norm = float(np.linalg.norm(target))
    if ref_norm > 0:
        ref /= ref_norm
    if target_norm > 0:
        target /= target_norm

    corr = correlate(target, ref, mode="full", method="fft")
    lags = correlation_lags(target.size, ref.size, mode="full")
    best_lag = int(lags[int(np.argmax(corr))])
    return float(best_lag) / float(sample_rate)


def synchronize_video_clips(video_paths: List[str]) -> List[Tuple[str, float]]:
    """
    Compute per-video audio offsets relative to the first (reference) video.

    Args:
        video_paths: Ordered input video paths. First path is used as reference.

    Returns:
        List of (video_path, offset_seconds) tuples in input order.
    """
    if not video_paths:
        return []

    results: List[Tuple[str, float]] = [(video_paths[0], 0.0)]

    with tempfile.TemporaryDirectory(prefix="sync_audio_") as temp_dir:
        temp_root = Path(temp_dir)
        reference_wav = str(temp_root / "reference.wav")
        extract_audio_from_video(video_paths[0], reference_wav)

        for index, video_path in enumerate(video_paths[1:], start=1):
            try:
                target_wav = str(temp_root / f"target_{index}.wav")
                extract_audio_from_video(video_path, target_wav)
                offset = find_audio_offset(reference_wav, target_wav)
            except Exception:
                offset = 0.0
            results.append((video_path, offset))

    return results


def apply_sync_offsets(
    synchronized_clips: Sequence[Tuple[str, float]],
    output_dir: str,
) -> List[str]:
    """
    Materialize aligned clips by trimming each clip to a common start timeline.

    Args:
        synchronized_clips: Sequence of (video_path, offset_seconds) tuples.
        output_dir: Folder for aligned intermediate files.

    Returns:
        Ordered list of aligned clip paths.
    """
    if not synchronized_clips:
        return []

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    min_offset = min(offset for _, offset in synchronized_clips)

    aligned_paths: List[str] = []
    for index, (video_path, offset) in enumerate(synchronized_clips):
        trim_seconds = max(0.0, offset - min_offset)
        aligned_path = output_root / f"aligned_{index:03d}.mp4"
        if trim_seconds <= 1e-4:
            standardize_video_format(video_path, str(aligned_path))
        else:
            run_ffmpeg_command(
                [
                    "-y",
                    "-ss",
                    f"{trim_seconds:.6f}",
                    "-i",
                    video_path,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(aligned_path),
                ]
            )
        aligned_paths.append(str(aligned_path))

    return aligned_paths


@dataclass
class RenderPipelineArtifacts:
    """Container describing useful intermediate paths generated by the pipeline."""

    standardized: List[str]
    stabilized: List[str]
    synchronized: List[Tuple[str, float]]
    aligned: List[str]
    final_output: str
    working_dir: str


def cleanup_render_artifacts(artifacts: RenderPipelineArtifacts) -> None:
    """
    Remove the working directory created by render_video_pipeline.

    Args:
        artifacts: Artifacts object returned by render_video_pipeline.
    """
    shutil.rmtree(artifacts.working_dir, ignore_errors=True)


def render_video_pipeline(input_video_paths: List[str], final_output_path: str) -> RenderPipelineArtifacts:
    """
    Conceptual high-level render pipeline using the modular helpers in this module.

    Ordered flow:
    1. Standardize input container/codec format.
    2. Stabilize each contributor clip.
    3. Synchronize via audio cross-correlation and apply alignment trims.
    4. Apply high-level visual enhancements and transitions.

    Args:
        input_video_paths: Raw source clip paths.
        final_output_path: Destination for final rendered video.

    Returns:
        RenderPipelineArtifacts with intermediate and final paths.

    Notes:
        Intermediates are written to a generated working directory. Call
        cleanup_render_artifacts when you no longer need those files.
    """
    if not input_video_paths:
        raise ValueError("render_video_pipeline requires at least one input video path.")

    final_parent = Path(final_output_path).resolve().parent
    final_parent.mkdir(parents=True, exist_ok=True)

    temp_root = Path(tempfile.mkdtemp(prefix="render_pipeline_"))
    standardized_dir = temp_root / "01_standardized"
    stabilized_dir = temp_root / "02_stabilized"
    aligned_dir = temp_root / "03_aligned"
    standardized_dir.mkdir(parents=True, exist_ok=True)
    stabilized_dir.mkdir(parents=True, exist_ok=True)
    aligned_dir.mkdir(parents=True, exist_ok=True)

    standardized_paths: List[str] = []
    for index, src_path in enumerate(input_video_paths):
        out_path = standardized_dir / f"standardized_{index:03d}.mp4"
        standardized_paths.append(standardize_video_format(src_path, str(out_path)))

    stabilized_paths: List[str] = []
    for index, src_path in enumerate(standardized_paths):
        out_path = stabilized_dir / f"stabilized_{index:03d}.mp4"
        stabilized_paths.append(stabilize_single_video(src_path, str(out_path)))

    synchronized = synchronize_video_clips(stabilized_paths)
    aligned_paths = apply_sync_offsets(synchronized, str(aligned_dir))

    apply_basic_enhancements(aligned_paths, final_output_path)

    return RenderPipelineArtifacts(
        standardized=standardized_paths,
        stabilized=stabilized_paths,
        synchronized=synchronized,
        aligned=aligned_paths,
        final_output=final_output_path,
        working_dir=str(temp_root),
    )
