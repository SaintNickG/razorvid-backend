"""
job_schema.py
-------------
Shared job schema used by both the local asyncio runner and the AWS
SQS/Lambda handler. Keeping the schema in one place ensures both
environments dispatch and track jobs identically.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class JobStatus(str, Enum):
    PENDING    = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE   = "COMPLETE"
    FAILED     = "FAILED"


class EventType(str, Enum):
    CHEER   = "cheer"
    SPORT   = "sport"
    CONCERT = "concert"
    DANCE   = "dance"


class CuttingStrategy(str, Enum):
    LOCAL       = "local"        # free tier — librosa + OpenCV
    REKOGNITION = "rekognition"  # paid tier — AWS Rekognition
    INTERVAL    = "interval"     # fallback — round-robin fixed interval


class EffectIntensity(str, Enum):
    SUBTLE    = "subtle"
    BALANCED  = "balanced"
    CINEMATIC = "cinematic"


@dataclass
class MulticamJob:
    """
    Represents a single multicam render job passed between the API,
    job runner, and status store.

    Attributes:
        job_id:                   Unique identifier for this job.
        video_paths:              List of local paths or S3 URIs for input videos.
        output_path:              Destination path or S3 URI for the rendered MP4.
        cut_interval:             Seconds between angle switches (interval mode only).
        target_width:             Output resolution width.
        target_height:            Output resolution height.
        target_fps:               Output frame rate.
        cutting_strategy:         AI cutting mode: local | rekognition | interval.
        event_type:               Event type: cheer | sport | concert | dance.
        effect_intensity:         Visual effects intensity: subtle | balanced | cinematic.
        rekognition_sample_rate:  Analyze every Nth frame (paid tier only).
        audio_source_override:    Optional input video path explicitly chosen by user.
        selected_audio_source_path: Actual source path chosen as master audio.
        status:                   Current job lifecycle status.
        error:                    Error message if status is FAILED.
        created_at:               ISO 8601 UTC timestamp of job creation.
        updated_at:               ISO 8601 UTC timestamp of last status update.
    """
    video_paths:              List[str]
    output_path:              str
    cut_interval:             float           = 5.0
    target_width:             int             = 1920
    target_height:            int             = 1080
    target_fps:               int             = 30
    cutting_strategy:         CuttingStrategy = CuttingStrategy.LOCAL
    event_type:               EventType       = EventType.CHEER
    effect_intensity:         EffectIntensity = EffectIntensity.BALANCED
    transition_style:         str             = "cut"
    rekognition_sample_rate:  int             = 15
    audio_source_override:    Optional[str]   = None
    selected_audio_source_path: Optional[str] = None
    sync_diagnostics:         Optional[dict]  = None
    project_id:               Optional[str]   = None
    job_id:                   str             = field(default_factory=lambda: str(uuid.uuid4()))
    status:                   JobStatus       = JobStatus.PENDING
    error:                    Optional[str]   = None
    created_at:               str             = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at:               str             = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        """Serialize the job to a plain dict (for SQS message body or DynamoDB item)."""
        return {
            "job_id":                  self.job_id,
            "video_paths":             self.video_paths,
            "output_path":             self.output_path,
            "cut_interval":            self.cut_interval,
            "target_width":            self.target_width,
            "target_height":           self.target_height,
            "target_fps":              self.target_fps,
            "cutting_strategy":        self.cutting_strategy.value,
            "event_type":              self.event_type.value,
            "effect_intensity":        self.effect_intensity.value,
            "transition_style":        self.transition_style,
            "rekognition_sample_rate": self.rekognition_sample_rate,
            "audio_source_override":   self.audio_source_override,
            "selected_audio_source_path": self.selected_audio_source_path,
            "sync_diagnostics":         self.sync_diagnostics,
            "project_id":              self.project_id,
            "status":                  self.status.value,
            "error":                   self.error,
            "created_at":              self.created_at,
            "updated_at":              self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MulticamJob":
        """Deserialize a job from a plain dict (from SQS message or DynamoDB item)."""
        return cls(
            job_id                   = data["job_id"],
            video_paths              = data["video_paths"],
            output_path              = data["output_path"],
            cut_interval             = float(data.get("cut_interval", 5.0)),
            target_width             = int(data.get("target_width", 1920)),
            target_height            = int(data.get("target_height", 1080)),
            target_fps               = int(data.get("target_fps", 30)),
            cutting_strategy         = CuttingStrategy(data.get("cutting_strategy", "local")),
            event_type               = EventType(data.get("event_type", "cheer")),
            effect_intensity         = EffectIntensity(data.get("effect_intensity", "balanced")),
            transition_style         = data.get("transition_style", "cut"),
            rekognition_sample_rate  = int(data.get("rekognition_sample_rate", 15)),
            audio_source_override     = data.get("audio_source_override"),
            selected_audio_source_path = data.get("selected_audio_source_path"),
            sync_diagnostics          = data.get("sync_diagnostics"),
            project_id               = data.get("project_id"),
            status                   = JobStatus(data.get("status", JobStatus.PENDING)),
            error                    = data.get("error"),
            created_at               = data.get("created_at", ""),
            updated_at               = data.get("updated_at", ""),
        )

    def mark_processing(self) -> None:
        self.status     = JobStatus.PROCESSING
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_complete(self) -> None:
        self.status     = JobStatus.COMPLETE
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_failed(self, error: str) -> None:
        self.status     = JobStatus.FAILED
        self.error      = error
        self.updated_at = datetime.now(timezone.utc).isoformat()
