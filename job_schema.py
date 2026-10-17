"""
job_schema.py
-------------
Shared job schema used by both the local asyncio runner and the AWS
SQS/Lambda handler. Keeping the schema in one place ensures both
environments dispatch and track jobs identically.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import List, Optional


class JobStatus(str, Enum):
    PENDING    = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE   = "COMPLETE"
    FAILED     = "FAILED"


class EventType(str, Enum):
    CHEER        = "cheer"
    SPORT        = "sport"
    CONCERT      = "concert"
    DANCE        = "dance"
    WEDDING      = "wedding"
    SPOKEN_WORD  = "spoken_word"


class CuttingStrategy(str, Enum):
    LOCAL       = "local"        # free tier — librosa + OpenCV
    REKOGNITION = "rekognition"  # paid tier — AWS Rekognition
    INTERVAL    = "interval"     # fallback — round-robin fixed interval


class EffectIntensity(str, Enum):
    SUBTLE    = "subtle"
    BALANCED  = "balanced"
    CINEMATIC = "cinematic"


class OrientationMode(str, Enum):
    AUTO      = "auto"
    LANDSCAPE = "landscape"
    PORTRAIT  = "portrait"
    NATIVE    = "native"


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
    orientation_mode:         OrientationMode = OrientationMode.AUTO
    orientation_crop_depth:   int             = 100
    creator_video_paths:      List[str]        = field(default_factory=list)
    rekognition_sample_rate:  int             = 15
    audio_source_override:    Optional[str]   = None
    selected_audio_source_path: Optional[str] = None
    sync_diagnostics:         Optional[dict]  = None
    project_id:               Optional[str]   = None
    output_parts:             List[str]        = field(default_factory=list)
    max_duration:             Optional[float] = 240.0
    auto_split:               bool            = True
    part_index:               Optional[int]   = None
    total_parts:              Optional[int]   = None
    force_full_track_sync:    bool            = False
    angle_failures:          List[dict[str, str]] = field(default_factory=list)
    # Basenames of input files silently dropped for being below the 480px
    # minimum resolution (typically iPhone/Photos thumbnail or companion tracks).
    excluded_low_quality_files: List[str]     = field(default_factory=list)
    job_id:                   str             = field(default_factory=lambda: str(uuid.uuid4()))
    status:                   JobStatus       = JobStatus.PENDING
    error:                    Optional[str]   = None
    created_at:               str             = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at:               str             = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Output retention: set on completion, output is purged after this time.
    expires_at:               Optional[str]   = None
    output_purged:            bool            = False

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
            "orientation_mode":        self.orientation_mode.value,
            "orientation_crop_depth":  self.orientation_crop_depth,
            "creator_video_paths":     self.creator_video_paths,
            "rekognition_sample_rate": self.rekognition_sample_rate,
            "audio_source_override":   self.audio_source_override,
            "selected_audio_source_path": self.selected_audio_source_path,
            "sync_diagnostics":         self.sync_diagnostics,
            "project_id":              self.project_id,
            "output_parts":            self.output_parts,
            "max_duration":            self.max_duration,
            "auto_split":              self.auto_split,
            "part_index":              self.part_index,
            "total_parts":             self.total_parts,
            "force_full_track_sync":   self.force_full_track_sync,
            "angle_failures":          self.angle_failures,
            "excluded_low_quality_files": self.excluded_low_quality_files,
            "status":                  self.status.value,
            "error":                   self.error,
            "created_at":              self.created_at,
            "updated_at":              self.updated_at,
            "expires_at":              self.expires_at,
            "output_purged":           self.output_purged,
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
            orientation_mode         = OrientationMode(data.get("orientation_mode", "auto")),
            orientation_crop_depth   = int(data.get("orientation_crop_depth", 100)),
            creator_video_paths      = list(data.get("creator_video_paths") or []),
            rekognition_sample_rate  = int(data.get("rekognition_sample_rate", 15)),
            audio_source_override     = data.get("audio_source_override"),
            selected_audio_source_path = data.get("selected_audio_source_path"),
            sync_diagnostics          = data.get("sync_diagnostics"),
            project_id               = data.get("project_id"),
            output_parts             = list(data.get("output_parts") or []),
            max_duration             = float(data["max_duration"]) if data.get("max_duration") is not None else 240.0,
            auto_split               = bool(data.get("auto_split", True)),
            part_index               = int(data["part_index"]) if data.get("part_index") is not None else None,
            total_parts              = int(data["total_parts"]) if data.get("total_parts") is not None else None,
            force_full_track_sync    = bool(data.get("force_full_track_sync", False)),
            angle_failures           = list(data.get("angle_failures") or []),
            excluded_low_quality_files = list(data.get("excluded_low_quality_files") or []),
            status                   = JobStatus(data.get("status", JobStatus.PENDING)),
            error                    = data.get("error"),
            created_at               = data.get("created_at", ""),
            updated_at               = data.get("updated_at", ""),
            expires_at               = data.get("expires_at"),
            output_purged            = bool(data.get("output_purged", False)),
        )

    def mark_processing(self) -> None:
        self.status     = JobStatus.PROCESSING
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_complete(self, retention_hours: float = 24.0) -> None:
        self.status     = JobStatus.COMPLETE
        self.updated_at = datetime.now(timezone.utc).isoformat()
        self.expires_at = (datetime.now(timezone.utc) + timedelta(hours=retention_hours)).isoformat()

    def mark_failed(self, error: str) -> None:
        self.status     = JobStatus.FAILED
        self.error      = error
        self.updated_at = datetime.now(timezone.utc).isoformat()
