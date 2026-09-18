import importlib.util
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from multicam_pipeline.multicam_cutter import build_cut_list, CutSegment


def test_ingestion_picks_largest_video_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract_metadata must select the highest-resolution video stream, not the first."""
    from multicam_pipeline.ingestion import extract_metadata

    # Simulate a .mov with a thumbnail track (stream 0, 224x128) before the
    # real video track (stream 1, 1920x1080) — exactly the iPhone companion file layout.
    fake_probe = {
        "streams": [
            {"codec_type": "video", "width": 224, "height": 128, "r_frame_rate": "15/1"},
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30/1"},
            {"codec_type": "audio", "channels": 2},
        ],
        "format": {"duration": "10.0"},
    }
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: fake_probe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    meta = extract_metadata("fake.mov")
    assert meta.width == 1920
    assert meta.height == 1080
    assert meta.fps == 30.0


def test_ingestion_applies_phone_rotation_to_display_orientation(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline.ingestion import extract_metadata

    fake_probe = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "side_data_list": [{"rotation": -90}],
            },
            {"codec_type": "audio", "channels": 2},
        ],
        "format": {"duration": "10.0"},
    }
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: fake_probe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    meta = extract_metadata("phone.mov")
    assert (meta.width, meta.height) == (1080, 1920)


def test_ingestion_rejects_thumbnail_track_only_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_and_ingest must reject a file whose only video stream is a thumbnail track."""
    from multicam_pipeline.ingestion import validate_and_ingest

    fake_probe = {
        "streams": [
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "video", "width": 224, "height": 128, "r_frame_rate": "15/1"},
        ],
        "format": {"duration": "190.0"},
    }
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: fake_probe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    with pytest.raises(ValueError, match="resolution.*too low"):
        validate_and_ingest(["IMG_7506.mov"])


def test_ingestion_excludes_low_res_companion_when_enough_angles_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A low-resolution companion file should be dropped, not fail the render, when 2+ good angles remain."""
    from multicam_pipeline.ingestion import validate_and_ingest

    good_probe = {
        "streams": [
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30/1"},
        ],
        "format": {"duration": "60.0"},
    }
    companion_probe = {
        "streams": [
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "video", "width": 480, "height": 360, "r_frame_rate": "30/1"},
        ],
        "format": {"duration": "60.0"},
    }

    def fake_ffprobe(path: str) -> dict:
        return companion_probe if "companion" in path else good_probe

    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", fake_ffprobe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    results = validate_and_ingest(["cam1.mov", "cam2.mov", "companion.mov"])

    assert {m.path for m in results} == {"cam1.mov", "cam2.mov"}


def test_ffmpeg_command_caps_threads_for_long_renders() -> None:
    from multicam_pipeline.rendering import build_ffmpeg_command

    segments = [
        CutSegment(float(i) * 2.0, float(i) * 2.0 + 2.0, f"cam{i % 3}.mp4", 0.0)
        for i in range(60)
    ]
    command = build_ffmpeg_command(
        segments,
        "output.mp4",
        "libx264",
        audio_source_path="cam0.mp4",
    )

    # Every input must cap its decoder threads, otherwise long renders exhaust
    # the Lambda thread limit and FFmpeg exits with EAGAIN (245).
    input_indexes = [index for index, arg in enumerate(command) if arg == "-i"]
    for index in input_indexes:
        window = command[max(0, index - 7):index]
        assert "-threads" in window
        assert int(window[window.index("-threads") + 1]) <= 2
    assert "-filter_complex_threads" in command


def test_ffmpeg_command_opens_each_source_once() -> None:
    """Input count must equal unique source files, not segment count."""
    from multicam_pipeline.rendering import build_ffmpeg_command

    # 60 segments across 3 source files — previously opened 60 inputs.
    segments = [
        CutSegment(float(i) * 2.0, float(i) * 2.0 + 2.0, f"cam{i % 3}.mp4", 0.0)
        for i in range(60)
    ]
    command = build_ffmpeg_command(
        segments,
        "output.mp4",
        "libx264",
        audio_source_path="cam0.mp4",
    )

    # cam0.mp4 is both a source and the audio reference — must not be opened twice.
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert inputs == ["cam0.mp4", "cam1.mp4", "cam2.mp4"], (
        f"Expected 3 unique inputs, got {len(inputs)}: {inputs}"
    )

    # trim filters must appear in the filtergraph for each segment.
    filter_complex = command[command.index("-filter_complex") + 1]
    # Video trim uses "trim=start=" (not prefixed by 'a').
    # atrim=start= (audio) is a substring of trim=start=, so count exclusively.
    video_trims = filter_complex.count("trim=start=") - filter_complex.count("atrim=start=")
    assert video_trims == 60, f"Expected 60 video trims, got {video_trims}"
    assert filter_complex.count("atrim=start=") == 60  # audio trims
    assert filter_complex.count("setpts=PTS-STARTPTS") >= 60


def test_render_normalizes_loudness_once_per_source_not_per_segment() -> None:
    """
    Loudness normalization must run once per unique audio source, not once per
    cut segment — normalizing every short trimmed clip independently would
    pump/duck audibly between cuts instead of leveling mic gain differences
    across cameras.
    """
    from multicam_pipeline.rendering import build_ffmpeg_command

    segments = [
        CutSegment(float(i) * 2.0, float(i) * 2.0 + 2.0, f"cam{i % 3}.mp4", 0.0)
        for i in range(30)
    ]
    command = build_ffmpeg_command(
        segments,
        "output.mp4",
        "libx264",
        audio_source_path="cam0.mp4",
    )
    filter_complex = command[command.index("-filter_complex") + 1]

    # All 30 segments pull from the single shared audio reference, so loudnorm
    # must appear exactly once — not once per segment.
    assert filter_complex.count("loudnorm=") == 1


def test_audio_reference_quality_score_ignores_render_time_normalization() -> None:
    """
    The master-audio-reference picker must judge raw signal quality (duration,
    activity, dynamic range, clipping) directly from decoded samples — it must
    never depend on or be affected by the render-time loudnorm filter, which
    only runs later inside the FFmpeg output stage.
    """
    from multicam_pipeline.audio_sync import _audio_quality_score

    rng = np.random.default_rng(0)
    quiet_clip = (rng.standard_normal(22050 * 5).astype(np.float32)) * 0.01
    strong_clip = np.clip(rng.standard_normal(22050 * 5).astype(np.float32) * 0.3, -0.95, 0.95)

    quiet_score = _audio_quality_score(quiet_clip, max_length=len(strong_clip))
    strong_score = _audio_quality_score(strong_clip, max_length=len(strong_clip))

    # A clip with real signal activity/dynamic range must clearly outrank a
    # near-silent one — proving the picker still sees each source's true,
    # un-normalized loudness/activity profile.
    assert strong_score > quiet_score


def test_orientation_crop_depth_only_zooms_opposing_angles() -> None:
    from multicam_pipeline.rendering import build_ffmpeg_command

    segments = [
        CutSegment(0.0, 2.0, "landscape.mp4", 0.0),
        CutSegment(2.0, 4.0, "portrait.mp4", 0.0),
    ]
    command = build_ffmpeg_command(
        segments,
        "output.mp4",
        "libx264",
        target_width=1920,
        target_height=1080,
        orientation_mode="landscape",
        orientation_crop_depth=100,
        source_dimensions={
            "landscape.mp4": (1920, 1080),
            "portrait.mp4": (1080, 1920),
        },
    )
    filters = command[command.index("-filter_complex") + 1]
    assert "scale=1920:1080" in filters
    assert "scale=1920:3412" in filters
    assert "crop=w='min(iw,1920)':h='min(ih,1080)'" in filters


def test_native_orientation_preserves_full_opposing_frame() -> None:
    from multicam_pipeline.rendering import build_ffmpeg_command

    segments = [CutSegment(0.0, 2.0, "portrait.mp4", 0.0)]
    command = build_ffmpeg_command(
        segments,
        "output.mp4",
        "libx264",
        target_width=1920,
        target_height=1080,
        orientation_mode="native",
        orientation_crop_depth=100,
        source_dimensions={"portrait.mp4": (1080, 1920)},
    )
    filters = command[command.index("-filter_complex") + 1]
    assert "scale=606:1080" in filters
    assert "pad=1920:1080" in filters


def test_auto_orientation_requires_creator_portrait_and_half_portrait() -> None:
    from multicam_pipeline.ingestion import VideoMetadata
    from multicam_pipeline.job_schema import MulticamJob
    from multicam_pipeline.pipeline import _resolve_output_orientation

    metadata = [
        VideoMetadata("owner.mov", 10, 30, 1080, 1920, 2),
        VideoMetadata("guest-a.mov", 10, 30, 1080, 1920, 2),
        VideoMetadata("guest-b.mov", 10, 30, 1920, 1080, 2),
        VideoMetadata("guest-c.mov", 10, 30, 1920, 1080, 2),
    ]
    job = MulticamJob([item.path for item in metadata], "output.mp4", creator_video_paths=["owner.mov"])
    assert _resolve_output_orientation(metadata, job) == "portrait"

    job.creator_video_paths = ["guest-b.mov"]
    assert _resolve_output_orientation(metadata, job) == "landscape"


def test_metadata_sync_window_requires_complete_plausible_overlap() -> None:
    from multicam_pipeline.audio_sync import _metadata_window_plan

    paths = ["cam-a.mov", "cam-b.mov"]
    plan = _metadata_window_plan(
        paths,
        {"cam-a.mov": 1_700_000_000.0, "cam-b.mov": 1_700_000_010.0},
        {"cam-a.mov": 120.0, "cam-b.mov": 120.0},
    )
    assert plan == ({"cam-a.mov": 10.0, "cam-b.mov": 0.0}, 45.0)
    assert _metadata_window_plan(paths, {"cam-a.mov": 1_700_000_000.0}, {path: 120.0 for path in paths}) is None
    assert _metadata_window_plan(
        paths,
        {"cam-a.mov": 1_700_000_000.0, "cam-b.mov": 1_700_020_000.0},
        {path: 120.0 for path in paths},
    ) is None


def test_metadata_window_reconstructs_full_offset_without_changing_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    import multicam_pipeline.audio_sync as audio_sync

    monkeypatch.setattr(
        audio_sync,
        "_align_with_window_consensus",
        lambda _sig, _ref: audio_sync.AlignmentResult(0.25, 0.9, 3.0, 0.01, 1.0, 4),
    )
    attempt = audio_sync._compute_offsets_for_reference(
        ["reference.mov", "camera.mov"],
        [np.ones(1000), np.ones(1000)],
        audio_sync.AudioReferenceChoice("reference.mov", 0, 1.0),
        audio_sync._resolve_match_thresholds("concert", "local"),
        audio_start_offsets={"reference.mov": 0.0, "camera.mov": 10.0},
    )
    assert attempt.offsets["camera.mov"] == pytest.approx(-10.25)


def test_weak_metadata_window_falls_back_to_full_track(monkeypatch: pytest.MonkeyPatch) -> None:
    import multicam_pipeline.audio_sync as audio_sync

    paths = ["reference.mov", "camera.mov"]
    extraction_calls = []
    monkeypatch.setattr(audio_sync, "_metadata_window_plan", lambda *_: ({path: 0.0 for path in paths}, 45.0))

    def fake_extract(path, sr=audio_sync.TARGET_SR, start_seconds=0.0, duration_seconds=None):
        extraction_calls.append((path, duration_seconds))
        return np.ones(1000)

    monkeypatch.setattr(audio_sync, "_extract_mono_audio", fake_extract)
    reference = audio_sync.AudioReferenceChoice(paths[0], 0, 1.0)
    weak = audio_sync.ReferenceAttempt(reference, {paths[0]: 0.0, paths[1]: 0.0}, {}, [(paths[1], 0, 0, 0, 0, 0)], 0.0)
    strong_result = audio_sync.AlignmentResult(-1.2, 0.9, 3.0, 0.01, 1.0, 4)
    strong = audio_sync.ReferenceAttempt(reference, {paths[0]: 0.0, paths[1]: -1.2}, {paths[0]: strong_result, paths[1]: strong_result}, [], 0.9)
    attempts = iter([weak, strong])
    monkeypatch.setattr(audio_sync, "_evaluate_reference_attempts", lambda *_args, **_kwargs: next(attempts))

    offsets, selected = audio_sync.align_videos_with_reference(
        paths,
        metadata_creation_times={path: 1_700_000_000.0 for path in paths},
        video_durations={path: 120.0 for path in paths},
    )
    assert offsets[paths[1]] == pytest.approx(-1.2)
    assert selected == paths[0]
    assert extraction_calls == [
        (paths[0], 45.0), (paths[1], 45.0),
        (paths[0], None), (paths[1], None),
    ]


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
        "-i", f"{pattern}=size=640x480:rate=30:duration=8",
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

    # cam2's shared audio sits 1.2s into its own file, so cam2 began recording
    # 1.2s earlier and starts earlier on the master timeline.
    assert abs(offsets[str(cam2)] + 1.2) < 0.2

    job = MulticamJob(
        video_paths=[str(cam1), str(cam2)],
        output_path=str(output),
        cut_interval=2.0,
        target_width=640,
        target_height=480,
        target_fps=30,
        cutting_strategy=CuttingStrategy.INTERVAL,
    )

    rendered_path = run_multicam_job(job)

    assert rendered_path == str(output)
    assert output.exists()
    assert output.stat().st_size > 10000

    # cam2 covers master [0, 8] and cam1 covers [1.2, 9.2], so the rendered
    # timeline must span both unique ends rather than a single camera.
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(output),
        ],
        check=True, capture_output=True, text=True,
    )
    assert abs(float(probe.stdout.strip()) - 9.2) < 0.5
