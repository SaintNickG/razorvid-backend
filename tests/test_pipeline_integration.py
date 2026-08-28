import importlib.util
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from multicam_pipeline.multicam_cutter import build_cut_list


def _missing_prerequisites() -> list[str]:
    """Return a list of missing runtime prerequisites for this integration test."""
    missing: list[str] = []

    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg binary")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe binary")

    for module_name in ("librosa", "scipy", "numpy"):
        if importlib.util.find_spec(module_name) is None:
            missing.append(f"python module '{module_name}'")

    return missing


def _make_reference_audio(path: Path) -> None:
    """Generate a deterministic broadband audio source for correlation tests."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", "anoisesrc=color=white:sample_rate=22050:duration=8:seed=4242",
        "-c:a", "pcm_s16le",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_synthetic_video(
    path: Path,
    pattern: str,
    audio_path: Path,
    audio_delay_ms: int = 0,
) -> None:
    """
    Generate a short synthetic MP4 with a shared deterministic audio source.

    The optional audio delay lets us simulate cameras that start later than
    the master timeline so audio_sync alignment can be validated.
    """
    audio_filter = f"adelay={audio_delay_ms}|{audio_delay_ms}" if audio_delay_ms > 0 else "anull"

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"{pattern}=size=640x360:rate=30:duration=8",
        "-i", str(audio_path),
        "-filter:a", audio_filter,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", "8",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def test_build_cut_list_keeps_switching_between_available_angles() -> None:
    offsets = {"cam1": 0.0, "cam2": 5.0}
    durations = {"cam1": 40.0, "cam2": 40.0}

    segments = build_cut_list(["cam1", "cam2"], offsets, cut_interval=5.0, durations=durations)

    assert len(segments) > 0
    assert {seg.source_video_path for seg in segments} == {"cam1", "cam2"}

    runs = []
    current_run = 1
    prev = segments[0].source_video_path
    for seg in segments[1:]:
        if seg.source_video_path == prev:
            current_run += 1
        else:
            runs.append(current_run)
            current_run = 1
        prev = seg.source_video_path
    runs.append(current_run)

    assert max(runs) <= 2, "angle selection should alternate across available sources, not stall on a single angle"


def test_build_cut_list_preserves_unique_prefix_and_suffix() -> None:
    segments = build_cut_list(
        ["cam1", "cam2"],
        {"cam1": 0.0, "cam2": 5.0},
        cut_interval=5.0,
        durations={"cam1": 50.0, "cam2": 40.0},
    )

    assert segments[0].source_video_path == "cam1"
    assert segments[0].start_time == pytest.approx(0.0)
    assert segments[0].end_time == pytest.approx(5.0)
    assert segments[-1].source_video_path == "cam1"
    assert segments[-1].start_time == pytest.approx(40.0)
    assert segments[-1].end_time == pytest.approx(50.0)
    assert any(segment.source_video_path == "cam2" for segment in segments)


def test_build_cut_list_uses_all_angles_for_shared_window() -> None:
    segments = build_cut_list(
        ["cam1", "cam2", "cam3"],
        {"cam1": 0.0, "cam2": 5.0, "cam3": 10.0},
        cut_interval=5.0,
        durations={"cam1": 50.0, "cam2": 40.0, "cam3": 30.0},
    )

    assert segments[0].source_video_path == "cam1"
    assert segments[0].start_time == pytest.approx(0.0)
    assert segments[0].end_time == pytest.approx(5.0)
    assert segments[-1].source_video_path == "cam1"
    assert segments[-1].start_time == pytest.approx(40.0)
    assert segments[-1].end_time == pytest.approx(50.0)

    shared_segments = [
        segment for segment in segments
        if segment.start_time >= 10.0 and segment.end_time <= 40.0
    ]
    assert {segment.source_video_path for segment in shared_segments} == {
        "cam1", "cam2", "cam3"
    }


def test_build_cut_list_keeps_remaining_angles_active_after_one_ends() -> None:
    segments = build_cut_list(
        ["cam1", "cam2", "cam3"],
        {"cam1": 0.0, "cam2": 0.0, "cam3": 0.0},
        cut_interval=2.0,
        durations={"cam1": 50.0, "cam2": 45.0, "cam3": 40.0},
    )

    middle = [segment for segment in segments if 40.0 <= segment.start_time < 45.0]
    assert {segment.source_video_path for segment in middle} == {"cam1", "cam2"}
    assert all(segment.source_video_path == "cam1" for segment in segments if segment.start_time >= 45.0)


def test_ai_cut_list_reaches_angles_that_outlive_first_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import multicam_pipeline.ai_cutter as ai_cutter
    from multicam_pipeline.job_schema import EventType, MulticamJob, CuttingStrategy

    def fake_analyze_angle(path: str, offset: float, duration: float, strategy: CuttingStrategy,
                           event_type: EventType, sample_rate: int) -> ai_cutter.AngleSignals:
        times = np.arange(0.0, duration, 0.1)
        energy = np.ones(len(times), dtype=np.float32)
        motion = np.ones(len(times), dtype=np.float32)
        beats = np.zeros(len(times), dtype=bool)
        return ai_cutter.AngleSignals(path, offset, duration, times, energy, motion, beats)

    monkeypatch.setattr(ai_cutter, "_analyze_angle", fake_analyze_angle)

    job = MulticamJob(
        video_paths=["cam1", "cam2"],
        output_path="output.mp4",
        cutting_strategy=CuttingStrategy.LOCAL,
        event_type=EventType.SPORT,
    )
    segments = ai_cutter.build_ai_cut_list(
        ["cam1", "cam2"],
        {"cam1": 0.0, "cam2": 0.0},
        job,
        durations={"cam1": 40.0, "cam2": 45.0},
    )

    assert max(segment.end_time for segment in segments) == pytest.approx(45.0)
    assert any(
        segment.source_video_path == "cam2" and segment.end_time > 40.0
        for segment in segments
    )


def test_ai_cut_list_continues_cutting_through_long_render(monkeypatch: pytest.MonkeyPatch) -> None:
    import multicam_pipeline.ai_cutter as ai_cutter
    from multicam_pipeline.job_schema import EventType, MulticamJob, CuttingStrategy

    def fake_analyze_angle(path: str, offset: float, duration: float, strategy: CuttingStrategy,
                           event_type: EventType, sample_rate: int) -> ai_cutter.AngleSignals:
        times = np.arange(0.0, duration, 0.1)
        values = np.ones(len(times), dtype=np.float32)
        return ai_cutter.AngleSignals(
            path, offset, duration, times, values, values,
            np.zeros(len(times), dtype=bool),
        )

    monkeypatch.setattr(ai_cutter, "_analyze_angle", fake_analyze_angle)
    job = MulticamJob(
        video_paths=["cam1", "cam2", "cam3"],
        output_path="output.mp4",
        cutting_strategy=CuttingStrategy.LOCAL,
        event_type=EventType.SPORT,
    )
    segments = ai_cutter.build_ai_cut_list(
        job.video_paths,
        {"cam1": 0.0, "cam2": 0.0, "cam3": 0.0},
        job,
        durations={"cam1": 180.0, "cam2": 180.0, "cam3": 180.0},
    )

    assert len(segments) >= 20
    assert segments[-1].end_time == pytest.approx(180.0)


def test_align_videos_and_render_end_to_end(tmp_path: Path) -> None:
    missing = _missing_prerequisites()
    if missing:
        pytest.skip("missing prerequisites: " + ", ".join(missing))

    from multicam_pipeline.audio_sync import align_videos
    from multicam_pipeline.job_schema import CuttingStrategy, MulticamJob
    from multicam_pipeline.pipeline import run_multicam_job

    cam1 = tmp_path / "cam1.mp4"
    cam2 = tmp_path / "cam2.mp4"
    reference_audio = tmp_path / "reference.wav"
    output = tmp_path / "multicam_output.mp4"

    _make_reference_audio(reference_audio)
    _make_synthetic_video(cam1, pattern="testsrc", audio_path=reference_audio, audio_delay_ms=0)
    _make_synthetic_video(cam2, pattern="testsrc2", audio_path=reference_audio, audio_delay_ms=1200)

    offsets = align_videos([str(cam1), str(cam2)])

    # cam2 audio starts later than cam1 by roughly 1.2s.
    assert abs(offsets[str(cam2)] - 1.2) < 0.2

    job = MulticamJob(
        video_paths=[str(cam1), str(cam2)],
        output_path=str(output),
        cut_interval=2.0,
        target_width=640,
        target_height=360,
        target_fps=30,
        cutting_strategy=CuttingStrategy.INTERVAL,
    )

    rendered_path = run_multicam_job(job)

    assert rendered_path == str(output)
    assert output.exists()
    assert output.stat().st_size > 10000
