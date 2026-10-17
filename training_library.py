"""Manifest and validation primitives for the RazorVid training library.

Media stays in object storage; this module stores only reproducible metadata
and cut annotations in JSON Lines manifests.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlparse


SUPPORTED_MEDIA_KINDS = {"video", "audio", "image"}
SUPPORTED_SPLITS = {"train", "validation", "test"}
REQUIRED_LICENSE_FIELDS = ("license_spdx", "license_url")


@dataclass(frozen=True)
class SourceProvenance:
    """Rights and retrieval facts for one source asset."""

    source_url: str
    license_spdx: str
    license_url: str
    retrieved_at: str
    attribution: str = ""
    creator: str = ""
    consent_status: str = "not_applicable"
    usage_notes: str = ""


@dataclass(frozen=True)
class CutAnnotation:
    """Human or algorithm-generated decision for a timeline interval."""

    start_seconds: float
    end_seconds: float
    selected_angle: str
    reason: str
    annotator: str
    confidence: Optional[float] = None
    reference_source: Optional[str] = None


@dataclass(frozen=True)
class TrainingExample:
    """One training/evaluation example represented by a manifest row."""

    example_id: str
    media_uri: str
    media_kind: str
    split: str
    provenance: SourceProvenance
    annotations: tuple[CutAnnotation, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    source_group_id: Optional[str] = None

    def validate(self) -> list[str]:
        """Return actionable validation errors without touching media bytes."""
        errors: list[str] = []
        if not self.example_id.strip():
            errors.append("example_id is required")
        if not self.media_uri.strip():
            errors.append("media_uri is required")
        if self.media_kind not in SUPPORTED_MEDIA_KINDS:
            errors.append(f"media_kind must be one of {sorted(SUPPORTED_MEDIA_KINDS)}")
        if self.split not in SUPPORTED_SPLITS:
            errors.append(f"split must be one of {sorted(SUPPORTED_SPLITS)}")

        for field_name in REQUIRED_LICENSE_FIELDS:
            if not getattr(self.provenance, field_name).strip():
                errors.append(f"provenance.{field_name} is required")
        if not _is_http_url(self.provenance.source_url):
            errors.append("provenance.source_url must be an http(s) URL")
        if not _is_http_url(self.provenance.license_url):
            errors.append("provenance.license_url must be an http(s) URL")
        try:
            date.fromisoformat(self.provenance.retrieved_at)
        except ValueError:
            errors.append("provenance.retrieved_at must be an ISO date")

        previous_end = -1.0
        for index, annotation in enumerate(self.annotations):
            if annotation.start_seconds < 0 or annotation.end_seconds <= annotation.start_seconds:
                errors.append(f"annotations[{index}] must have a positive time range")
            if annotation.start_seconds < previous_end:
                errors.append(f"annotations[{index}] overlaps the previous annotation")
            if annotation.confidence is not None and not 0.0 <= annotation.confidence <= 1.0:
                errors.append(f"annotations[{index}].confidence must be between 0 and 1")
            previous_end = annotation.end_seconds
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TrainingExample":
        provenance = SourceProvenance(**payload["provenance"])
        annotations = tuple(CutAnnotation(**item) for item in payload.get("annotations", []))
        return cls(
            example_id=payload["example_id"],
            media_uri=payload["media_uri"],
            media_kind=payload["media_kind"],
            split=payload["split"],
            provenance=provenance,
            annotations=annotations,
            tags=tuple(payload.get("tags", [])),
            source_group_id=payload.get("source_group_id"),
        )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def load_manifest(path: str | Path) -> Iterator[TrainingExample]:
    """Yield validated-schema examples from a JSONL manifest."""
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                example = TrainingExample.from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid manifest row {line_number}: {exc}") from exc
            errors = example.validate()
            if errors:
                raise ValueError(f"Invalid manifest row {line_number}: {'; '.join(errors)}")
            yield example


def write_manifest(path: str | Path, examples: Iterable[TrainingExample]) -> None:
    """Write deterministic JSONL after validating every example."""
    rows = []
    for example in examples:
        errors = example.validate()
        if errors:
            raise ValueError(f"Invalid example '{example.example_id}': {'; '.join(errors)}")
        rows.append(json.dumps(example.to_dict(), sort_keys=True))
    Path(path).write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")