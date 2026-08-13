import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


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
