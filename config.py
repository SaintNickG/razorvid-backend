"""
config.py
---------
Centralized configuration loaded from environment variables.
Set ENV=local for development, ENV=aws for production.

Local dev (.env):
    ENV=local
    LOCAL_UPLOAD_DIR=/tmp/multicam/uploads
    LOCAL_OUTPUT_DIR=/tmp/multicam/output

AWS production (.env):
    ENV=aws
    S3_INPUT_BUCKET=my-multicam-input
    S3_OUTPUT_BUCKET=my-multicam-output
    DYNAMODB_TABLE=multicam-jobs
    SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account>/multicam-jobs
    AWS_REGION=us-east-1
"""

import os


# ---------------------------------------------------------------------------
# Environment mode
# ---------------------------------------------------------------------------

ENV = os.environ.get("ENV", "local")  # "local" | "aws"
IS_LOCAL = ENV == "local"
IS_AWS   = ENV == "aws"

# ---------------------------------------------------------------------------
# Local dev paths
# ---------------------------------------------------------------------------

LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR", "/tmp/multicam/uploads")
LOCAL_OUTPUT_DIR = os.environ.get("LOCAL_OUTPUT_DIR", "/tmp/multicam/output")

# ---------------------------------------------------------------------------
# AWS settings
# ---------------------------------------------------------------------------

AWS_REGION        = os.environ.get("AWS_REGION", "us-east-1")
S3_INPUT_BUCKET   = os.environ.get("S3_INPUT_BUCKET", "multicam-input")
S3_OUTPUT_BUCKET  = os.environ.get("S3_OUTPUT_BUCKET", "multicam-output")
DYNAMODB_TABLE    = os.environ.get("DYNAMODB_TABLE", "multicam-jobs")
SQS_QUEUE_URL     = os.environ.get("SQS_QUEUE_URL", "")

# ---------------------------------------------------------------------------
# Shared pipeline defaults
# ---------------------------------------------------------------------------

DEFAULT_CUT_INTERVAL  = float(os.environ.get("DEFAULT_CUT_INTERVAL", "5.0"))
DEFAULT_TARGET_WIDTH  = int(os.environ.get("DEFAULT_TARGET_WIDTH", "1920"))
DEFAULT_TARGET_HEIGHT = int(os.environ.get("DEFAULT_TARGET_HEIGHT", "1080"))
DEFAULT_TARGET_FPS    = int(os.environ.get("DEFAULT_TARGET_FPS", "30"))

# Max upload size: 1GB per file during beta. The API streams each upload through
# the ECS task before S3 storage, so this leaves capacity for concurrent requests.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(1 * 1024 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# AI cutting tiers
# ---------------------------------------------------------------------------

# Cutting strategy per plan: "local" (free) | "rekognition" (paid)
FREE_TIER_STRATEGY  = os.environ.get("FREE_TIER_STRATEGY",  "local")
PAID_TIER_STRATEGY  = os.environ.get("PAID_TIER_STRATEGY",  "rekognition")

# Supported event types
EVENT_TYPES = ["cheer", "sport", "concert", "dance", "wedding", "spoken_word"]

# Default Rekognition sample rate — analyze every Nth frame
# Lower N = higher quality, higher cost. Configurable per job.
DEFAULT_REKOGNITION_SAMPLE_RATE = int(os.environ.get("DEFAULT_REKOGNITION_SAMPLE_RATE", "15"))

# Local OpenCV motion analysis samples sequential frames at this rate, then
# interpolates scores onto the higher-resolution audio timeline.
MOTION_ANALYSIS_FPS = float(os.environ.get("MOTION_ANALYSIS_FPS", "2.0"))

# Cost per 1000 Rekognition DetectLabels calls (USD)
# Used by the frontend slider to show live cost estimates
REKOGNITION_COST_PER_1000 = float(os.environ.get("REKOGNITION_COST_PER_1000", "1.00"))

# Minimum seconds between cuts (prevents seizure-inducing rapid cuts)
MIN_CUT_DURATION = float(os.environ.get("MIN_CUT_DURATION", "2.0"))

# Maximum seconds on the same angle before forcing a switch
MAX_CUT_DURATION = float(os.environ.get("MAX_CUT_DURATION", "8.0"))
