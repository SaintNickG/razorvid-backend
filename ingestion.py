"""
ingestion.py
------------
Handles uploaded video file validation and metadata extraction
(FPS, duration, resolution, audio channels) via FFprobe.
"""

import subprocess
import json
import os
from math import isfinite
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

# Minimum short-side resolution (px) for a file to count as a real camera
# angle. Configurable so it can be temporarily lowered (e.g. for a practice
# render using already-uploaded low-res footage) without a code change.
MIN_VIDEO_SHORT_SIDE_PX = int(os.environ.get("MIN_VIDEO_SHORT_SIDE_PX", "480"))


@dataclass
class VideoMetadata:
    """
    Metadata extracted from a video file.

    Attributes:
        path:           Absolute path to the video file.
        duration:       Duration in seconds.
        fps:            Frames per second (float).
        width:          Video width in pixels.
        height:         Video height in pixels.
        audio_channels: Number of audio channels (0 if no audio stream).
        has_audio:      True if the file contains at least one audio stream.
    """
    path: str
    duration: float
    fps: float
    width: int
    height: int
    audio_channels: int
    creation_time: Optional[float] = None
    video_duration: Optional[float] = None
    audio_duration: Optional[float] = None

    @property
    def has_audio(self) -> bool:
        return self.audio_channels > 0

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def _run_ffprobe(video_path: str) -> dict:
    """
    Run ffprobe on a video file and return the parsed JSON output.

    Args:
        video_path: Path to the video file.

    Returns:
        Parsed ffprobe JSON as a dict.

    Raises:
        RuntimeError: If ffprobe fails or returns invalid JSON.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for '{video_path}':\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse ffprobe output: {e}")


def _parse_fps(fps_str: str) -> float:
    """
    Parse an FFprobe fraction FPS string like '30000/1001' into a float.

    Args:
        fps_str: FPS string in 'num/den' or plain float format.

    Returns:
        FPS as a float.
    """
    if "/" in fps_str:
        num, den = fps_str.split("/")
        return round(int(num) / int(den), 4) if int(den) != 0 else 0.0
    return float(fps_str)


def _parse_creation_time(tags: dict) -> Optional[float]:
    """Parse common container/camera capture-time tags as a UTC Unix timestamp."""
    tag_names = {
        "creation_time",
        "com.apple.quicktime.creationdate",
        "creationdate",
        "creation_date",
        "date_time_original",
    }
    for key, value in (tags or {}).items():
        if str(key).casefold() not in tag_names or not value:
            continue
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            try:
                timestamp = float(text)
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp > 0:
                return timestamp
    return None


def _stream_duration(stream: dict, fallback: float) -> float:
    try:
        duration = float(stream.get("duration"))
        return duration if isfinite(duration) and duration >= 0 else fallback
    except (TypeError, ValueError):
        return fallback


def extract_metadata(video_path: str) -> VideoMetadata:
    """
    Extract metadata from a video file using FFprobe.

    Args:
        video_path: Path to the video file.

    Returns:
        VideoMetadata dataclass populated with stream info.

    Raises:
        FileNotFoundError: If the file does not exist.
        RuntimeError: If ffprobe fails or required streams are missing.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: '{video_path}'")

    probe = _run_ffprobe(video_path)
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})

    duration = float(fmt.get("duration", 0.0))

    # Extract video stream properties — pick the largest-area stream so that
    # companion thumbnail tracks (e.g. iPhone 224x128 mebx) are not mistaken
    # for the primary camera angle.
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise RuntimeError(f"No video stream found in '{video_path}'")
    video_stream = max(
        video_streams,
        key=lambda s: int(s.get("width", 0)) * int(s.get("height", 0)),
    )

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    rotation = int(video_stream.get("tags", {}).get("rotate", 0) or 0)
    for side_data in video_stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            rotation = int(side_data["rotation"] or 0)
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width
    fps = _parse_fps(video_stream.get("r_frame_rate", "0/1"))

    # Extract audio stream properties
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    audio_channels = int(audio_stream.get("channels", 0)) if audio_stream else 0

    creation_time = (
        _parse_creation_time(fmt.get("tags", {}))
        or _parse_creation_time(video_stream.get("tags", {}))
    )

    return VideoMetadata(
        path=video_path,
        duration=duration,
        fps=fps,
        width=width,
        height=height,
        audio_channels=audio_channels,
        creation_time=creation_time,
        video_duration=_stream_duration(video_stream, duration),
        audio_duration=_stream_duration(audio_stream, duration) if audio_stream else 0.0,
    )


def validate_and_ingest(
    video_paths: List[str],
    failed_angles: Optional[List[dict[str, str]]] = None,
) -> List[VideoMetadata]:
    """
    Validate and extract metadata for a list of uploaded video files.

    Validation rules:
        - File must exist and be non-empty
        - Must contain a valid video stream
        - Must contain an audio stream (required for sync)
        - Duration must be > 1 second

    Files whose resolution is below the 480px short-side minimum are excluded
    rather than failing the whole render — this is a common iPhone/Photos
    quirk where a low-res "companion" or thumbnail file gets bundled alongside
    the real camera angle. The render only fails outright if fewer than 2
    usable angles remain after exclusion.

    Args:
        video_paths: List of paths to uploaded video files.

    Returns:
        List of VideoMetadata objects, one per valid input file.

    Raises:
        ValueError: If fewer than 2 usable angles remain after validation.
    """
    if not video_paths:
        raise ValueError("No video paths provided.")

    results: List[VideoMetadata] = []
    low_res_skips: List[tuple[str, str]] = []  # (path, resolution) for each excluded file
    failures = failed_angles if failed_angles is not None else []

    for path in video_paths:
        print(f"[ingestion] Ingesting '{path}'...")

        try:
            meta = extract_metadata(path)
        except Exception as exc:
            reason = str(exc).replace(path, os.path.basename(path)).replace("\n", "; ").strip()
            failures.append({
                "file": os.path.basename(path),
                "reason": reason[:500] or "Video metadata could not be read.",
                "category": "validation",
            })
            print(f"[ingestion]   ✗ excluding unreadable file '{path}': {reason}")
            continue

        # Validation checks
        if meta.duration <= 1.0:
            reason = f"Clip is too short ({meta.duration:.2f}s); minimum is 1s."
            failures.append({"file": os.path.basename(path), "reason": reason, "category": "validation"})
            continue
        if not meta.has_audio:
            reason = "No audio track; audio is required to synchronize this angle."
            failures.append({"file": os.path.basename(path), "reason": reason, "category": "validation"})
            continue
        if meta.fps <= 0:
            reason = f"No valid frame rate was found ({meta.fps} FPS)."
            failures.append({"file": os.path.basename(path), "reason": reason, "category": "validation"})
            continue
        if min(meta.width, meta.height) < MIN_VIDEO_SHORT_SIDE_PX:
            # Note: this is frequently *real* footage that was auto-compressed
            # during sharing (iMessage/SMS, some cloud links) rather than an
            # actual thumbnail/companion track, so the message must not assume
            # which cause it is.
            print(f"[ingestion]   ✗ excluding low-resolution file ({meta.resolution}): '{path}'")
            low_res_skips.append((path, meta.resolution))
            failures.append({
                "file": os.path.basename(path),
                "reason": f"Resolution {meta.resolution} is below the {MIN_VIDEO_SHORT_SIDE_PX}px minimum.",
                "category": "low_resolution",
            })
            continue

        print(
            f"[ingestion]   ✓ {meta.resolution} @ {meta.fps}fps | "
            f"{meta.duration:.2f}s | {meta.audio_channels}ch audio"
        )
        results.append(meta)

    if len(results) < 2 and low_res_skips:
        first_path, first_resolution = low_res_skips[0]
        base_reason = (
            f"'{first_path}' video resolution {first_resolution} is too low. "
            f"Minimum short-side is {MIN_VIDEO_SHORT_SIDE_PX}px. This can happen with a thumbnail/companion "
            f"track, or with real footage that was auto-compressed while sharing "
            f"(e.g. sent over iMessage/SMS or a low-quality cloud link)."
        )
        if len(low_res_skips) > 1:
            other_names = ", ".join(os.path.basename(p) for p, _ in low_res_skips[1:])
            base_reason += (
                f" {len(low_res_skips)} of {len(video_paths)} uploaded files were too low "
                f"resolution to use ({other_names} also affected), leaving only "
                f"{len(results)} usable angle(s) — at least 2 are required. Try re-uploading "
                f"the original files directly (e.g. via AirDrop or a direct file transfer) "
                f"instead of a shared/forwarded copy."
            )
        raise ValueError(base_reason)
    if len(results) < 2:
        details = "; ".join(f"{item['file']}: {item['reason']}" for item in failures)
        raise ValueError(
            f"Only {len(results)} usable angle(s) remain after validation; at least 2 are required. {details}"
        )

    return results
