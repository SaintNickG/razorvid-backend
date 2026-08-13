"""
multicam_cutter.py
------------------
Builds a multicam cut list (timeline) from aligned video tracks using
a configurable interval-based switching strategy. Respects per-video
audio offsets so cuts only reference segments where a given angle is
actually available.

Dependencies:
    pip install dataclasses  # stdlib in Python 3.7+
"""

from dataclasses import dataclass
from typing import List, Dict, Optional
import math


@dataclass
class CutSegment:
    """
    Represents a single cut in the multicam timeline.

    Attributes:
        start_time:       Start time in seconds on the master timeline.
        end_time:         End time in seconds on the master timeline.
        source_video_path: Path to the video file used for this segment.
        offset:           Audio sync offset (seconds) for this video relative
                          to the master reference. Used by the renderer to
                          calculate the correct trim point within the source file.
                          local_time = master_time - offset
        transition_theme: Named visual treatment applied at segment start.
    """
    start_time: float
    end_time: float
    source_video_path: str
    offset: float
    transition_theme: str = "hard_cut"

    @property
    def duration(self) -> float:
        """Duration of this cut segment in seconds."""
        return self.end_time - self.start_time

    @property
    def source_start(self) -> float:
        """
        The actual start position within the source video file.
        Accounts for the audio sync offset so FFmpeg trims correctly.
        """
        return max(0.0, self.start_time - self.offset)

    @property
    def source_end(self) -> float:
        """The actual end position within the source video file."""
        return max(0.0, self.end_time - self.offset)


def _get_video_duration(video_path: str) -> Optional[float]:
    """
    Retrieve video duration in seconds using FFprobe.

    Args:
        video_path: Path to the video file.

    Returns:
        Duration in seconds, or None if it cannot be determined.
    """
    import subprocess
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        try:
            return float(result.stdout.strip())
        except ValueError:
            return None
    return None


def build_cut_list(
    video_paths: List[str],
    offsets: Dict[str, float],
    cut_interval: float = 5.0,
    durations: Optional[Dict[str, float]] = None,
) -> List[CutSegment]:
    """
    Build an automated multicam cut list by switching between available
    angles at a fixed interval across the master timeline.

    Switching strategy:
        - Divide the master timeline into windows of `cut_interval` seconds.
        - For each window, cycle through video angles in order.
        - Skip an angle if the master time window falls outside the range
          where that video has content (based on its offset and duration).
        - If no angle is available for a window, it is dropped from the timeline.

    Args:
        video_paths:   Ordered list of video file paths (same order as align_videos).
        offsets:       Dict of {video_path: offset_seconds} from align_videos().
        cut_interval:  How many seconds each angle holds before switching (default 5s).
        durations:     Optional dict of {video_path: duration_seconds}. If not
                       provided, FFprobe is used to detect durations automatically.

    Returns:
        Ordered list of CutSegment objects representing the full multicam timeline.

    Raises:
        ValueError: If video_paths is empty or cut_interval <= 0.

    Example:
        offsets = {"cam1.mp4": 0.0, "cam2.mp4": 1.5, "cam3.mp4": -0.3}
        segments = build_cut_list(list(offsets.keys()), offsets, cut_interval=4.0)
    """
    if not video_paths:
        raise ValueError("video_paths must not be empty.")
    if cut_interval <= 0:
        raise ValueError("cut_interval must be a positive number of seconds.")

    # Step 1: Resolve durations for all videos
    resolved_durations: Dict[str, float] = {}
    for path in video_paths:
        if durations and path in durations:
            resolved_durations[path] = durations[path]
        else:
            detected = _get_video_duration(path)
            if detected is None:
                print(f"[multicam_cutter] Warning: could not detect duration for '{path}', skipping.")
                resolved_durations[path] = 0.0
            else:
                resolved_durations[path] = detected

    # Step 2: Compute the master timeline bounds
    # Master timeline starts at 0 and ends when the last video finishes.
    # Each video's content spans [offset, offset + duration] on the master timeline.
    master_end = max(
        offsets[p] + resolved_durations[p]
        for p in video_paths
        if resolved_durations[p] > 0
    )

    print(f"[multicam_cutter] Master timeline duration: {master_end:.2f}s")
    print(f"[multicam_cutter] Cut interval: {cut_interval}s | Angles: {len(video_paths)}")

    # Step 3: Walk the master timeline in cut_interval steps
    num_windows = math.ceil(master_end / cut_interval)
    cut_list: List[CutSegment] = []
    angle_index = 0  # cycles through video_paths in round-robin order

    for window in range(num_windows):
        window_start = window * cut_interval
        window_end = min(window_start + cut_interval, master_end)

        # Step 4: Find the next available angle for this window
        # Try each angle starting from the current round-robin position.
        assigned = False
        for attempt in range(len(video_paths)):
            candidate_index = (angle_index + attempt) % len(video_paths)
            candidate_path = video_paths[candidate_index]
            candidate_offset = offsets[candidate_path]
            candidate_duration = resolved_durations[candidate_path]

            # The candidate is available if the window overlaps its content range
            content_start = candidate_offset
            content_end = candidate_offset + candidate_duration

            # Check overlap: window must intersect [content_start, content_end]
            if window_start >= content_end or window_end <= content_start:
                # This angle has no content in this window — try the next
                continue

            # Clamp the window to the actual available content range
            effective_start = max(window_start, content_start)
            effective_end = min(window_end, content_end)

            if effective_end - effective_start < 0.1:
                # Segment too short to be useful (< 100ms), skip
                continue

            cut_list.append(CutSegment(
                start_time=effective_start,
                end_time=effective_end,
                source_video_path=candidate_path,
                offset=candidate_offset,
            ))

            # Advance round-robin to the next angle for the following window
            angle_index = (candidate_index + 1) % len(video_paths)
            assigned = True
            break

        if not assigned:
            print(f"[multicam_cutter] Warning: no available angle for window "
                  f"{window_start:.2f}s–{window_end:.2f}s, dropping.")

    print(f"[multicam_cutter] Generated {len(cut_list)} cut segments.")
    return cut_list


def summarize_cut_list(cut_list: List[CutSegment]) -> None:
    """
    Print a human-readable summary of the cut list to stdout.

    Args:
        cut_list: List of CutSegment objects from build_cut_list().
    """
    print(f"\n{'─' * 70}")
    print(f"{'#':<5} {'Source':<30} {'Start':>8} {'End':>8} {'Dur':>7} {'SrcStart':>10}")
    print(f"{'─' * 70}")
    for i, seg in enumerate(cut_list):
        name = seg.source_video_path.split("/")[-1][:28]
        print(
            f"{i:<5} {name:<30} {seg.start_time:>7.2f}s {seg.end_time:>7.2f}s "
            f"{seg.duration:>6.2f}s {seg.source_start:>9.2f}s"
        )
    total = sum(s.duration for s in cut_list)
    print(f"{'─' * 70}")
    print(f"Total timeline duration: {total:.2f}s | Segments: {len(cut_list)}\n")


def assign_transition_themes(
    cut_list: List[CutSegment],
    event_profile: str,
    effect_intensity: str = "balanced",
) -> List[CutSegment]:
    """
    Assign alternating transition themes when angle changes.

    Existing non-default themes (e.g. matrix moments) are preserved.
    """
    profile = (event_profile or "cheer").lower()
    intensity = (effect_intensity or "balanced").lower()

    if intensity == "subtle":
        if profile == "concert":
            cycle = ["hard_cut", "chroma_pop", "neon_glow"]
        elif profile == "sport":
            cycle = ["hard_cut", "chroma_pop", "flash_punch"]
        else:
            cycle = ["hard_cut", "flash_punch", "chroma_pop"]
    elif intensity == "cinematic":
        if profile == "concert":
            cycle = ["neon_glow", "flash_punch", "chroma_pop", "whip_blur"]
        elif profile == "sport":
            cycle = ["whip_blur", "flash_punch", "chroma_pop", "neon_glow"]
        else:
            cycle = ["flash_punch", "whip_blur", "neon_glow", "chroma_pop"]
    else:
        if profile == "sport":
            cycle = ["hard_cut", "whip_blur", "flash_punch", "chroma_pop"]
        elif profile == "concert":
            cycle = ["hard_cut", "neon_glow", "flash_punch", "chroma_pop"]
        else:
            cycle = ["hard_cut", "flash_punch", "whip_blur", "neon_glow"]

    idx = 0
    prev_path = None
    for seg in cut_list:
        if seg.transition_theme != "hard_cut":
            prev_path = seg.source_video_path
            continue

        if prev_path is None:
            seg.transition_theme = "hard_cut"
        elif seg.source_video_path != prev_path:
            seg.transition_theme = cycle[idx % len(cycle)]
            idx += 1
        else:
            seg.transition_theme = "hard_cut"

        prev_path = seg.source_video_path

    return cut_list
