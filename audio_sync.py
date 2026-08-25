"""
audio_sync.py
-------------
Audio-based video synchronization using Generalized Cross-Correlation
with Phase Transform (GCC-PHAT) to compute time offsets between video
files relative to a master reference track.

Dependencies:
    pip install librosa scipy numpy ffmpeg-python
"""

import subprocess
import tempfile
import os
import numpy as np
import librosa
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional


# Sample rate used for all audio extraction and correlation
TARGET_SR = 22050
HOP_LENGTH = 512


@dataclass
class AudioReferenceChoice:
    """Selected master audio reference and its scoring details."""
    path: str
    index: int
    score: float


@dataclass
class AlignmentResult:
    """Detailed alignment result for a single video against the reference."""
    delay_seconds: float
    confidence: float
    peak_ratio: float
    agreement_std_seconds: float
    agreement_support: float
    modality_support: int


@dataclass
class MatchThresholds:
    """Validation thresholds for deciding whether an alignment is reliable."""
    min_confidence: float
    min_peak_ratio: float
    max_agreement_std_seconds: float
    min_modality_support: int
    min_peak_ratio_with_partial_support: float


@dataclass
class ReferenceAttempt:
    """Alignment attempt summary for one chosen reference track."""
    reference: AudioReferenceChoice
    offsets: Dict[str, float]
    results: Dict[str, AlignmentResult]
    weak_matches: List[tuple[str, float, float, float, float, int]]
    avg_confidence: float


def _extract_mono_audio(video_path: str, sr: int = TARGET_SR) -> np.ndarray:
    """
    Extract mono audio from a video file using FFmpeg into a temporary WAV,
    then load it with librosa at the target sample rate.

    Args:
        video_path: Absolute or relative path to the input video file.
        sr: Target sample rate in Hz (default 22050).

    Returns:
        1D numpy array of normalized float32 audio samples.

    Raises:
        RuntimeError: If FFmpeg fails to extract audio.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ac", "1",             # downmix to mono
            "-ar", str(sr),         # resample to target SR
            "-vn",                  # strip video stream
            "-f", "wav",
            tmp_path
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg audio extraction failed for {video_path}:\n"
                f"{result.stderr.decode()}"
            )

        # librosa loads and normalizes to float32 in [-1.0, 1.0]
        audio, _ = librosa.load(tmp_path, sr=sr, mono=True)
        return audio

    finally:
        os.unlink(tmp_path)


def _gcc_phat(sig: np.ndarray, ref: np.ndarray, sr: int = TARGET_SR) -> AlignmentResult:
    """
    Compute the time delay between two signals using GCC-PHAT.

    GCC-PHAT whitens the cross-power spectrum before taking the IFFT,
    which sharpens the correlation peak and improves delay estimation
    accuracy in reverberant or noisy conditions vs standard cross-correlation.

    Algorithm:
        1. Zero-pad both signals and compute their FFTs
        2. Compute cross-power spectrum: X = FFT(sig) * conj(FFT(ref))
        3. Whiten by magnitude: X_phat = X / |X|
        4. IFFT to get the sharpened GCC-PHAT correlation function
        5. Find the peak lag index and convert to seconds

    Args:
        sig: Audio signal to align (1D float32 array).
        ref: Reference audio signal (1D float32 array).
        sr: Sample rate shared by both signals.

    Returns:
        AlignmentResult containing delay and match-confidence metrics.
    """
    n = len(sig) + len(ref) - 1
    # Round up to next power of 2 for efficient FFT computation
    n_fft = 1 << (n - 1).bit_length()

    # Step 1: FFT both signals with zero-padding
    SIG = np.fft.rfft(sig, n=n_fft)
    REF = np.fft.rfft(ref, n=n_fft)

    # Step 2: Cross-power spectrum
    cross_power = SIG * np.conj(REF)

    # Step 3: PHAT weighting — whiten by dividing by magnitude
    magnitude = np.abs(cross_power)
    # Guard against division by zero
    magnitude = np.where(magnitude == 0, 1e-10, magnitude)
    cross_power_phat = cross_power / magnitude

    # Step 4: IFFT produces the sharpened correlation function
    gcc = np.fft.irfft(cross_power_phat, n=n_fft)

    # Step 5: Rearrange so lag=0 is at center index, then find peak
    gcc = np.concatenate((gcc[-(len(ref) - 1):], gcc[:len(sig)]))
    peak_index = int(np.argmax(gcc))
    peak_value = float(gcc[peak_index])

    # Compare strongest peak to the next-best candidate outside a small
    # neighborhood to quantify ambiguity in periodic/noisy signals.
    exclusion = max(1, int(0.02 * sr))
    secondary = gcc.copy()
    lo = max(0, peak_index - exclusion)
    hi = min(len(secondary), peak_index + exclusion + 1)
    secondary[lo:hi] = -np.inf
    second_peak = float(np.max(secondary)) if np.isfinite(np.max(secondary)) else 0.0
    peak_ratio = peak_value / (abs(second_peak) + 1e-12)

    # Robust prominence score relative to distribution spread.
    median_val = float(np.median(gcc))
    std_val = float(np.std(gcc)) + 1e-12
    prominence_z = (peak_value - median_val) / std_val

    ratio_score = min(1.0, max(0.0, (peak_ratio - 1.0) / 4.0))
    prominence_score = min(1.0, max(0.0, prominence_z / 12.0))
    confidence = 0.65 * ratio_score + 0.35 * prominence_score

    # Convert peak index to signed lag relative to center
    center = len(ref) - 1
    lag_samples = peak_index - center

    return AlignmentResult(
        delay_seconds=lag_samples / sr,
        confidence=confidence,
        peak_ratio=peak_ratio,
        agreement_std_seconds=0.0,
        agreement_support=1.0,
        modality_support=1,
    )


def _fuse_alignment_results(results: List[AlignmentResult]) -> AlignmentResult:
    """Fuse multiple full-track alignment estimates into one robust result."""
    if not results:
        return AlignmentResult(
            delay_seconds=0.0,
            confidence=0.0,
            peak_ratio=0.0,
            agreement_std_seconds=0.0,
            agreement_support=0.0,
            modality_support=0,
        )

    delays = np.array([r.delay_seconds for r in results], dtype=np.float64)
    weights = np.array([max(1e-6, r.confidence) for r in results], dtype=np.float64)

    # Find the densest agreement cluster across delay candidates.
    cluster_radius = 0.22
    best_center = delays[0]
    best_support = -1.0
    for d in delays:
        support = float(np.sum(weights[np.abs(delays - d) <= cluster_radius]))
        if support > best_support:
            best_support = support
            best_center = d

    inliers = np.abs(delays - best_center) <= cluster_radius
    if not np.any(inliers):
        inliers = np.ones_like(delays, dtype=bool)

    fused_delay = float(np.average(delays[inliers], weights=weights[inliers]))
    support_fraction = float(np.sum(weights[inliers]) / (np.sum(weights) + 1e-12))
    fused_confidence = float(min(1.0, np.mean([r.confidence for r in results]) * support_fraction * 1.25))
    fused_peak_ratio = float(max(r.peak_ratio for r in results))
    agreement_std = float(np.std(delays[inliers])) if np.sum(inliers) > 1 else 0.0
    modality_support = int(sum(1 for r in results if r.confidence >= 0.45))

    return AlignmentResult(
        delay_seconds=fused_delay,
        confidence=fused_confidence,
        peak_ratio=fused_peak_ratio,
        agreement_std_seconds=agreement_std,
        agreement_support=support_fraction,
        modality_support=modality_support,
    )


def _prepare_alignment_signal(audio: np.ndarray) -> np.ndarray:
    """
    Condition raw waveform for more stable cross-correlation.

    Steps:
        - DC removal
        - Peak normalization
        - Pre-emphasis to highlight transients over low-frequency rumble
    """
    if audio.size == 0:
        return audio

    y = np.asarray(audio, dtype=np.float32)
    y = y - np.mean(y)

    peak = float(np.max(np.abs(y))) + 1e-12
    y = y / peak

    # Simple pre-emphasis filter: y[n] - a*y[n-1]
    a = 0.97
    y = np.append(y[0], y[1:] - a * y[:-1]).astype(np.float32)
    return y


def _align_full_track_multifeature(sig: np.ndarray, ref: np.ndarray) -> AlignmentResult:
    """
    Align using the entire track with multiple feature views.

    This improves robustness when the best sync marker is not musical
    (e.g., crowd shout, clap, whistle, dog bark).
    """
    sig = _prepare_alignment_signal(sig)
    ref = _prepare_alignment_signal(ref)

    if sig.size < 256 or ref.size < 256:
        return AlignmentResult(
            delay_seconds=0.0,
            confidence=0.0,
            peak_ratio=0.0,
            agreement_std_seconds=0.0,
            agreement_support=0.0,
            modality_support=0,
        )

    # Waveform GCC-PHAT over full tracks.
    waveform_res = _gcc_phat(sig, ref, sr=TARGET_SR)

    # Onset envelope captures sharp events regardless of content type.
    sig_onset = librosa.onset.onset_strength(y=sig, sr=TARGET_SR, hop_length=HOP_LENGTH)
    ref_onset = librosa.onset.onset_strength(y=ref, sr=TARGET_SR, hop_length=HOP_LENGTH)
    onset_sr = TARGET_SR / HOP_LENGTH
    onset_res = _gcc_phat(sig_onset, ref_onset, sr=onset_sr)

    # RMS envelope captures sustained loudness structure over full duration.
    sig_rms = librosa.feature.rms(y=sig, hop_length=HOP_LENGTH)[0]
    ref_rms = librosa.feature.rms(y=ref, hop_length=HOP_LENGTH)[0]
    rms_res = _gcc_phat(sig_rms, ref_rms, sr=onset_sr)

    # Percussive onset envelope improves reliability for claps/shouts/hits.
    _, sig_perc = librosa.effects.hpss(sig)
    _, ref_perc = librosa.effects.hpss(ref)
    sig_perc_onset = librosa.onset.onset_strength(y=sig_perc, sr=TARGET_SR, hop_length=HOP_LENGTH)
    ref_perc_onset = librosa.onset.onset_strength(y=ref_perc, sr=TARGET_SR, hop_length=HOP_LENGTH)
    perc_res = _gcc_phat(sig_perc_onset, ref_perc_onset, sr=onset_sr)

    return _fuse_alignment_results([waveform_res, onset_res, rms_res, perc_res])


def _align_with_window_consensus(sig: np.ndarray, ref: np.ndarray) -> AlignmentResult:
    """Add high-confidence local windows as corroborating evidence for full-track sync."""
    full_track_result = _align_full_track_multifeature(sig, ref)
    if full_track_result.confidence >= 0.60 and full_track_result.peak_ratio >= 1.50:
        return full_track_result

    candidates = [full_track_result]
    window_samples = 20 * TARGET_SR
    shared_length = min(len(sig), len(ref))

    if shared_length < window_samples:
        return candidates[0]

    for fraction in (0.0, 0.4, 0.8):
        start = int((shared_length - window_samples) * fraction)
        window_result = _align_full_track_multifeature(
            sig[start:start + window_samples],
            ref[start:start + window_samples],
        )
        if window_result.confidence >= 0.35 and window_result.peak_ratio >= 1.2:
            candidates.append(window_result)

    consensus = _fuse_alignment_results(candidates)
    return consensus if consensus.confidence > full_track_result.confidence else full_track_result


def _resolve_match_thresholds(
    event_type: Optional[str],
    cutting_strategy: Optional[str],
) -> MatchThresholds:
    """
    Tune thresholds per content profile and cutting mode.

    Goal: reduce false positives (bad sync accepted) without over-triggering
    false negatives (good sync rejected).
    """
    profile = (event_type or "cheer").lower()

    if profile == "concert":
        thresholds = MatchThresholds(
            min_confidence=0.36,
            min_peak_ratio=1.35,
            max_agreement_std_seconds=0.16,
            min_modality_support=2,
            min_peak_ratio_with_partial_support=2.2,
        )
    elif profile == "sport":
        thresholds = MatchThresholds(
            min_confidence=0.42,
            min_peak_ratio=1.55,
            max_agreement_std_seconds=0.20,
            min_modality_support=2,
            min_peak_ratio_with_partial_support=2.4,
        )
    else:  # cheer default
        thresholds = MatchThresholds(
            min_confidence=0.40,
            min_peak_ratio=1.50,
            max_agreement_std_seconds=0.18,
            min_modality_support=2,
            min_peak_ratio_with_partial_support=2.3,
        )

    # Local AI mode should be stricter because visual cuts heavily depend on sync.
    if (cutting_strategy or "").lower() == "local":
        thresholds.min_confidence = min(0.60, thresholds.min_confidence + 0.03)
        thresholds.max_agreement_std_seconds = max(0.10, thresholds.max_agreement_std_seconds - 0.02)
        thresholds.min_modality_support = 2
        thresholds.min_peak_ratio_with_partial_support = max(
            thresholds.min_peak_ratio_with_partial_support,
            thresholds.min_peak_ratio + 1.0,
        )

    return thresholds


def _audio_quality_score(audio: np.ndarray, max_length: int) -> float:
    """
    Heuristic audio quality score balancing completeness and signal clarity.

    Score components:
        - Duration coverage (most complete track)
        - Active/voiced ratio (less silence)
        - Dynamic spread between quiet and loud regions
        - Clipping penalty
    """
    if audio.size == 0 or max_length <= 0:
        return 0.0

    abs_audio = np.abs(audio)

    duration_norm = min(1.0, len(audio) / max_length)
    active_ratio = float(np.mean(abs_audio > 0.015))
    active_norm = min(1.0, active_ratio / 0.60)

    p20 = float(np.percentile(abs_audio, 20))
    p95 = float(np.percentile(abs_audio, 95))
    spread = max(0.0, p95 - p20)
    spread_norm = min(1.0, spread / 0.25)

    clipping_ratio = float(np.mean(abs_audio >= 0.98))
    clipping_norm = max(0.0, 1.0 - min(1.0, clipping_ratio / 0.02))

    return (
        0.45 * duration_norm
        + 0.25 * active_norm
        + 0.20 * spread_norm
        + 0.10 * clipping_norm
    )


def _select_audio_reference(video_paths: List[str], audio_tracks: List[np.ndarray]) -> AudioReferenceChoice:
    """Pick the best audio reference track from the provided angles."""
    max_length = max((len(track) for track in audio_tracks), default=1)
    scored = []

    for idx, (path, audio) in enumerate(zip(video_paths, audio_tracks)):
        score = _audio_quality_score(audio, max_length=max_length)
        scored.append((score, idx, path))

    best_score, best_idx, best_path = max(scored, key=lambda x: x[0])
    print(
        f"[audio_sync] Selected master reference: '{best_path}' "
        f"(quality score={best_score:.3f})"
    )

    return AudioReferenceChoice(
        path=best_path,
        index=best_idx,
        score=best_score,
    )


def _rank_audio_references(video_paths: List[str], audio_tracks: List[np.ndarray]) -> List[AudioReferenceChoice]:
    """Return all candidate references ranked by quality descending."""
    max_length = max((len(track) for track in audio_tracks), default=1)
    scored: List[AudioReferenceChoice] = []
    for idx, (path, audio) in enumerate(zip(video_paths, audio_tracks)):
        scored.append(AudioReferenceChoice(path=path, index=idx, score=_audio_quality_score(audio, max_length)))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def _compute_offsets_for_reference(
    video_paths: List[str],
    audio_tracks: List[np.ndarray],
    reference: AudioReferenceChoice,
    thresholds: MatchThresholds,
) -> ReferenceAttempt:
    """Compute offsets and reliability outcomes using one fixed reference track."""
    reference_audio = audio_tracks[reference.index]
    offsets: Dict[str, float] = {reference.path: 0.0}
    results: Dict[str, AlignmentResult] = {
        reference.path: AlignmentResult(0.0, 1.0, 0.0, 0.0, 1.0, 1),
    }
    weak_matches: List[tuple[str, float, float, float, float, int]] = []
    confidences: List[float] = []

    for idx, (path, audio) in enumerate(zip(video_paths, audio_tracks)):
        if idx == reference.index:
            continue

        print(f"[audio_sync] Aligning '{path}' against master reference...")
        result = _align_with_window_consensus(audio, reference_audio)
        offsets[path] = round(result.delay_seconds, 6)
        results[path] = result
        confidences.append(result.confidence)
        print(
            f"[audio_sync]   → offset: {result.delay_seconds:+.4f}s "
            f"| confidence={result.confidence:.3f} "
            f"| peak_ratio={result.peak_ratio:.2f} "
            f"| agreement_std={result.agreement_std_seconds:.3f}s "
            f"| modality_support={result.modality_support}"
        )

        partial_support = result.modality_support == thresholds.min_modality_support
        partial_support_peak_fail = (
            partial_support
            and result.peak_ratio < thresholds.min_peak_ratio_with_partial_support
        )

        if (
            result.confidence < thresholds.min_confidence
            or result.peak_ratio < thresholds.min_peak_ratio
            or result.agreement_std_seconds > thresholds.max_agreement_std_seconds
            or result.modality_support < thresholds.min_modality_support
            or partial_support_peak_fail
        ):
            weak_matches.append(
                (
                    path,
                    result.delay_seconds,
                    result.confidence,
                    result.peak_ratio,
                    result.agreement_std_seconds,
                    result.modality_support,
                )
            )

    avg_conf = float(np.mean(confidences)) if confidences else 0.0
    return ReferenceAttempt(
        reference=reference,
        offsets=offsets,
        results=results,
        weak_matches=weak_matches,
        avg_confidence=avg_conf,
    )


def align_videos_with_reference(
    video_paths: List[str],
    preferred_reference_path: Optional[str] = None,
    event_type: Optional[str] = None,
    cutting_strategy: Optional[str] = None,
    include_diagnostics: bool = False,
) -> Tuple[Dict[str, float], str] | Tuple[Dict[str, float], str, Dict[str, dict]]:
    """
    Compute audio alignment offsets and return the selected master reference path.

    The master reference is chosen automatically from the input angles based on
    a quality heuristic that favors complete, active, and stable audio tracks.
    """
    if len(video_paths) < 2:
        raise ValueError("At least 2 video paths are required for alignment.")

    print(f"[audio_sync] Extracting audio from {len(video_paths)} videos...")

    thresholds = _resolve_match_thresholds(event_type=event_type, cutting_strategy=cutting_strategy)
    print(
        "[audio_sync] Match thresholds: "
        f"confidence>={thresholds.min_confidence:.2f}, "
        f"peak_ratio>={thresholds.min_peak_ratio:.2f}, "
        f"agreement_std<={thresholds.max_agreement_std_seconds:.3f}s, "
        f"modalities>={thresholds.min_modality_support}, "
        f"peak_ratio_if_partial_support>={thresholds.min_peak_ratio_with_partial_support:.2f}"
    )

    # Extract and analyze full audio tracks for each input video.
    audio_tracks: List[np.ndarray] = [
        _extract_mono_audio(path) for path in video_paths
    ]

    for path, audio in zip(video_paths, audio_tracks):
        print(f"[audio_sync]   full-track analyzed: '{path}' ({len(audio) / TARGET_SR:.2f}s)")

    if preferred_reference_path and preferred_reference_path in video_paths:
        preferred_index = video_paths.index(preferred_reference_path)
        reference = AudioReferenceChoice(
            path=preferred_reference_path,
            index=preferred_index,
            score=1.0,
        )
        print(f"[audio_sync] Using user-selected master reference: '{reference.path}'")
        candidate_refs = [reference]
    else:
        reference = _select_audio_reference(video_paths, audio_tracks)
        ranked = _rank_audio_references(video_paths, audio_tracks)
        candidate_refs = [reference] + [r for r in ranked if r.path != reference.path]

    best_attempt: Optional[ReferenceAttempt] = None
    for attempt_idx, candidate in enumerate(candidate_refs):
        if attempt_idx > 0:
            print(
                f"[audio_sync] Retrying alignment with alternate reference: '{candidate.path}' "
                f"(quality score={candidate.score:.3f})"
            )

        attempt = _compute_offsets_for_reference(
            video_paths=video_paths,
            audio_tracks=audio_tracks,
            reference=candidate,
            thresholds=thresholds,
        )

        if best_attempt is None:
            best_attempt = attempt
        else:
            better = (
                len(attempt.weak_matches) < len(best_attempt.weak_matches)
                or (
                    len(attempt.weak_matches) == len(best_attempt.weak_matches)
                    and attempt.avg_confidence > best_attempt.avg_confidence
                )
            )
            if better:
                best_attempt = attempt

        if not attempt.weak_matches:
            best_attempt = attempt
            break

    if best_attempt is None:
        raise RuntimeError("Audio alignment failed before producing any candidate result.")

    offsets = best_attempt.offsets
    weak_matches = best_attempt.weak_matches
    reference = best_attempt.reference

    if weak_matches:
        mismatch_lines = [
            "AUDIO_MATCH_FAILED: One or more videos did not produce a reliable full-track audio match.",
            f"Reference: {reference.path}",
            "Low-confidence matches:",
        ]
        for path, delay_seconds, confidence, peak_ratio, agreement_std, modality_support in weak_matches:
            mismatch_lines.append(
                f"- {path} | offset={delay_seconds:+.4f}s | confidence={confidence:.3f} "
                f"| peak_ratio={peak_ratio:.2f} | agreement_std={agreement_std:.3f}s "
                f"| modality_support={modality_support}"
            )
        if not (preferred_reference_path and preferred_reference_path in video_paths):
            mismatch_lines.append(
                "Note: alternate master references were also evaluated; no candidate met reliability thresholds."
            )
        mismatch_lines.append(
            "Tip: choose a different Master Audio Source override, or re-upload clips with clearer shared audio."
        )
        raise ValueError("\n".join(mismatch_lines))

    if include_diagnostics:
        diagnostics = {
            path: {
                "offset_seconds": round(result.delay_seconds, 6),
                "confidence": round(result.confidence, 3),
                "peak_ratio": round(result.peak_ratio, 3),
                "agreement_std_seconds": round(result.agreement_std_seconds, 4),
                "agreement_support": round(result.agreement_support, 3),
                "modality_support": result.modality_support,
            }
            for path, result in best_attempt.results.items()
        }
        return offsets, reference.path, diagnostics

    return offsets, reference.path


def align_videos(video_paths: List[str]) -> Dict[str, float]:
    """
    Compute time offsets for each video using GCC-PHAT audio cross-correlation.

    The master reference is selected automatically using audio quality and
    completeness heuristics. This wrapper keeps the original return shape for
    backward compatibility.

    Args:
        video_paths: Ordered list of video file paths.

    Returns:
        Dict mapping each video path to its float offset in seconds.
        Example:
            {
                "cam1.mp4": 0.0,     # selected master reference
                "cam2.mp4": 1.243,   # cam2 starts 1.243s after cam1
                "cam3.mp4": -0.512   # cam3 starts 0.512s before cam1
            }

    Raises:
        ValueError: If fewer than 2 video paths are provided.
        RuntimeError: If audio extraction fails for any video.
    """
    preferred_reference = video_paths[0] if video_paths else None
    offsets, _ = align_videos_with_reference(
        video_paths,
        preferred_reference_path=preferred_reference,
    )
    return offsets
