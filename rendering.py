"""
rendering.py
------------
FFmpeg-based rendering engine that takes a CutSegment list and produces
a single multicam MP4 (H.264/AAC) using a concat filtergraph.

Design goals:
    - Never load entire video files into memory — all processing is
      stream-based via FFmpeg subprocess pipes.
    - Auto-detect hardware acceleration (VideoToolbox → NVENC → VAAPI)
      and fall back cleanly to libx264 if none are available.
    - Build a single FFmpeg invocation using a filtergraph so all
      trimming, scaling, and concatenation happens in one pass.

Dependencies:
    pip install ffmpeg-python
"""

import subprocess
import platform
import shutil
from typing import List, Optional
from multicam_pipeline.multicam_cutter import CutSegment, assign_transition_themes


# ---------------------------------------------------------------------------
# Hardware acceleration detection
# ---------------------------------------------------------------------------

def _probe_encoder(encoder: str) -> bool:
    """
    Test whether a given FFmpeg encoder is available on this system by
    attempting a 1-frame null encode.

    Args:
        encoder: FFmpeg encoder name (e.g. 'h264_videotoolbox', 'h264_nvenc').

    Returns:
        True if the encoder is usable, False otherwise.
    """
    if shutil.which("ffmpeg") is None:
        raise EnvironmentError("ffmpeg is not installed or not on PATH.")

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1",  # tiny synthetic source
        "-vframes", "1",
        "-c:v", encoder,
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def detect_video_encoder() -> str:
    """
    Detect the best available hardware-accelerated H.264 encoder,
    falling back to software libx264 if none are found.

    Priority order:
        1. h264_videotoolbox  — Apple Silicon / macOS GPU
        2. h264_nvenc         — NVIDIA GPU (Linux/Windows)
        3. h264_vaapi         — Intel/AMD GPU via VAAPI (Linux)
        4. libx264            — Software fallback (always available)

    Returns:
        The FFmpeg encoder name string to use for the -c:v flag.
    """
    system = platform.system()

    candidates = []

    # VideoToolbox is macOS-only
    if system == "Darwin":
        candidates.append("h264_videotoolbox")

    # NVENC works on Linux and Windows
    if system in ("Linux", "Windows"):
        candidates.append("h264_nvenc")

    # VAAPI is Linux-only
    if system == "Linux":
        candidates.append("h264_vaapi")

    for encoder in candidates:
        print(f"[rendering] Probing encoder: {encoder}...")
        if _probe_encoder(encoder):
            print(f"[rendering] Using hardware encoder: {encoder}")
            return encoder

    print("[rendering] No hardware encoder available, falling back to libx264.")
    return "libx264"


# ---------------------------------------------------------------------------
# Encoder quality flags
# ---------------------------------------------------------------------------

def _estimate_target_bitrate_mbps(
    target_width: int,
    target_height: int,
    target_fps: int,
) -> float:
    """
    Estimate a sensible H.264 bitrate target from output geometry and frame rate.

    Baseline is 8 Mbps at 1920x1080 @ 30fps, then scaled by pixel count and fps.
    Clamp to practical bounds so tiny outputs do not starve and large outputs
    do not explode bitrate unexpectedly.
    """
    baseline_pixels = 1920 * 1080
    current_pixels = max(1, target_width * target_height)
    pixel_scale = current_pixels / baseline_pixels

    # Slightly sublinear fps scaling so 60fps does not simply double bitrate.
    fps_scale = max(1.0, (target_fps / 30.0) ** 0.8)
    mbps = 8.0 * pixel_scale * fps_scale

    return max(3.0, min(28.0, mbps))


def _event_profile_multipliers(event_profile: str) -> tuple[float, float, int]:
    """
    Return bitrate/GOP tuning multipliers for event-specific pacing.

    Returns:
        (bitrate_multiplier, gop_multiplier, audio_bitrate_kbps)
    """
    profile = (event_profile or "balanced").lower()

    if profile == "sport":
        # Fast motion benefits from more bitrate and more frequent keyframes.
        return 1.15, 0.75, 192
    if profile == "cheer":
        # Stunts and routine transitions benefit from slightly tighter GOP.
        return 1.08, 0.90, 192
    if profile == "concert":
        # Music-focused edits typically need stronger audio bitrate and can use longer GOP.
        return 1.00, 1.15, 256

    return 1.00, 1.00, 192


def _encoder_quality_flags(
    encoder: str,
    target_width: int,
    target_height: int,
    target_fps: int,
    event_profile: str = "balanced",
) -> List[str]:
    """
    Return encoder-specific quality/preset flags appropriate for each encoder.

    Args:
        encoder: The FFmpeg encoder name.

    Returns:
        List of FFmpeg flag strings to append to the command.
    """
    estimated_mbps = _estimate_target_bitrate_mbps(
        target_width=target_width,
        target_height=target_height,
        target_fps=target_fps,
    )
    bitrate_multiplier, gop_multiplier, _ = _event_profile_multipliers(event_profile)
    estimated_mbps *= bitrate_multiplier
    maxrate_mbps = estimated_mbps * 1.4
    bufsize_mbps = estimated_mbps * 2.0
    gop = max(20, int(target_fps * 2 * gop_multiplier))
    keyint_min = max(12, int(target_fps * gop_multiplier))

    if encoder == "libx264":
        # CRF 23 is a good default; preset 'fast' balances speed vs compression
        return [
            "-crf", "22",
            "-preset", "medium",
            "-g", str(gop),
            "-keyint_min", str(keyint_min),
            "-pix_fmt", "yuv420p",
        ]

    if encoder == "h264_videotoolbox":
        # VideoToolbox uses rate-control targets instead of CRF.
        return [
            "-b:v", f"{estimated_mbps:.1f}M",
            "-maxrate", f"{maxrate_mbps:.1f}M",
            "-bufsize", f"{bufsize_mbps:.1f}M",
            "-profile:v", "high",
            "-g", str(gop),
            "-keyint_min", str(keyint_min),
            "-allow_sw", "1",
            "-pix_fmt", "yuv420p",
        ]

    if encoder == "h264_nvenc":
        return [
            "-rc", "vbr",
            "-cq", "21",
            "-b:v", f"{estimated_mbps:.1f}M",
            "-maxrate", f"{maxrate_mbps:.1f}M",
            "-bufsize", f"{bufsize_mbps:.1f}M",
            "-preset", "p5",
            "-spatial_aq", "1",
            "-g", str(gop),
            "-pix_fmt", "yuv420p",
        ]

    if encoder == "h264_vaapi":
        return [
            "-qp", "23",
            "-b:v", f"{estimated_mbps:.1f}M",
            "-maxrate", f"{maxrate_mbps:.1f}M",
            "-bufsize", f"{bufsize_mbps:.1f}M",
            "-g", str(gop),
        ]

    return []


# ---------------------------------------------------------------------------
# Filtergraph builder
# ---------------------------------------------------------------------------

def _build_filtergraph(
    segments: List[CutSegment],
    target_width: int,
    target_height: int,
    target_fps: int,
    audio_source_input_index: Optional[int] = None,
    audio_source_offset: float = 0.0,
    audio_source_duration: Optional[float] = None,
) -> tuple[List[str], str]:
    """
    Build the FFmpeg -filter_complex string and the final output stream labels
    for a concat-based multicam render.

    Each segment is an independently trimmed input stream. The filtergraph:
        1. Scales every video segment to a common resolution (target_width x target_height)
           using pad to avoid distortion on mismatched aspect ratios.
        2. Sets a consistent frame rate via fps filter.
        3. Concatenates all video + audio streams in timeline order using the
           concat filter.

    Args:
        segments:       Ordered list of CutSegment objects.
        target_width:   Output video width in pixels.
        target_height:  Output video height in pixels.
        target_fps:     Output frame rate.

    Returns:
        Tuple of:
            - filter_lines: List of filtergraph fragment strings (joined with ';')
            - out_labels:   The final [vout][aout] stream label string for -map
    """
    filter_lines: List[str] = []
    n = len(segments)

    def _themed_chain(theme_name: str) -> str:
        # Keep effects duration-neutral so concat timing remains stable.
        if theme_name == "dissolve":
            return "fade=t=in:st=0:d=0.18"
        if theme_name == "whip_blur":
            return "tmix=frames=3:weights='1 2 1',unsharp=5:5:0.5:5:5:0.0,eq=contrast=1.10:saturation=1.12"
        if theme_name == "flash_punch":
            return "eq=brightness='if(lt(t,0.08),0.24,0)':contrast=1.14:saturation=1.10"
        if theme_name == "chroma_pop":
            return "eq=saturation=1.28:contrast=1.08"
        if theme_name == "neon_glow":
            return "eq=saturation=1.25:gamma=1.05,unsharp=5:5:0.6:5:5:0.0"
        if theme_name == "matrix_pan":
            return (
                "scale=iw*1.12:ih*1.12,"
                "crop=iw/1.12:ih/1.12:"
                "x='(in_w-out_w)/2 + 0.22*(in_w-out_w)*sin(2*PI*t)':"
                "y='(in_h-out_h)/2 + 0.10*(in_h-out_h)*cos(2*PI*t)',"
                "tblend=all_mode=average,"
                "eq=contrast=1.18:saturation=1.06:gamma=0.95"
            )
        return "null"

    for i in range(n):
        seg = segments[i]
        seg_theme = getattr(seg, "transition_theme", "hard_cut")
        # Each input is referenced as [i:v] and [i:a] in the filtergraph.
        # Scale to target resolution, pad black bars to preserve aspect ratio,
        # then enforce a consistent frame rate.
        filter_lines.append(
            f"[{i}:v]"
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
            f"{_themed_chain(seg_theme)},"
            f"fps={target_fps},"
            f"setsar=1"
            f"[v{i}]"
        )

        # Audio can either come from each segment input (legacy behavior) or
        # from one globally selected reference input for the full render.
        if audio_source_input_index is None:
            filter_lines.append(
                f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]"
            )
            continue

        seg_duration = max(0.0, seg.duration)
        src_start = seg.start_time - audio_source_offset
        src_end = seg.end_time - audio_source_offset

        overlap_start = max(0.0, src_start)
        overlap_end = src_end if audio_source_duration is None else min(src_end, audio_source_duration)
        body_duration = max(0.0, overlap_end - overlap_start)

        lead_silence = max(0.0, -src_start)
        if lead_silence > seg_duration:
            lead_silence = seg_duration

        trail_silence = max(0.0, seg_duration - lead_silence - body_duration)

        parts: List[str] = []

        if lead_silence > 0.0005:
            lead_label = f"a{i}_lead"
            filter_lines.append(
                f"anullsrc=r=44100:cl=stereo,"
                f"atrim=0:{lead_silence:.6f},"
                f"asetpts=PTS-STARTPTS[{lead_label}]"
            )
            parts.append(f"[{lead_label}]")

        if body_duration > 0.0005:
            body_label = f"a{i}_body"
            filter_lines.append(
                f"[{audio_source_input_index}:a]"
                f"atrim=start={overlap_start:.6f}:end={overlap_end:.6f},"
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=44100:channel_layouts=stereo"
                f"[{body_label}]"
            )
            parts.append(f"[{body_label}]")

        if trail_silence > 0.0005 or not parts:
            tail_label = f"a{i}_tail"
            silence_dur = trail_silence if parts else seg_duration
            filter_lines.append(
                f"anullsrc=r=44100:cl=stereo,"
                f"atrim=0:{silence_dur:.6f},"
                f"asetpts=PTS-STARTPTS[{tail_label}]"
            )
            parts.append(f"[{tail_label}]")

        if len(parts) == 1:
            filter_lines.append(f"{parts[0]}anull[a{i}]")
        else:
            filter_lines.append(
                f"{''.join(parts)}concat=n={len(parts)}:v=0:a=1[a{i}]"
            )

    # Build the concat filter input labels: [v0][a0][v1][a1]...[vN][aN]
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))

    # concat filter: n=segment count, v=1 video stream out, a=1 audio stream out
    filter_lines.append(
        f"{concat_inputs}concat=n={n}:v=1:a=1[vout][aout]"
    )

    return filter_lines, "[vout][aout]"


# ---------------------------------------------------------------------------
# FFmpeg command builder
# ---------------------------------------------------------------------------

def build_ffmpeg_command(
    segments: List[CutSegment],
    output_path: str,
    encoder: str,
    target_width: int = 1920,
    target_height: int = 1080,
    target_fps: int = 30,
    event_profile: str = "balanced",
    audio_source_path: Optional[str] = None,
    audio_source_offset: float = 0.0,
    audio_source_duration: Optional[float] = None,
) -> List[str]:
    """
    Construct the full FFmpeg subprocess command list for a multicam render.

    Each CutSegment becomes a separate -ss / -to trimmed input. The filtergraph
    handles scaling, fps normalization, and concatenation in a single pass.
    No intermediate files are written.

    Args:
        segments:      Ordered CutSegment list from build_cut_list().
        output_path:   Destination path for the rendered MP4.
        encoder:       FFmpeg video encoder name from detect_video_encoder().
        target_width:  Output width in pixels (default 1920).
        target_height: Output height in pixels (default 1080).
        target_fps:    Output frame rate (default 30).
        event_profile: Content profile used to tune encoder behavior.

    Returns:
        List of strings representing the complete FFmpeg command,
        ready to pass to subprocess.run().
    """
    cmd: List[str] = ["ffmpeg", "-y"]

    # --- Input section ---
    # Each segment is a separately trimmed input using -ss (seek) and -to.
    # Using input-side -ss is faster than output-side seeking for large files
    # because FFmpeg seeks before decoding.
    for seg in segments:
        cmd += [
            "-ss", f"{seg.source_start:.6f}",   # seek to trim start in source file
            "-to", f"{seg.source_end:.6f}",      # stop at trim end in source file
            "-i", seg.source_video_path,
        ]

    audio_source_input_index: Optional[int] = None
    if audio_source_path:
        cmd += ["-i", audio_source_path]
        audio_source_input_index = len(segments)

    # --- Filtergraph section ---
    filter_lines, out_labels = _build_filtergraph(
        segments,
        target_width,
        target_height,
        target_fps,
        audio_source_input_index=audio_source_input_index,
        audio_source_offset=audio_source_offset,
        audio_source_duration=audio_source_duration,
    )
    filter_complex = ";".join(filter_lines)

    cmd += ["-filter_complex", filter_complex]

    # Map the final concatenated video and audio streams to the output
    cmd += ["-map", "[vout]", "-map", "[aout]"]

    # --- Video encoding ---
    cmd += ["-c:v", encoder]
    cmd += _encoder_quality_flags(
        encoder=encoder,
        target_width=target_width,
        target_height=target_height,
        target_fps=target_fps,
        event_profile=event_profile,
    )

    # --- Audio encoding ---
    # AAC at 192kbps is transparent quality for stereo speech/music
    _, _, audio_kbps = _event_profile_multipliers(event_profile)
    cmd += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]

    # --- Container / output ---
    # movflags faststart moves the MP4 moov atom to the front of the file,
    # enabling progressive playback / streaming without full download.
    cmd += ["-movflags", "+faststart"]
    cmd += [output_path]

    return cmd


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render(
    segments: List[CutSegment],
    output_path: str,
    target_width: int = 1920,
    target_height: int = 1080,
    target_fps: int = 30,
    encoder: Optional[str] = None,
    event_profile: str = "balanced",
    effect_intensity: str = "balanced",
    transition_style: str = "cut",
    audio_source_path: Optional[str] = None,
    audio_source_offset: float = 0.0,
    audio_source_duration: Optional[float] = None,
) -> str:
    """
    Render a multicam cut list to a single MP4 file.

    This is the primary public entry point for the rendering module.
    It auto-detects the best encoder, builds the FFmpeg filtergraph command,
    and streams the output directly to disk without loading video into memory.

    Args:
        segments:      Ordered CutSegment list from build_cut_list().
        output_path:   Destination path for the output MP4 file.
        target_width:  Output resolution width (default 1920).
        target_height: Output resolution height (default 1080).
        target_fps:    Output frame rate (default 30).
        encoder:       Override the encoder (skips auto-detection). Useful
                       for testing or forcing a specific encoder in CI.
        event_profile: Event-aware profile used to tune bitrate/GOP strategy.

    Returns:
        The output_path string on success.

    Raises:
        ValueError:   If segments list is empty.
        RuntimeError: If FFmpeg exits with a non-zero return code.

    Example:
        from multicam_pipeline import align_videos, build_cut_list
        from multicam_pipeline.rendering import render

        videos = ["cam1.mp4", "cam2.mp4", "cam3.mp4"]
        offsets = align_videos(videos)
        segments = build_cut_list(videos, offsets, cut_interval=5.0)
        render(segments, "output/multicam.mp4")
    """
    if not segments:
        raise ValueError("Cannot render an empty cut list.")

    # Assign themed transitions before render so angle switches have
    # intentional visual identity while preserving matrix-marked segments.
    themed_segments = assign_transition_themes(
        segments,
        event_profile,
        effect_intensity,
        transition_style,
    )

    # Step 1: Resolve encoder
    resolved_encoder = encoder or detect_video_encoder()

    # Step 2: Build the FFmpeg command
    cmd = build_ffmpeg_command(
        segments=themed_segments,
        output_path=output_path,
        encoder=resolved_encoder,
        target_width=target_width,
        target_height=target_height,
        target_fps=target_fps,
        event_profile=event_profile,
        audio_source_path=audio_source_path,
        audio_source_offset=audio_source_offset,
        audio_source_duration=audio_source_duration,
    )

    print(f"[rendering] Starting render → {output_path}")
    print(f"[rendering] Encoder: {resolved_encoder} | "
          f"Resolution: {target_width}x{target_height} @ {target_fps}fps | "
                        f"Segments: {len(themed_segments)} | Profile: {event_profile}")
    print(f"[rendering] FFmpeg command:\n  {' '.join(cmd)}\n")

    # Step 3: Execute FFmpeg — stream stderr live so progress is visible
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # merge stderr into stdout for unified logging
        text=True,
        bufsize=1,                  # line-buffered for real-time output
    )

    # Stream FFmpeg output line by line so the caller sees live progress
    for line in process.stdout:
        print(f"[ffmpeg] {line}", end="")

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"FFmpeg render failed with exit code {process.returncode}. "
            f"See [ffmpeg] output above for details."
        )

    print(f"\n[rendering] ✓ Render complete → {output_path}")
    return output_path
