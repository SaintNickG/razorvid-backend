"""
ai_cutter.py
------------
Event-aware AI cut engine for the multicam pipeline.

Two modes:
    local       — free tier. Uses librosa (audio) + OpenCV (motion) to make
                  intelligent cut decisions entirely on-device. No API costs.

    rekognition — paid tier. Adds AWS Rekognition frame analysis (face detection,
                  activity labels, shot quality) on top of the local signals for
                  broadcast-quality cutting decisions.

Four event profiles:
    cheer   — beat-driven cuts, stunt apex detection, formation change detection
    sport   — motion-driven cuts, action zone tracking, wide/close logic
    concert — beat-driven cuts, performer face tracking, energy scoring
    dance   — movement-driven cuts, choreography tracking, energy scoring

Entry point:
    build_ai_cut_list(video_paths, offsets, job) -> List[CutSegment]

Dependencies:
    pip install librosa numpy opencv-python scipy boto3
"""

import os
import math
import tempfile
import subprocess
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import numpy as np
import librosa
import cv2
import boto3

from multicam_pipeline.multicam_cutter import CutSegment, _get_video_duration, assign_transition_themes
from multicam_pipeline.job_schema import MulticamJob, EventType, CuttingStrategy
from multicam_pipeline.config import (
    MIN_CUT_DURATION,
    MAX_CUT_DURATION,
    REKOGNITION_COST_PER_1000,
    AWS_REGION,
    MOTION_ANALYSIS_FPS,
)

# ---------------------------------------------------------------------------
# Internal signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class AngleSignals:
    """
    Per-angle signals computed during analysis.
    All arrays are aligned to the same time axis (one value per analysis frame).

    Attributes:
        path:           Video file path.
        offset:         Audio sync offset in seconds.
        duration:       Video duration in seconds.
        times:          Time axis in seconds for all signal arrays.
        audio_energy:   RMS audio energy per frame — detects speaker proximity.
        motion_score:   Frame-difference motion score — detects action.
        beat_frames:    Boolean mask — True at beat-aligned frames.
        rek_score:      Rekognition composite quality score (paid tier only).
        view_signature: Compact visual fingerprint for camera-view similarity.
    """
    path:         str
    offset:       float
    duration:     float
    times:        np.ndarray
    audio_energy: np.ndarray
    motion_score: np.ndarray
    beat_frames:  np.ndarray
    rek_score:    Optional[np.ndarray] = None
    view_signature: Optional[np.ndarray] = None


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

TARGET_SR    = 22050
HOP_LENGTH   = 512   # ~23ms per frame at 22050 Hz


def _extract_audio(video_path: str) -> np.ndarray:
    """Extract mono audio from a video file to a numpy array via FFmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-ac", "1", "-ar", str(TARGET_SR), "-vn", "-f", "wav", tmp_path],
            capture_output=True, check=True
        )
        audio, _ = librosa.load(tmp_path, sr=TARGET_SR, mono=True)
        return audio
    finally:
        os.unlink(tmp_path)


def _compute_audio_signals(
    audio: np.ndarray,
    sr: int = TARGET_SR,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute audio energy, beat frames, and time axis from a mono audio array.

    Returns:
        times:        Time in seconds for each analysis frame.
        energy:       Normalized RMS energy per frame [0, 1].
        beat_mask:    Boolean array — True at beat-aligned frames.
    """
    # RMS energy per hop — measures loudness / speaker proximity
    rms    = librosa.feature.rms(y=audio, hop_length=HOP_LENGTH)[0]
    energy = rms / (rms.max() + 1e-8)   # normalize to [0, 1]

    # Beat tracking — returns beat positions in frames
    _, beat_frame_indices = librosa.beat.beat_track(
        y=audio, sr=sr, hop_length=HOP_LENGTH
    )
    beat_mask = np.zeros(len(energy), dtype=bool)
    valid     = beat_frame_indices[beat_frame_indices < len(energy)]
    beat_mask[valid] = True

    times = librosa.frames_to_time(
        np.arange(len(energy)), sr=sr, hop_length=HOP_LENGTH
    )
    return times, energy, beat_mask


# ---------------------------------------------------------------------------
# Motion analysis (OpenCV)
# ---------------------------------------------------------------------------

def _compute_motion_score(
    video_path: str,
    times: np.ndarray,
    offset: float,
) -> np.ndarray:
    """
    Compute per-frame motion score using frame differencing via OpenCV.

    Samples sequential frames at a bounded rate, then interpolates the motion
    curve onto the audio-analysis time axis. Sequential decode avoids expensive
    random seeking at every audio frame for multi-minute renders.

    Args:
        video_path: Path to the video file.
        times:      Time axis in seconds (from audio analysis).
        offset:     Audio sync offset — adjusts seek positions.

    Returns:
        motion: Normalized motion score array aligned to `times`.
    """
    if len(times) == 0:
        return np.zeros(0, dtype=np.float32)

    cap = cv2.VideoCapture(video_path)
    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_step = max(1, int(round(source_fps / MOTION_ANALYSIS_FPS)))
    print(
        f"[ai_cutter]   motion sampling: {MOTION_ANALYSIS_FPS:.1f} fps "
        f"from {source_fps:.1f} fps source"
    )
    sampled_times: List[float] = []
    sampled_motion: List[float] = []
    prev_gray: Optional[np.ndarray] = None
    frame_index = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_index % frame_step == 0:
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            source_time = frame_index / source_fps
            sampled_times.append(source_time + offset)
            if prev_gray is None:
                sampled_motion.append(0.0)
            else:
                sampled_motion.append(float(cv2.absdiff(gray, prev_gray).mean()))
            prev_gray = gray

        frame_index += 1

    cap.release()

    if len(sampled_times) < 2:
        return np.zeros(len(times), dtype=np.float32)

    motion = np.interp(times, sampled_times, sampled_motion, left=0.0, right=0.0).astype(np.float32)
    max_val = float(motion.max())
    if max_val > 0:
        motion /= max_val
    return motion


def _compute_view_signature(video_path: str, duration: float) -> Optional[np.ndarray]:
    """
    Build a compact HSV histogram fingerprint for viewpoint similarity.

    Uses a few representative frames across the clip so similar camera
    locations can be grouped for matrix-style effects.
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        cap.release()
        return None

    sample_times = [max(0.2, duration * r) for r in (0.2, 0.5, 0.8)]
    hists: List[np.ndarray] = []

    for t in sample_times:
        frame_idx = int(t * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            continue
        small = cv2.resize(frame, (192, 108))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 6, 4], [0, 180, 0, 256, 0, 256])
        hist = cv2.normalize(hist, hist).flatten()
        hists.append(hist)

    cap.release()

    if not hists:
        return None
    return np.mean(np.stack(hists, axis=0), axis=0)


def _signature_similarity(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None:
        return 0.0
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _find_similar_view_group(signals: List[AngleSignals], min_similarity: float = 0.90) -> List[AngleSignals]:
    """Find the largest subset of angles that appear to share a similar location/view."""
    if len(signals) < 3:
        return []

    best_group: List[AngleSignals] = []
    for anchor in signals:
        group = [anchor]
        for other in signals:
            if other.path == anchor.path:
                continue
            if _signature_similarity(anchor.view_signature, other.view_signature) >= min_similarity:
                group.append(other)
        if len(group) > len(best_group):
            best_group = group

    return best_group if len(best_group) >= 3 else []


def _resolve_matrix_profile(effect_intensity: str) -> dict:
    """Map effect intensity to matrix detection and splice aggressiveness."""
    intensity = (effect_intensity or "balanced").lower()
    if intensity == "subtle":
        return {
            "min_similarity": 0.94,
            "top_k": 1,
            "percentile": 98.0,
            "min_gap_seconds": 16.0,
            "window_half": 0.55,
            "slice_len": 0.24,
        }
    if intensity == "cinematic":
        return {
            "min_similarity": 0.85,
            "top_k": 3,
            "percentile": 93.0,
            "min_gap_seconds": 8.0,
            "window_half": 1.05,
            "slice_len": 0.14,
        }
    return {
        "min_similarity": 0.90,
        "top_k": 2,
        "percentile": 96.0,
        "min_gap_seconds": 12.0,
        "window_half": 0.75,
        "slice_len": 0.18,
    }


def _find_highlight_peaks(
    signals: List[AngleSignals],
    timeline_end: float,
    top_k: int = 2,
    percentile: float = 96.0,
    min_gap_seconds: float = 12.0,
) -> List[float]:
    """Find strong action peaks from aggregate audio+motion curves."""
    if not signals:
        return []

    n = min(len(s.times) for s in signals)
    if n <= 0:
        return []

    times = signals[0].times[:n]
    agg = np.zeros(n, dtype=np.float32)
    for sig in signals:
        agg += 0.55 * sig.motion_score[:n] + 0.45 * sig.audio_energy[:n]
    agg /= max(1, len(signals))

    thr = float(np.percentile(agg, percentile))
    candidates: List[Tuple[float, float]] = []
    for i in range(1, n - 1):
        if agg[i] >= thr and agg[i] >= agg[i - 1] and agg[i] >= agg[i + 1]:
            t = float(times[i])
            if 0.7 < t < (timeline_end - 0.7):
                candidates.append((float(agg[i]), t))

    candidates.sort(reverse=True)
    selected: List[float] = []
    for _, t in candidates:
        if all(abs(t - s) >= min_gap_seconds for s in selected):
            selected.append(t)
        if len(selected) >= top_k:
            break

    return sorted(selected)


def _splice_matrix_moment(
    cut_list: List[CutSegment],
    peak_t: float,
    similar_group: List[AngleSignals],
    window_half: float = 0.75,
    slice_len: float = 0.18,
) -> List[CutSegment]:
    """Replace a short window around a highlight with rapid angle-rotation segments."""
    if len(similar_group) < 3:
        return cut_list

    start = peak_t - window_half
    end = peak_t + window_half
    if end <= start:
        return cut_list

    kept: List[CutSegment] = []
    for seg in cut_list:
        if seg.end_time <= start or seg.start_time >= end:
            kept.append(seg)
            continue
        if seg.start_time < start:
            kept.append(CutSegment(
                start_time=seg.start_time,
                end_time=start,
                source_video_path=seg.source_video_path,
                offset=seg.offset,
                transition_theme=seg.transition_theme,
            ))
        if seg.end_time > end:
            kept.append(CutSegment(
                start_time=end,
                end_time=seg.end_time,
                source_video_path=seg.source_video_path,
                offset=seg.offset,
                transition_theme=seg.transition_theme,
            ))

    similar_group = sorted(similar_group, key=lambda s: s.path)
    t = start
    idx = 0
    matrix_segments: List[CutSegment] = []
    while t < end - 1e-6:
        next_t = min(end, t + slice_len)
        angle = similar_group[idx % len(similar_group)]
        matrix_segments.append(CutSegment(
            start_time=t,
            end_time=next_t,
            source_video_path=angle.path,
            offset=angle.offset,
            transition_theme="matrix_pan",
        ))
        t = next_t
        idx += 1

    merged = kept + matrix_segments
    merged.sort(key=lambda s: s.start_time)
    return merged


# ---------------------------------------------------------------------------
# AWS Rekognition scoring (paid tier)
# ---------------------------------------------------------------------------

def _extract_frame_jpeg(video_path: str, time_sec: float) -> Optional[bytes]:
    """Extract a single frame from a video at a given time as JPEG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(time_sec), "-i", video_path,
             "-vframes", "1", "-f", "image2", tmp_path],
            capture_output=True
        )
        if result.returncode != 0:
            return None
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _rekognition_score_frames(
    video_path: str,
    times: np.ndarray,
    offset: float,
    sample_rate: int,
    event_type: EventType,
) -> np.ndarray:
    """
    Score every Nth frame using AWS Rekognition DetectLabels and DetectFaces.

    Composite score per frame:
        - Face count × 0.4       (more faces = more interesting angle)
        - Activity confidence × 0.4  (jumping, dancing, performing)
        - Image sharpness × 0.2  (avoid blurry frames)

    Scores are interpolated between sampled frames so the output array
    aligns with the full `times` axis.

    Args:
        video_path:  Path to the video file.
        times:       Full time axis from audio analysis.
        offset:      Audio sync offset in seconds.
        sample_rate: Analyze every Nth frame (configurable per job).
        event_type:  Drives which Rekognition labels boost the score.

    Returns:
        scores: Normalized composite score array aligned to `times`.
    """
    rek    = boto3.client("rekognition", region_name=AWS_REGION)
    scores = np.zeros(len(times), dtype=np.float32)

    # Labels that boost score per event type
    BOOST_LABELS: Dict[EventType, List[str]] = {
        EventType.CHEER:   ["Cheerleading", "Gymnastics", "Dance", "Jumping", "Acrobatics", "Crowd"],
        EventType.SPORT:   ["Sport", "Ball", "Running", "Jumping", "Athlete", "Competition"],
        EventType.CONCERT: ["Concert", "Performance", "Music", "Crowd", "Stage", "Microphone"],
        EventType.DANCE:   ["Dance", "Dancing", "Performance", "Person", "Stage", "Choreography"],
    }
    boost_labels = BOOST_LABELS.get(event_type, [])

    sampled_indices = list(range(0, len(times), sample_rate))
    sampled_scores  = {}

    for i in sampled_indices:
        source_t = max(0.0, times[i] - offset)
        jpeg     = _extract_frame_jpeg(video_path, source_t)
        if jpeg is None:
            sampled_scores[i] = 0.0
            continue

        frame_score = 0.0

        try:
            # Face detection — more faces = more interesting angle
            face_resp  = rek.detect_faces(
                Image={"Bytes": jpeg},
                Attributes=["DEFAULT"]
            )
            face_count = len(face_resp.get("FaceDetails", []))
            face_score = min(face_count / 3.0, 1.0)  # cap at 3 faces = 1.0

            # Label detection — boost for event-relevant activity
            label_resp    = rek.detect_labels(
                Image={"Bytes": jpeg},
                MaxLabels=20,
                MinConfidence=60.0
            )
            label_names   = [l["Name"] for l in label_resp.get("Labels", [])]
            boost_hit     = any(l in label_names for l in boost_labels)
            activity_conf = max(
                (l["Confidence"] for l in label_resp.get("Labels", [])
                 if l["Name"] in boost_labels),
                default=0.0
            ) / 100.0

            # Image quality — use face sharpness if available
            sharpness = 0.5
            if face_resp.get("FaceDetails"):
                sharpness = face_resp["FaceDetails"][0].get(
                    "Quality", {}
                ).get("Sharpness", 50.0) / 100.0

            frame_score = (
                face_score     * 0.4 +
                activity_conf  * 0.4 +
                sharpness      * 0.2
            )

        except Exception as e:
            print(f"[ai_cutter] Rekognition error at t={times[i]:.2f}s: {e}")
            frame_score = 0.0

        sampled_scores[i] = frame_score

    # Interpolate scores between sampled frames
    sample_x = np.array(sorted(sampled_scores.keys()))
    sample_y = np.array([sampled_scores[k] for k in sample_x])

    if len(sample_x) >= 2:
        scores = np.interp(np.arange(len(times)), sample_x, sample_y)
    elif len(sample_x) == 1:
        scores[:] = sample_y[0]

    # Normalize
    max_s = scores.max()
    if max_s > 0:
        scores /= max_s

    return scores


# ---------------------------------------------------------------------------
# Per-event cut decision logic
# ---------------------------------------------------------------------------

def _cheer_angle_score(
    sig: AngleSignals,
    frame_idx: int,
    strategy: CuttingStrategy,
) -> float:
    """
    Score an angle at a given frame for cheerleading content.

    Cheerleading scoring weights:
        - Audio energy  0.35  (proximity to music/crowd)
        - Motion score  0.35  (tumbling, stunts, jumps)
        - Beat aligned  0.15  (cuts on the beat feel natural)
        - Rek score     0.15  (paid: face count + activity labels)
    """
    i = min(frame_idx, len(sig.audio_energy) - 1)

    score = (
        sig.audio_energy[i] * 0.35 +
        sig.motion_score[i] * 0.35 +
        (0.15 if sig.beat_frames[i] else 0.0)
    )

    if strategy == CuttingStrategy.REKOGNITION and sig.rek_score is not None:
        score += sig.rek_score[i] * 0.15
    else:
        # Without Rekognition, redistribute its weight to motion
        score += sig.motion_score[i] * 0.15

    return float(score)


def _sport_angle_score(
    sig: AngleSignals,
    frame_idx: int,
    strategy: CuttingStrategy,
) -> float:
    """
    Score an angle at a given frame for general sports content.

    Sports scoring weights:
        - Motion score  0.50  (action is the primary signal)
        - Audio energy  0.25  (crowd reaction)
        - Beat aligned  0.10  (less important for sports)
        - Rek score     0.15  (paid: activity detection)
    """
    i = min(frame_idx, len(sig.audio_energy) - 1)

    score = (
        sig.motion_score[i]  * 0.50 +
        sig.audio_energy[i]  * 0.25 +
        (0.10 if sig.beat_frames[i] else 0.0)
    )

    if strategy == CuttingStrategy.REKOGNITION and sig.rek_score is not None:
        score += sig.rek_score[i] * 0.15
    else:
        score += sig.motion_score[i] * 0.15

    return float(score)


def _concert_angle_score(
    sig: AngleSignals,
    frame_idx: int,
    strategy: CuttingStrategy,
) -> float:
    """
    Score an angle at a given frame for concert content.

    Concert scoring weights:
        - Beat aligned  0.30  (cuts on the beat are essential for music)
        - Audio energy  0.30  (proximity to performer/PA)
        - Motion score  0.25  (performer movement)
        - Rek score     0.15  (paid: face/performer detection)
    """
    i = min(frame_idx, len(sig.audio_energy) - 1)

    score = (
        (0.30 if sig.beat_frames[i] else 0.0) +
        sig.audio_energy[i] * 0.30 +
        sig.motion_score[i] * 0.25
    )

    if strategy == CuttingStrategy.REKOGNITION and sig.rek_score is not None:
        score += sig.rek_score[i] * 0.15
    else:
        score += sig.audio_energy[i] * 0.15

    return float(score)


# Map event type to its scoring function
_SCORE_FN = {
    EventType.CHEER:   _cheer_angle_score,
    EventType.SPORT:   _sport_angle_score,
    EventType.CONCERT: _concert_angle_score,
}


# ---------------------------------------------------------------------------
# Cut decision engine
# ---------------------------------------------------------------------------

def _pick_best_angle(
    signals:      List[AngleSignals],
    frame_idx:    int,
    event_type:   EventType,
    strategy:     CuttingStrategy,
    last_path:    Optional[str],
    last_cut_t:   float,
    current_t:    float,
) -> Optional[AngleSignals]:
    """
    Pick the best angle at a given frame using event-aware scoring.

    Rules:
        - Never pick an angle that hasn't started or has already ended.
        - Enforce MIN_CUT_DURATION — don't cut too soon after the last cut.
        - Enforce MAX_CUT_DURATION — force a switch if on same angle too long.
        - Apply a small penalty for repeating the same angle consecutively
          to encourage variety.

    Returns:
        The best AngleSignals, or None if no valid angle is available.
    """
    score_fn      = _SCORE_FN[event_type]
    time_on_angle = current_t - last_cut_t
    scores        = []

    for sig in signals:
        # Skip angles with no content at this time
        content_start = sig.offset
        content_end   = sig.offset + sig.duration
        if current_t < content_start or current_t >= content_end:
            continue

        s = score_fn(sig, frame_idx, strategy)

        # Small penalty for repeating the same angle — encourages variety
        if sig.path == last_path:
            s *= 0.6

        scores.append((s, sig))

    if not scores:
        return None

    # Force a switch if we've been on the same angle too long
    if time_on_angle >= MAX_CUT_DURATION and last_path is not None:
        scores = [(s, sig) for s, sig in scores if sig.path != last_path]
        if not scores:
            return None

    # Sort by score descending and return the best
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores[0][1]


# ---------------------------------------------------------------------------
# Signal analysis pipeline
# ---------------------------------------------------------------------------

def _analyze_angle(
    path:        str,
    offset:      float,
    duration:    float,
    strategy:    CuttingStrategy,
    event_type:  EventType,
    sample_rate: int,
) -> AngleSignals:
    """
    Run the full signal analysis pipeline for a single video angle.

    Steps:
        1. Extract mono audio
        2. Compute RMS energy + beat detection
        3. Compute motion score via OpenCV frame differencing
        4. (Paid) Run Rekognition on every Nth frame

    Args:
        path:        Video file path.
        offset:      Audio sync offset in seconds.
        duration:    Video duration in seconds.
        strategy:    local | rekognition.
        event_type:  Drives Rekognition label boosting.
        sample_rate: Rekognition frame sampling rate (paid only).

    Returns:
        AngleSignals populated with all computed signals.
    """
    print(f"[ai_cutter] Analyzing '{os.path.basename(path)}'...")

    # Step 1 + 2: Audio signals
    audio              = _extract_audio(path)
    times, energy, beats = _compute_audio_signals(audio)

    # Step 3: Motion scoring
    print(f"[ai_cutter]   → computing motion score...")
    motion = _compute_motion_score(path, times, offset)

    view_signature = _compute_view_signature(path, duration)

    # Step 4: Rekognition (paid tier only)
    rek_score = None
    if strategy == CuttingStrategy.REKOGNITION:
        print(f"[ai_cutter]   → running Rekognition (every {sample_rate} frames)...")
        rek_score = _rekognition_score_frames(
            path, times, offset, sample_rate, event_type
        )

    return AngleSignals(
        path         = path,
        offset       = offset,
        duration     = duration,
        times        = times,
        audio_energy = energy,
        motion_score = motion,
        beat_frames  = beats,
        rek_score    = rek_score,
        view_signature = view_signature,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_ai_cut_list(
    video_paths: List[str],
    offsets:     Dict[str, float],
    job:         MulticamJob,
    durations:   Optional[Dict[str, float]] = None,
) -> List[CutSegment]:
    """
    Build an AI-driven multicam cut list using event-aware signal analysis.

    This is the primary entry point replacing build_cut_list() for AI mode.
    Falls back to interval cutting if fewer than 2 angles have valid signals.

    Algorithm:
        1. Analyze all angles in parallel (audio + motion + optional Rekognition)
        2. Walk the master timeline frame by frame
        3. At each valid cut point (beat-aligned or MIN_CUT_DURATION elapsed),
           score all available angles and pick the best
        4. Enforce MIN/MAX cut duration rules
        5. Build CutSegment list from the decisions

    Args:
        video_paths: Ordered list of video file paths.
        offsets:     Dict of {path: offset_seconds} from align_videos().
        job:         MulticamJob with cutting_strategy, event_type, sample_rate.
        durations:   Optional pre-computed durations dict.

    Returns:
        Ordered list of CutSegment objects for the FFmpeg renderer.

    Raises:
        ValueError: If fewer than 2 video paths are provided.
    """
    if len(video_paths) < 2:
        raise ValueError("At least 2 video paths are required.")

    # ---------------------------------------------------------------------------
    # Step 1: Resolve durations
    # ---------------------------------------------------------------------------
    resolved: Dict[str, float] = {}
    for path in video_paths:
        if durations and path in durations:
            resolved[path] = durations[path]
        else:
            d = _get_video_duration(path)
            resolved[path] = d if d else 0.0

    master_end = max(
        offsets[p] + resolved[p]
        for p in video_paths
        if resolved[p] > 0
    )

    print(f"[ai_cutter] Master timeline: {master_end:.2f}s | "
          f"Strategy: {job.cutting_strategy.value} | "
          f"Event: {job.event_type.value}")

    # ---------------------------------------------------------------------------
    # Step 2: Analyze all angles
    # ---------------------------------------------------------------------------
    signals: List[AngleSignals] = []
    for path in video_paths:
        if resolved[path] <= 0:
            print(f"[ai_cutter] Skipping '{path}' — zero duration.")
            continue
        sig = _analyze_angle(
            path        = path,
            offset      = offsets[path],
            duration    = resolved[path],
            strategy    = job.cutting_strategy,
            event_type  = job.event_type,
            sample_rate = job.rekognition_sample_rate,
        )
        signals.append(sig)

    if len(signals) < 2:
        print("[ai_cutter] Warning: fewer than 2 valid angles — falling back to interval cutting.")
        from multicam_pipeline.multicam_cutter import build_cut_list
        return build_cut_list(video_paths, offsets, job.cut_interval, durations)

    # ---------------------------------------------------------------------------
    # Step 3: Walk the master timeline and make cut decisions
    # ---------------------------------------------------------------------------
    # Use the reference angle's time axis as the master clock
    ref_times  = signals[0].times
    cut_list:  List[CutSegment] = []
    last_path: Optional[str]   = None
    last_cut_t: float          = 0.0
    seg_start:  float          = 0.0
    current_angle: Optional[AngleSignals] = None

    for i, t in enumerate(ref_times):
        # Only consider times within the master timeline
        if t > master_end:
            break

        time_since_cut = t - last_cut_t

        # Determine if this is a valid cut point:
        # - Must have been on current angle for at least MIN_CUT_DURATION
        # - Prefer beat-aligned frames for natural-feeling cuts
        # - Force cut if MAX_CUT_DURATION exceeded
        is_beat         = any(sig.beat_frames[min(i, len(sig.beat_frames)-1)] for sig in signals)
        force_cut       = time_since_cut >= MAX_CUT_DURATION
        natural_cut     = time_since_cut >= MIN_CUT_DURATION and is_beat
        is_valid_cut    = force_cut or natural_cut or current_angle is None

        if not is_valid_cut:
            continue

        # Pick the best angle at this moment
        best = _pick_best_angle(
            signals     = signals,
            frame_idx   = i,
            event_type  = job.event_type,
            strategy    = job.cutting_strategy,
            last_path   = last_path,
            last_cut_t  = last_cut_t,
            current_t   = t,
        )

        if best is None:
            continue

        # If the angle changed, close the previous segment and open a new one
        if current_angle is not None and best.path != current_angle.path:
            seg_end = t
            if seg_end - seg_start >= 0.1:
                cut_list.append(CutSegment(
                    start_time        = seg_start,
                    end_time          = seg_end,
                    source_video_path = current_angle.path,
                    offset            = current_angle.offset,
                ))
            seg_start  = t
            last_cut_t = t
            last_path  = current_angle.path

        current_angle = best

        # Initialize on first valid angle
        if last_path is None:
            last_path  = best.path
            last_cut_t = t

    # ---------------------------------------------------------------------------
    # Step 4: Close the final segment
    # ---------------------------------------------------------------------------
    if current_angle is not None and master_end - seg_start >= 0.1:
        cut_list.append(CutSegment(
            start_time        = seg_start,
            end_time          = master_end,
            source_video_path = current_angle.path,
            offset            = current_angle.offset,
        ))

    cut_list = assign_transition_themes(
        cut_list,
        job.event_type,
        job.effect_intensity,
    )

    # Optional matrix-style highlight treatment for cheer events when enough
    # similar-view angles are available.
    if job.event_type == EventType.CHEER:
        matrix_profile = _resolve_matrix_profile(str(job.effect_intensity))
        similar_group = _find_similar_view_group(
            signals,
            min_similarity=float(matrix_profile["min_similarity"]),
        )
        if similar_group:
            peaks = _find_highlight_peaks(
                signals,
                timeline_end=master_end,
                top_k=int(matrix_profile["top_k"]),
                percentile=float(matrix_profile["percentile"]),
                min_gap_seconds=float(matrix_profile["min_gap_seconds"]),
            )
            for peak_t in peaks:
                cut_list = _splice_matrix_moment(
                    cut_list,
                    peak_t,
                    similar_group,
                    window_half=float(matrix_profile["window_half"]),
                    slice_len=float(matrix_profile["slice_len"]),
                )
            if peaks:
                print(
                    f"[ai_cutter] Injected matrix moments at "
                    f"{', '.join(f'{p:.2f}s' for p in peaks)} "
                    f"using {len(similar_group)} similar-view angles "
                    f"(intensity={job.effect_intensity.value})."
                )

    print(f"[ai_cutter] Generated {len(cut_list)} AI-driven cut segments.")
    return cut_list


# ---------------------------------------------------------------------------
# Cost estimation utility (used by frontend slider)
# ---------------------------------------------------------------------------

def estimate_rekognition_cost(
    video_duration_seconds: float,
    fps:                    float,
    sample_rate:            int,
    num_angles:             int,
) -> dict:
    """
    Estimate the AWS Rekognition cost for a given job configuration.

    Called by the FastAPI cost-estimate endpoint to power the frontend
    cost/quality tradeoff slider.

    Args:
        video_duration_seconds: Total duration of the event in seconds.
        fps:                    Video frame rate.
        sample_rate:            Analyze every Nth frame.
        num_angles:             Number of camera angles.

    Returns:
        Dict with frame counts and estimated USD cost.
    """
    total_frames    = int(video_duration_seconds * fps)
    sampled_frames  = math.ceil(total_frames / sample_rate)
    # Each sampled frame = 2 Rekognition calls (DetectLabels + DetectFaces)
    total_api_calls = sampled_frames * 2 * num_angles
    cost_usd        = (total_api_calls / 1000) * REKOGNITION_COST_PER_1000

    return {
        "total_frames":    total_frames,
        "sampled_frames":  sampled_frames * num_angles,
        "total_api_calls": total_api_calls,
        "estimated_cost":  round(cost_usd, 4),
        "sample_rate":     sample_rate,
        "num_angles":      num_angles,
    }
