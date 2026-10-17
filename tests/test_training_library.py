import json

import pytest

from multicam_pipeline.training_library import (
    CutAnnotation,
    SourceProvenance,
    TrainingExample,
    load_manifest,
    write_manifest,
)


def _example() -> TrainingExample:
    return TrainingExample(
        example_id="concert-001",
        media_uri="s3://razorvid-training/concert-001.mp4",
        media_kind="video",
        split="train",
        provenance=SourceProvenance(
            source_url="https://example.org/concert-001",
            license_spdx="CC-BY-4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            retrieved_at="2026-09-22",
            attribution="Example Creator",
            consent_status="public_license",
        ),
        annotations=(CutAnnotation(
            start_seconds=0.0,
            end_seconds=3.5,
            selected_angle="camera-a",
            reason="action_peak",
            annotator="human:editor-1",
            confidence=0.92,
            reference_source="concert-interval-profile-v1",
        ),),
        tags=("concert", "multicam", "action_peak"),
        source_group_id="concert-session-001",
    )


def test_manifest_round_trip(tmp_path) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(path, [_example()])

    loaded = list(load_manifest(path))

    assert loaded == [_example()]


def test_manifest_rejects_missing_provenance(tmp_path) -> None:
    path = tmp_path / "manifest.jsonl"
    payload = _example().to_dict()
    payload["provenance"]["license_url"] = ""
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="license_url"):
        list(load_manifest(path))


def test_annotations_must_be_ordered_and_non_overlapping() -> None:
    example = _example()
    invalid = TrainingExample(
        **{**example.__dict__, "annotations": (
            example.annotations[0],
            CutAnnotation(2.0, 4.0, "camera-b", "reaction", "human:editor-1"),
        )}
    )

    assert any("overlaps" in error for error in invalid.validate())