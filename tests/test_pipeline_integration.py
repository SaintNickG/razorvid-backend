import importlib.util
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from multicam_pipeline.multicam_cutter import build_cut_list, CutSegment


def test_audio_mixer_centers_primary_and_half_pans_quieter_support() -> None:
    from multicam_pipeline.rendering import AudioMixSettings, audio_mix_levels, build_audio_mix_plan

    plan = build_audio_mix_plan(
        0.0, 8.0, "master", {"master": 0.0, "left": 0.0, "right": 0.0},
        {"master": 8.0, "left": 8.0, "right": 8.0}, {},
        AudioMixSettings(supporting_pan=1.0),
    )
    levels = audio_mix_levels(plan, 4.0)
    assert levels["master"] == (1.0, 0.0)
    assert levels["left"][1] == -0.5
    assert levels["right"][1] == 0.5
    assert 0 < levels["left"][0] < levels["master"][0]
    assert levels["left"][0] == levels["right"][0]


def test_audio_mixer_bounds_handoffs_by_real_overlap_and_keeps_gaps() -> None:
    from multicam_pipeline.rendering import audio_mix_levels, build_audio_mix_plan

    plan = build_audio_mix_plan(
        0.0, 8.0, "master", {"master": 2.0, "fallback": 0.0},
        {"master": 2.0, "fallback": 8.0}, {},
    )
    assert [(fade.start_time, fade.end_time) for fade in plan.transitions] == [(2.0, 2.75), (3.25, 4.0)]
    assert audio_mix_levels(plan, 1.0)["fallback"] == (1.0, 0.0)
    assert audio_mix_levels(plan, 3.0)["master"] == (1.0, 0.0)
    assert audio_mix_levels(plan, 5.0)["fallback"] == (1.0, 0.0)
    at_start = audio_mix_levels(plan, 2.375)
    assert at_start["master"][0] == pytest.approx(at_start["fallback"][0])
    assert at_start["master"][1] == 0.0
    assert abs(at_start["fallback"][1]) < 0.5
    gap_plan = build_audio_mix_plan(0.0, 5.0, "master", {"master": 0.0, "late": 3.0}, {"master": 2.0, "late": 2.0}, {})
    assert not gap_plan.transitions
    assert audio_mix_levels(gap_plan, 2.5) == {}


def test_audio_mixer_filtergraph_is_opt_in_and_reuses_inputs() -> None:
    from multicam_pipeline.rendering import build_audio_mix_plan, build_ffmpeg_command

    paths = ["master.mp4", "left.mp4", "right.mp4"]
    plan = build_audio_mix_plan(0.0, 8.0, paths[0], dict.fromkeys(paths, 0.0), dict.fromkeys(paths, 8.0), {})
    segments = [CutSegment(0.0, 8.0, paths[0], 0.0)]
    command = build_ffmpeg_command(segments, "output.mp4", "libx264", audio_mix_plan=plan)
    inputs = [command[index + 1] for index, value in enumerate(command) if value == "-i"]
    graph = command[command.index("-filter_complex") + 1]
    assert inputs == paths
    assert graph.count("loudnorm=") == 3
    assert "amix=inputs=3" in graph
    assert "latency=true" in graph
    assert "acrossfade" not in graph
    default = build_ffmpeg_command(segments, "default.mp4", "libx264", audio_source_path=paths[0])
    assert "amix" not in default[default.index("-filter_complex") + 1]


def test_audio_mixer_rejects_invalid_settings() -> None:
    from multicam_pipeline.rendering import AudioMixSettings

    for settings in ({"transition_seconds": -1}, {"supporting_gain": 1}, {"supporting_pan": float("nan")}):
        with pytest.raises(ValueError):
            AudioMixSettings(**settings)


def test_audio_fallback_selects_best_source_with_video_coverage() -> None:
    from multicam_pipeline.rendering import build_audio_spans

    spans = build_audio_spans(
        0.0, 12.0, "master",
        {"master": 2.0, "expired": 0.0, "best": 0.0, "backup": 0.0},
        {"master": 4.0, "expired": 6.0, "best": 10.0, "backup": 12.0},
        {"master": 0.5, "expired": 1.0, "best": 0.9, "backup": 0.6},
    )

    assert [(span.start_time, span.end_time, span.source_path) for span in spans] == [
        (0.0, 2.0, "expired"),
        (2.0, 6.0, "master"),
        (6.0, 10.0, "best"),
        (10.0, 12.0, "backup"),
    ]


def test_audio_fallback_preserves_absolute_part_timing_and_real_gaps() -> None:
    from multicam_pipeline.rendering import build_audio_spans

    spans = build_audio_spans(
        120.0, 130.0, "master",
        {"master": 0.0, "late": 125.0},
        {"master": 122.0, "late": 3.0},
        {"late": 0.8},
    )

    assert [(span.start_time, span.end_time, span.source_path, span.offset) for span in spans] == [
        (120.0, 122.0, "master", 0.0),
        (122.0, 125.0, None, 0.0),
        (125.0, 128.0, "late", 125.0),
        (128.0, 130.0, None, 0.0),
    ]


def test_audio_fallback_opens_non_visible_source_once_and_uses_aligned_trims() -> None:
    from multicam_pipeline.rendering import build_audio_spans, build_ffmpeg_command

    spans = build_audio_spans(
        0.0, 12.0, "master.mp4",
        {"master.mp4": 2.0, "fallback.mp4": 0.0},
        {"master.mp4": 4.0, "fallback.mp4": 12.0},
        {"fallback.mp4": 0.8},
    )
    command = build_ffmpeg_command(
        [CutSegment(0.0, 12.0, "visible.mp4", 0.0)], "output.mp4", "libx264",
        audio_source_path="master.mp4", audio_spans=spans,
    )
    inputs = [command[index + 1] for index, value in enumerate(command) if value == "-i"]
    graph = command[command.index("-filter_complex") + 1]
    assert inputs == ["visible.mp4", "fallback.mp4", "master.mp4"]
    assert "asplit=2" in graph
    assert "atrim=start=0.000000:end=4.000000" in graph
    assert "atrim=start=6.000000:end=12.000000" in graph
    assert graph.count("loudnorm=") == 2


def test_ingestion_preserves_audio_and_video_coverage_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline.ingestion import extract_metadata
    from multicam_pipeline.pipeline import _resolve_durations

    monkeypatch.setattr("os.path.isfile", lambda _: True)
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30/1", "duration": "6.0"},
            {"codec_type": "audio", "channels": 2, "duration": "10.0"},
        ],
        "format": {"duration": "10.0"},
    })

    metadata = extract_metadata("short-video.mp4")
    assert metadata.audio_duration == 10.0
    assert metadata.video_duration == 6.0
    assert _resolve_durations([metadata]) == {"short-video.mp4": 6.0}


def test_pipeline_fallback_requires_audio_and_video_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import pipeline
    from multicam_pipeline.ingestion import VideoMetadata
    from multicam_pipeline.job_schema import MulticamJob

    paths = ["master", "short-video", "short-audio", "complete"]
    metadata = [
        VideoMetadata(paths[0], 2.0, 30.0, 1920, 1080, 2, video_duration=2.0, audio_duration=2.0),
        VideoMetadata(paths[1], 10.0, 30.0, 1920, 1080, 2, video_duration=5.0, audio_duration=10.0),
        VideoMetadata(paths[2], 10.0, 30.0, 1920, 1080, 2, video_duration=10.0, audio_duration=7.0),
        VideoMetadata(paths[3], 10.0, 30.0, 1920, 1080, 2, video_duration=10.0, audio_duration=10.0),
    ]
    offsets = dict(zip(paths, [2.0, 0.0, 0.0, 0.0]))
    diagnostics = {path: {"audio_quality_score": score} for path, score in zip(paths, [0.7, 0.99, 0.8, 0.5])}
    rendered = []
    monkeypatch.setattr(pipeline, "validate_and_ingest", lambda _, **_kwargs: metadata)
    monkeypatch.setattr(pipeline, "align_videos_with_reference", lambda *_args, **_kwargs: (offsets, paths[0], diagnostics))
    monkeypatch.setattr(pipeline, "_build_segments", lambda **_kwargs: [CutSegment(0.0, 10.0, paths[3], 0.0)])
    monkeypatch.setattr(pipeline, "render", lambda **kwargs: rendered.append(kwargs) or kwargs["output_path"])
    job = MulticamJob(video_paths=paths, output_path=str(tmp_path / "output.mp4"))

    pipeline.run_multicam_job(job)

    assert [(span.start_time, span.end_time, span.source_path) for span in rendered[0]["audio_spans"]] == [
        (0.0, 2.0, "short-video"),
        (2.0, 4.0, "master"),
        (4.0, 5.0, "short-video"),
        (5.0, 7.0, "short-audio"),
        (7.0, 10.0, "complete"),
    ]


def test_pipeline_uses_remaining_angles_when_selected_audio_source_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multicam_pipeline import pipeline
    from multicam_pipeline.ingestion import VideoMetadata
    from multicam_pipeline.job_schema import MulticamJob

    paths = ["bad-master.mp4", "cam1.mp4", "cam2.mp4"]
    good_metadata = [
        VideoMetadata(path, 10.0, 30.0, 1920, 1080, 2)
        for path in paths[1:]
    ]
    rendered = []

    def fake_ingest(_paths, failed_angles):
        failed_angles.append({
            "file": "bad-master.mp4",
            "reason": "No audio track; audio is required to synchronize this angle.",
            "category": "validation",
        })
        return good_metadata

    def fake_align(video_paths, preferred_reference_path=None, **_kwargs):
        assert video_paths == paths[1:]
        assert preferred_reference_path is None
        return ({path: 0.0 for path in video_paths}, video_paths[0], {})

    monkeypatch.setattr(pipeline, "validate_and_ingest", fake_ingest)
    monkeypatch.setattr(pipeline, "align_videos_with_reference", fake_align)
    monkeypatch.setattr(
        pipeline,
        "_build_segments",
        lambda **kwargs: [CutSegment(0.0, 10.0, kwargs["video_paths"][0], 0.0)],
    )
    monkeypatch.setattr(pipeline, "render", lambda **kwargs: rendered.append(kwargs) or kwargs["output_path"])
    job = MulticamJob(
        video_paths=paths,
        output_path=str(tmp_path / "output.mp4"),
        audio_source_override=paths[0],
    )

    pipeline.run_multicam_job(job)

    assert rendered[0]["segments"][0].source_video_path == "cam1.mp4"
    assert job.angle_failures[0]["file"] == "bad-master.mp4"


def test_split_cut_segments_preserves_offsets_and_duration_caps() -> None:
    from multicam_pipeline.pipeline import split_cut_segments

    segments = [
        CutSegment(0.0, 90.0, "cam-a.mp4", 0.0),
        CutSegment(90.0, 180.0, "cam-b.mp4", 10.0),
        CutSegment(180.0, 360.0, "cam-a.mp4", 0.0),
    ]

    parts = split_cut_segments(segments, 120.0)

    assert len(parts) == 3
    assert [(part[0].start_time, part[-1].end_time) for part in parts] == [
        (0.0, 120.0),
        (120.0, 240.0),
        (240.0, 360.0),
    ]
    assert all(sum(segment.duration for segment in part) <= 120.0 for part in parts)
    assert parts[1][0].source_start == pytest.approx(110.0)
    assert parts[1][0].transition_theme == "hard_cut"


def test_pipeline_auto_split_records_all_rendered_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import pipeline
    from multicam_pipeline.ingestion import VideoMetadata
    from multicam_pipeline.job_schema import MulticamJob

    paths = ["cam-a.mp4", "cam-b.mp4"]
    metadata = [
        VideoMetadata(path=path, duration=250.0, fps=30.0, width=1920, height=1080, audio_channels=2)
        for path in paths
    ]
    segments = [
        CutSegment(0.0, 120.0, paths[0], 0.0),
        CutSegment(120.0, 240.0, paths[1], 0.0),
        CutSegment(240.0, 250.0, paths[0], 0.0),
    ]
    rendered = []

    monkeypatch.setattr(pipeline, "validate_and_ingest", lambda _, **_kwargs: metadata)
    monkeypatch.setattr(pipeline, "align_videos_with_reference", lambda *_args, **_kwargs: ({paths[0]: 0.0, paths[1]: 0.0}, paths[0], {}))
    monkeypatch.setattr(pipeline, "_build_segments", lambda **_kwargs: segments)
    monkeypatch.setattr(pipeline, "render", lambda **kwargs: rendered.append(kwargs) or kwargs["output_path"])

    job = MulticamJob(
        video_paths=paths,
        output_path=str(tmp_path / "output.mp4"),
        max_duration=120.0,
        auto_split=True,
    )

    output_path = pipeline.run_multicam_job(job)

    expected_parts = [
        str(tmp_path / "output_part_01_of_03.mp4"),
        str(tmp_path / "output_part_02_of_03.mp4"),
        str(tmp_path / "output_part_03_of_03.mp4"),
    ]
    assert output_path == expected_parts[0]
    assert job.output_parts == expected_parts
    assert job.total_parts == 3
    assert len(rendered) == 3
    assert all(sum(segment.duration for segment in call["segments"]) <= 120.0 for call in rendered)


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


def test_ingestion_reads_quicktime_creation_date_for_metadata_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline.ingestion import extract_metadata

    creation_value = "2026-10-03T14:30:00-07:00"
    fake_probe = {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30/1"},
            {"codec_type": "audio", "channels": 2},
        ],
        "format": {
            "duration": "120.0",
            "tags": {"com.apple.quicktime.creationdate": creation_value},
        },
    }
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: fake_probe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    metadata = extract_metadata("iphone.mov")

    assert metadata.creation_time == pytest.approx(
        datetime.fromisoformat(creation_value).timestamp()
    )


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


def test_ingestion_skips_invalid_angle_and_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline.ingestion import validate_and_ingest

    def fake_ffprobe(path: str) -> dict:
        streams = [{"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30/1"}]
        if path != "broken.mp4":
            streams.append({"codec_type": "audio", "channels": 2})
        return {"streams": streams, "format": {"duration": "60.0"}}

    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", fake_ffprobe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)
    failures = []

    results = validate_and_ingest(
        ["cam1.mp4", "broken.mp4", "cam2.mp4"],
        failed_angles=failures,
    )

    assert {item.path for item in results} == {"cam1.mp4", "cam2.mp4"}
    assert failures == [{
        "file": "broken.mp4",
        "reason": "No audio track; audio is required to synchronize this angle.",
        "category": "validation",
    }]


def test_ingestion_reports_all_low_res_files_when_render_cannot_proceed(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When every uploaded file is too low resolution (e.g. all compressed by
    iMessage/SMS on the way to the app), the error must name all affected
    files and explain the likely cause — not just repeat one arbitrary file's
    reason as if it were the only problem.
    """
    from multicam_pipeline.ingestion import validate_and_ingest

    low_res_probe = {
        "streams": [
            {"codec_type": "audio", "channels": 1},
            {"codec_type": "video", "width": 480, "height": 360, "r_frame_rate": "24/1"},
        ],
        "format": {"duration": "150.0"},
    }
    monkeypatch.setattr("multicam_pipeline.ingestion._run_ffprobe", lambda _: low_res_probe)
    monkeypatch.setattr("os.path.isfile", lambda _: True)

    with pytest.raises(ValueError) as excinfo:
        validate_and_ingest(["a.mov", "b.mov", "c.mov"])

    message = str(excinfo.value)
    assert "b.mov" in message and "c.mov" in message
    assert "3 of 3" in message
    assert "iMessage" in message


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
    assert filter_complex.count("atrim=start=") == 1
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


def test_align_videos_with_reference_accepts_external_audio_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import audio_sync

    board_audio = np.zeros(22050 * 4, dtype=np.float32)
    board_audio[500:8000] = np.sin(np.linspace(0, 20 * np.pi, 7500, dtype=np.float32))
    cam1 = np.zeros_like(board_audio)
    cam1[1500:8200] = board_audio[500:7200]
    cam2 = np.zeros_like(board_audio)
    cam2[3000:9200] = board_audio[2500:8700]

    def fake_extract(path: str, *args, **kwargs) -> np.ndarray:
        if path.endswith("board.wav"):
            return board_audio
        if "cam1" in path:
            return cam1
        return cam2

    monkeypatch.setattr(audio_sync, "_extract_mono_audio", fake_extract)
    monkeypatch.setattr(
        audio_sync,
        "_align_with_window_consensus",
        lambda sig, ref: audio_sync.AlignmentResult(
            delay_seconds=0.0,
            confidence=0.95,
            peak_ratio=4.0,
            agreement_std_seconds=0.01,
            agreement_support=1.0,
            modality_support=2,
        ),
    )

    offsets, reference = audio_sync.align_videos_with_reference(
        ["cam1.mp4", "cam2.mp4"],
        preferred_reference_path="/tmp/board.wav",
    )

    assert reference == "/tmp/board.wav"
    assert set(offsets) == {"cam1.mp4", "cam2.mp4"}


def test_alignment_drops_weak_angle_when_two_reliable_angles_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import audio_sync

    paths = ["cam1.mp4", "cam2.mp4", "bad-audio.mp4"]
    monkeypatch.setattr(audio_sync, "_extract_mono_audio", lambda *_args, **_kwargs: np.ones(1000, dtype=np.float32))

    def reliable_result() -> audio_sync.AlignmentResult:
        return audio_sync.AlignmentResult(0.0, 0.95, 4.0, 0.01, 1.0, 4)

    def fake_evaluate(video_paths, *_args, **_kwargs):
        reference = audio_sync.AudioReferenceChoice(video_paths[0], 0, 1.0)
        if "bad-audio.mp4" in video_paths:
            return audio_sync.ReferenceAttempt(
                reference,
                {path: 0.0 for path in video_paths},
                {path: reliable_result() for path in video_paths},
                [("bad-audio.mp4", 0.0, 0.1, 1.0, 0.4, 1)],
                0.6,
            )
        return audio_sync.ReferenceAttempt(
            reference,
            {path: 0.0 for path in video_paths},
            {path: reliable_result() for path in video_paths},
            [],
            0.95,
        )

    monkeypatch.setattr(audio_sync, "_evaluate_reference_attempts", fake_evaluate)
    failures = []

    offsets, reference = audio_sync.align_videos_with_reference(
        paths,
        cutting_strategy="local",
        force_full_track_sync=True,
        failed_angles=failures,
    )

    assert set(offsets) == {"cam1.mp4", "cam2.mp4"}
    assert reference == "cam1.mp4"
    assert failures == [{
        "file": "bad-audio.mp4",
        "reason": "Audio sync was unreliable: confidence 0.100 below 0.430; peak ratio 1.00 below 1.50; agreement spread 0.400s above 0.160s; only 1 matching signal feature(s), 2 required.",
        "category": "sync",
    }]


def test_alignment_reports_weak_angle_when_fewer_than_two_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import audio_sync

    paths = ["cam1.mp4", "bad-audio.mp4"]
    monkeypatch.setattr(audio_sync, "_extract_mono_audio", lambda *_args, **_kwargs: np.ones(1000, dtype=np.float32))
    result = audio_sync.AlignmentResult(0.0, 0.1, 1.0, 0.4, 0.0, 1)

    def fake_evaluate(video_paths, *_args, **_kwargs):
        reference = audio_sync.AudioReferenceChoice(video_paths[0], 0, 1.0)
        return audio_sync.ReferenceAttempt(
            reference,
            {path: 0.0 for path in video_paths},
            {path: result for path in video_paths},
            [("bad-audio.mp4", 0.0, 0.1, 1.0, 0.4, 1)],
            0.1,
        )

    monkeypatch.setattr(audio_sync, "_evaluate_reference_attempts", fake_evaluate)
    failures = []

    with pytest.raises(ValueError, match="AUDIO_MATCH_FAILED"):
        audio_sync.align_videos_with_reference(
            paths,
            cutting_strategy="local",
            force_full_track_sync=True,
            failed_angles=failures,
        )

    assert failures and failures[0]["file"] == "bad-audio.mp4"
    assert "confidence 0.100 below 0.430" in failures[0]["reason"]


def test_job_status_endpoint_returns_angle_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from multicam_pipeline.routers import jobs

    failure = {
        "file": "bad-audio.mp4",
        "reason": "Audio sync was unreliable.",
        "category": "sync",
    }
    record = {
        "job_id": "job-1",
        "status": "COMPLETE",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:01:00+00:00",
        "angle_failures": [failure],
    }
    monkeypatch.setattr(jobs, "IS_LOCAL", True)
    monkeypatch.setattr(jobs, "_get_status_local", lambda _job_id: record)

    response = asyncio.run(jobs.get_status("job-1", {}))

    assert response.angle_failures == [failure]


def test_upper_subject_framing_score_prefers_upper_half(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import ai_cutter

    upper_frame = np.zeros((80, 120, 3), dtype=np.uint8)
    upper_frame[10:30, 40:80] = 200
    upper_frame[30:55, 45:75] = 180

    lower_frame = np.zeros((80, 120, 3), dtype=np.uint8)
    lower_frame[45:70, 40:80] = 200
    lower_frame[20:45, 45:75] = 180

    upper_score = ai_cutter._framing_quality_for_frame(upper_frame)
    lower_score = ai_cutter._framing_quality_for_frame(lower_frame)

    assert upper_score > lower_score


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


def test_partial_timestamp_metadata_uses_full_track_only_for_missing_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    import multicam_pipeline.audio_sync as audio_sync

    paths = ["early.mov", "anchor.mov", "missing-time.mov"]
    extraction_calls = []

    def fake_extract(path, sr=audio_sync.TARGET_SR, start_seconds=0.0, duration_seconds=None):
        extraction_calls.append((path, start_seconds, duration_seconds))
        return np.ones(1000, dtype=np.float32)

    def fake_metadata_attempt(paths, tracks, thresholds, preferred_reference_path, audio_start_offsets=None):
        assert paths == ["early.mov", "anchor.mov"]
        assert audio_start_offsets == {"early.mov": 10.0, "anchor.mov": 0.0}
        reference = audio_sync.AudioReferenceChoice("anchor.mov", 1, 0.95)
        reference_result = audio_sync.AlignmentResult(0.0, 1.0, 4.0, 0.0, 1.0, 4)
        early_result = audio_sync.AlignmentResult(-10.0, 0.9, 3.0, 0.01, 1.0, 4)
        return audio_sync.ReferenceAttempt(
            reference,
            {"early.mov": -10.0, "anchor.mov": 0.0},
            {"early.mov": early_result, "anchor.mov": reference_result},
            [],
            0.9,
        )

    monkeypatch.setattr(audio_sync, "_extract_mono_audio", fake_extract)
    monkeypatch.setattr(audio_sync, "_evaluate_reference_attempts", fake_metadata_attempt)
    monkeypatch.setattr(
        audio_sync,
        "_align_with_window_consensus",
        lambda _sig, _ref: audio_sync.AlignmentResult(2.0, 0.9, 4.0, 0.01, 1.0, 4),
    )

    offsets, selected, diagnostics = audio_sync.align_videos_with_reference(
        paths,
        include_diagnostics=True,
        metadata_creation_times={"early.mov": 1_700_000_000.0, "anchor.mov": 1_700_000_010.0},
        video_durations={path: 120.0 for path in paths},
    )

    assert selected == "anchor.mov"
    assert offsets == {"early.mov": -10.0, "anchor.mov": 0.0, "missing-time.mov": -2.0}
    assert diagnostics["early.mov"]["metadata_assisted"] is True
    assert diagnostics["anchor.mov"]["metadata_assisted"] is True
    assert diagnostics["missing-time.mov"]["metadata_assisted"] is False
    assert extraction_calls == [
        ("early.mov", 10.0, 45.0),
        ("anchor.mov", 0.0, 45.0),
        ("anchor.mov", 0.0, None),
        ("missing-time.mov", 0.0, None),
    ]


def test_local_worker_preserves_angle_failures_on_pipeline_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicam_pipeline import pipeline
    from multicam_pipeline.job_runner import _run_pipeline_sync
    from multicam_pipeline.job_schema import MulticamJob

    failure = {
        "file": "broken.mp4",
        "reason": "No audio track; audio is required to synchronize this angle.",
        "category": "validation",
    }

    def fail_render(job):
        job.angle_failures = [failure]
        raise ValueError("Only 1 usable angle remains; at least 2 are required.")

    monkeypatch.setattr(pipeline, "run_multicam_job", fail_render)
    job = MulticamJob(["good.mp4", "broken.mp4"], "output.mp4")

    result = _run_pipeline_sync(job.to_dict())

    assert result[4] == [failure]
    assert result[5].startswith("ValueError:")
    restored = MulticamJob.from_dict({**job.to_dict(), "angle_failures": [failure]})
    assert restored.to_dict()["angle_failures"] == [failure]


def test_render_notifications_include_angle_failure_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from multicam_pipeline import notify

    failure = {"file": "bad<cam>.mp4", "reason": "No audio track.", "category": "validation"}
    messages = []
    monkeypatch.setattr(notify, "_from_email", "render@example.com")
    monkeypatch.setattr(notify, "_get_owner_email", lambda _owner: "owner@example.com")
    monkeypatch.setattr(notify, "_ses", SimpleNamespace(send_email=lambda **message: messages.append(message)))

    notify.send_render_complete("job-1", "Concert", "owner-1", [failure])
    notify.send_render_failed("job-2", "Concert", "owner-1", [failure])

    assert "with skipped angles" in messages[0]["Message"]["Subject"]["Data"]
    assert "bad<cam>.mp4: No audio track." in messages[0]["Message"]["Body"]["Text"]["Data"]
    assert "bad&lt;cam&gt;.mp4" in messages[0]["Message"]["Body"]["Html"]["Data"]
    assert "bad<cam>.mp4: No audio track." in messages[1]["Message"]["Body"]["Text"]["Data"]


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
    duration_seconds: float = 8.0,
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
        "-i", f"{pattern}=size=640x480:rate=30:duration={duration_seconds}",
        "-i", str(audio_path),
        "-filter:a", audio_filter,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(duration_seconds),
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_audio_mixer_fixture(directory: Path):
    from multicam_pipeline.rendering import build_audio_mix_plan

    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, frequency, duration in [("fallback", 660, 8.0), ("master", 440, 2.0), ("room", 1320, 8.0)]:
        audio = directory / f"{name}.wav"
        video = directory / f"{name}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:sample_rate=44100:duration={duration}",
            "-c:a", "pcm_s16le", str(audio),
        ], check=True, capture_output=True)
        _make_synthetic_video(video, "testsrc2", audio, duration_seconds=duration)
        paths[name] = str(video)
    plan = build_audio_mix_plan(
        0.0, 8.0, paths["master"],
        {paths["master"]: 2.0, paths["fallback"]: 0.0, paths["room"]: 0.0},
        {paths["master"]: 2.0, paths["fallback"]: 8.0, paths["room"]: 8.0},
        {paths["fallback"]: 0.8, paths["room"]: 0.4},
    )
    return paths, plan


def _render_audio_mixer_fixture(paths, plan, output: Path, start: float = 0.0) -> np.ndarray:
    from multicam_pipeline.rendering import build_ffmpeg_command

    command = build_ffmpeg_command(
        [CutSegment(start, 8.0, paths["fallback"], 0.0)], str(output), "libx264",
        target_width=320, target_height=240, audio_mix_plan=plan,
    )
    command[command.index("-c:a") + 1] = "pcm_s16le"
    subprocess.run(command, check=True, capture_output=True)
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1",
    ], check=True, capture_output=True)
    return np.frombuffer(decoded.stdout, dtype="<f4").reshape(-1, 2)


def test_audio_mixer_real_stereo_handoffs_and_panning(tmp_path: Path) -> None:
    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")
    paths, plan = _make_audio_mixer_fixture(tmp_path)
    audio = _render_audio_mixer_fixture(paths, plan, tmp_path / "mixed.mov")

    assert len(audio) / 44100 == pytest.approx(8.0, abs=0.03)
    assert float(np.max(np.abs(audio))) <= 0.96
    for start in np.arange(1.5, 4.5, 0.1):
        window = audio[round(start * 44100):round((start + 0.1) * 44100)]
        assert float(np.sqrt(np.mean(window * window))) > 0.01
    window = audio[round(3.0 * 44100):round(3.2 * 44100)]
    amplitudes = {}
    for frequency in (440, 660, 1320):
        phase = np.exp(-2j * np.pi * frequency * np.arange(len(window)) / 44100)
        amplitudes[frequency] = np.abs(np.sum(window * phase[:, None], axis=0)) * 2 / len(window)
    assert amplitudes[440][0] / amplitudes[440][1] == pytest.approx(1.0, abs=0.03)
    assert amplitudes[660][1] / amplitudes[660][0] == pytest.approx(np.tan(np.pi / 8), abs=0.05)
    assert amplitudes[1320][0] / amplitudes[1320][1] == pytest.approx(np.tan(np.pi / 8), abs=0.05)
    assert max(amplitudes[660]) < min(amplitudes[440]) * 0.4
    assert max(amplitudes[1320]) < min(amplitudes[440]) * 0.4
    mono = audio.mean(axis=1)
    assert float(np.sqrt(np.mean(mono * mono))) > 0.01


def test_audio_mixer_real_gaps_are_not_filled_with_other_event_times(tmp_path: Path) -> None:
    from multicam_pipeline.rendering import build_audio_mix_plan

    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")
    paths, _plan = _make_audio_mixer_fixture(tmp_path)
    plan = build_audio_mix_plan(
        0.0, 8.0, paths["master"],
        {paths["master"]: 0.0, paths["room"]: 3.0},
        {paths["master"]: 2.0, paths["room"]: 5.0}, {},
    )
    audio = _render_audio_mixer_fixture(paths, plan, tmp_path / "gap.mov")
    assert not plan.transitions
    assert len(audio) / 44100 == pytest.approx(8.0, abs=0.03)
    gap = audio[round(2.2 * 44100):round(2.8 * 44100)]
    assert float(np.max(np.abs(gap))) < 1e-4
    assert float(np.sqrt(np.mean(audio[round(3.3 * 44100):round(3.8 * 44100)] ** 2))) > 0.01


def test_audio_mixer_stereo_input_is_folded_to_center_without_doubling(tmp_path: Path) -> None:
    from multicam_pipeline.rendering import build_audio_mix_plan

    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")
    paths, _plan = _make_audio_mixer_fixture(tmp_path)
    stereo_audio = tmp_path / "stereo.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=8",
        "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(stereo_audio),
    ], check=True, capture_output=True)
    stereo_video = tmp_path / "stereo.mp4"
    _make_synthetic_video(stereo_video, "testsrc2", stereo_audio)
    plan = build_audio_mix_plan(0.0, 8.0, str(stereo_video), {str(stereo_video): 0.0}, {str(stereo_video): 8.0}, {})
    audio = _render_audio_mixer_fixture(paths, plan, tmp_path / "center.mov")
    assert len(audio) / 44100 == pytest.approx(8.0, abs=0.03)
    assert np.max(np.abs(audio[:, 0] - audio[:, 1])) < 1e-4
    assert float(np.max(np.abs(audio))) < 0.95
    assert float(np.sqrt(np.mean(audio * audio))) > 0.01


def test_audio_mixer_split_mid_fade_keeps_same_waveform(tmp_path: Path) -> None:
    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")
    paths, plan = _make_audio_mixer_fixture(tmp_path)
    whole = _render_audio_mixer_fixture(paths, plan, tmp_path / "whole.mov")
    start = 2.375
    part = _render_audio_mixer_fixture(paths, plan, tmp_path / "part.mov", start=start)
    assert len(part) / 44100 == pytest.approx(8.0 - start, abs=0.03)
    expected = whole[round(start * 44100):round(start * 44100) + len(part)]
    samples = min(len(expected), len(part))
    for channel in (0, 1):
        assert np.corrcoef(expected[441:samples - 441, channel], part[441:samples - 441, channel])[0, 1] > 0.99


@pytest.mark.parametrize("master_offset,part_start", [(2.0, 0.0), (21.366454, 0.0), (2.0, 4.11), (21.366454, 23.476454)])
def test_render_applies_master_audio_offset_once(tmp_path: Path, master_offset: float, part_start: float) -> None:
    from multicam_pipeline.audio_sync import TARGET_SR, _extract_mono_audio, _gcc_phat
    from multicam_pipeline.rendering import build_ffmpeg_command

    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")

    audio_path = tmp_path / "reference.wav"
    early_video = tmp_path / "early.mp4"
    master_video = tmp_path / "master.mp4"
    output_path = tmp_path / "render.mp4"
    _make_reference_audio(audio_path)
    _make_synthetic_video(early_video, "testsrc", audio_path, duration_seconds=master_offset + 8.0)
    _make_synthetic_video(master_video, "testsrc2", audio_path)
    segments = [
        CutSegment(0.0, master_offset + 0.02, str(early_video), 0.0),
        CutSegment(master_offset + 0.02, master_offset + 2.11, str(master_video), master_offset),
        CutSegment(master_offset + 2.11, master_offset + 4.18, str(early_video), 0.0),
        CutSegment(master_offset + 4.18, master_offset + 6.0, str(master_video), master_offset),
    ]
    segments = [
        CutSegment(max(segment.start_time, part_start), segment.end_time, segment.source_video_path, segment.offset)
        for segment in segments if segment.end_time > part_start
    ]
    command = build_ffmpeg_command(
        segments,
        str(output_path),
        "libx264",
        target_width=320,
        target_height=240,
        audio_source_path=str(master_video),
        audio_source_offset=master_offset,
        audio_source_duration=8.0,
    )
    subprocess.run(command, check=True, capture_output=True)
    source_audio = _extract_mono_audio(str(master_video))
    output_audio = _extract_mono_audio(str(output_path))
    result = _gcc_phat(output_audio, source_audio, sr=TARGET_SR)

    assert result.delay_seconds == pytest.approx(master_offset - part_start, abs=0.08)
    assert len(output_audio) / TARGET_SR == pytest.approx(segments[-1].end_time - segments[0].start_time, abs=0.08)


@pytest.mark.parametrize("part_start", [0.0, 3.0, 5.0])
def test_render_replaces_short_master_without_audio_gaps(tmp_path: Path, part_start: float) -> None:
    from multicam_pipeline.audio_sync import TARGET_SR, _extract_mono_audio, _gcc_phat
    from multicam_pipeline.rendering import build_audio_spans, build_ffmpeg_command

    if _missing_prerequisites():
        pytest.skip("FFmpeg/audio prerequisites are unavailable")

    audio_path = tmp_path / "reference.wav"
    fallback_video = tmp_path / "fallback.mp4"
    visible_video = tmp_path / "visible.mp4"
    master_video = tmp_path / "short-master.mp4"
    output_path = tmp_path / "fallback-render.mp4"
    _make_reference_audio(audio_path)
    _make_synthetic_video(fallback_video, "testsrc", audio_path)
    _make_synthetic_video(visible_video, "testsrc2", audio_path)
    subprocess.run([
        "ffmpeg", "-y", "-ss", "2", "-i", str(fallback_video), "-t", "2",
        "-c:v", "libx264", "-c:a", "aac", str(master_video),
    ], check=True, capture_output=True)
    paths = [str(master_video), str(fallback_video), str(visible_video)]
    spans = build_audio_spans(
        part_start, 8.0, str(master_video),
        {paths[0]: 2.0, paths[1]: 0.0, paths[2]: 0.0},
        {paths[0]: 2.0, paths[1]: 8.0, paths[2]: 8.0},
        {paths[1]: 0.9, paths[2]: 0.2},
    )
    command = build_ffmpeg_command(
        [CutSegment(part_start, 8.0, str(visible_video), 0.0)],
        str(output_path), "libx264", target_width=320, target_height=240,
        audio_source_path=str(master_video), audio_spans=spans,
    )
    if part_start >= 4.0:
        assert str(master_video) not in command
    subprocess.run(command, check=True, capture_output=True)
    original = _extract_mono_audio(str(fallback_video))
    output = _extract_mono_audio(str(output_path))
    assert len(output) / TARGET_SR == pytest.approx(8.0 - part_start, abs=0.08)
    for start, end in [(part_start, min(2.0, 8.0)), (max(part_start, 2.0), 4.0), (max(part_start, 4.0), 8.0)]:
        if end - start < 0.5:
            continue
        rendered = output[round((start - part_start) * TARGET_SR):round((end - part_start) * TARGET_SR)]
        source = original[round(start * TARGET_SR):round(end * TARGET_SR)]
        result = _gcc_phat(rendered, source, sr=TARGET_SR)
        assert abs(result.delay_seconds) < 0.08
        assert float(np.sqrt(np.mean(rendered * rendered))) > 0.01


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
