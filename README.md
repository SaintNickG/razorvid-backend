# 🎬 Multicam Pipeline

A backend pipeline for multi-angle video processing. Upload videos from multiple camera angles, synchronize them by audio track using GCC-PHAT cross-correlation, and render a single multicam timeline MP4 with automated cuts — all asynchronously.

---

## Table of Contents

- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Environment Configuration](#environment-configuration)
- [API Reference](#api-reference)
- [Pipeline Internals](#pipeline-internals)
- [Job Environments](#job-environments)
- [Make Commands](#make-commands)
- [AWS Infrastructure](#aws-infrastructure)
- [Dependencies](#dependencies)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Client                               │
│         Browser Upload Form  /  API Consumer                │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   FastAPI (main.py)                         │
│                                                             │
│   POST /upload   POST /render   GET /status   GET /download │
└──────┬──────────────────┬───────────────────────────────────┘
       │                  │
       ▼                  ▼
┌────────────┐   ┌─────────────────────────────────────────┐
│  Storage   │   │           Job Dispatch                  │
│            │   │                                         │
│ Local disk │   │  ENV=local → asyncio + ProcessPool      │
│ or S3      │   │  ENV=aws   → SQS → Lambda               │
└────────────┘   └──────────────────┬──────────────────────┘
                                    │
                                    ▼
                 ┌─────────────────────────────────────────┐
                 │            Pipeline Core                 │
                 │                                         │
                 │  ingestion.py   → validate + metadata   │
                 │  audio_sync.py  → GCC-PHAT alignment    │
                 │  multicam_cutter.py → cut list builder  │
                 │  rendering.py   → FFmpeg filtergraph    │
                 └──────────────────┬──────────────────────┘
                                    │
                                    ▼
                 ┌─────────────────────────────────────────┐
                 │              Output                      │
                 │                                         │
                 │  Local: /tmp/multicam/output/           │
                 │  AWS:   s3://<output-bucket>/<job-id>/  │
                 └─────────────────────────────────────────┘
```

---

## Project Structure

```
multicam_pipeline/
├── main.py                 # FastAPI app entry point
├── config.py               # Centralized env var config
├── Dockerfile              # Multi-stage Docker build
├── docker-compose.yml      # Local container orchestration
├── Makefile                # Shorthand dev commands
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .dockerignore
│
├── routers/
│   ├── upload.py           # POST /upload
│   ├── jobs.py             # POST /render  GET /status/{id}  GET /jobs
│   └── download.py         # GET  /download/{job_id}
│
├── static/
│   └── index.html          # Drag-and-drop upload form
│
├── ingestion.py            # File validation + FFprobe metadata
├── audio_sync.py           # GCC-PHAT audio alignment
├── multicam_cutter.py      # Cut list builder + CutSegment schema
├── rendering.py            # FFmpeg filtergraph renderer
├── job_schema.py           # Shared MulticamJob dataclass
├── job_runner.py           # Local asyncio job runner
└── aws_handler.py          # AWS Lambda + SQS + S3 + DynamoDB handler
```

---

## Quick Start

## Deployment checklist

Before the first production deploy, complete these items:

1. Create the AWS resources the backend expects:
   - S3 input bucket for uploads
   - S3 output bucket for rendered videos
   - DynamoDB table for job status
   - SQS queue for job dispatch
2. Set the runtime environment variables in the ECS task definition:
   - `ENV=aws`
   - `AWS_REGION`
   - `S3_INPUT_BUCKET`
   - `S3_OUTPUT_BUCKET`
   - `DYNAMODB_TABLE`
  - `PROJECTS_DYNAMODB_TABLE`
   - `SQS_QUEUE_URL`
   - `ALLOWED_ORIGINS`
   - `AUTH_REQUIRED` (set to `false` for the first deploy)
  - `COGNITO_ISSUER` (required when `AUTH_REQUIRED=true`)
  - `COGNITO_CLIENT_ID` (recommended when `AUTH_REQUIRED=true`)
  - Optional: `COGNITO_JWKS_URL` (overrides issuer-derived JWKS URL)
  - Optional: `AUTH_ALLOW_UNVERIFIED_TOKENS=false` (keep false in production)
  - Optional: `SQS_MESSAGE_GROUP_ID` (used only when `SQS_QUEUE_URL` points to a FIFO queue)
  - `STRIPE_SECRET_KEY` (required for checkout session creation)
  - `STRIPE_WEBHOOK_SECRET` (required for webhook verification)
  - `STRIPE_PRICE_IDS_JSON` (for example: `{"pro":"price_abc123"}`)
  - Optional: `STRIPE_DEFAULT_SUCCESS_URL`, `STRIPE_DEFAULT_CANCEL_URL`
  - Optional: `BILLING_STORE_PATH=/tmp/multicam/billing.json`
  - Recommended in AWS: `BILLING_DYNAMODB_TABLE=multicam-billing`
  - Optional: `ADMIN_USER_IDS` (comma-separated principal IDs)
  - Optional: `ADMIN_GROUPS` (comma-separated claim groups, default `admin`)
3. Configure the frontend production env file with the deployed API URL:
   - `NEXT_PUBLIC_API_URL=https://api.your-domain.com`
4. Point `api.your-domain.com` to the ALB DNS name created for the ECS service.
5. If you want auth enabled later, set `AUTH_REQUIRED=true` and provide Cognito values.
6. Test the upload → render → dashboard flow end to end.

### Render Worker

AWS renders are consumed by the `RazorVidRenderWorker` Lambda from the
`multicam-jobs` SQS queue. Deploy it after the API and whenever pipeline code
changes:

```bash
AWS_PROFILE=razorvid ./deploy-render-worker.sh
```

The script builds `Dockerfile.worker`, creates or updates the Lambda worker,
configures its S3/DynamoDB/SQS permissions, ensures the queue mapping is enabled,
and configures a dead-letter queue plus CloudWatch alarms for worker errors,
stalled queue messages, and exhausted retries.


### With Docker (recommended)

```bash
# 1. Clone the repo and enter the project directory
cd multicam_pipeline

# 2. Create your environment file
cp .env.example .env

# 3. Build and start
make

# 4. Open in browser
open http://localhost:8000
```

### Without Docker (local Python)

```bash
# Requires: Python 3.11+, FFmpeg installed on PATH

# 1. Install dependencies
make install

# 2. Create your environment file
cp .env.example .env

# 3. Start the API
make dev

# 4. Open in browser
open http://localhost:8000
```

> Install FFmpeg on macOS: `brew install ffmpeg`
>
> Install FFmpeg on Ubuntu/Debian: `sudo apt install ffmpeg`

---

## Environment Configuration

Copy `.env.example` to `.env` and configure for your environment.

### Local development (`ENV=local`)

```env
ENV=local
LOCAL_UPLOAD_DIR=/tmp/multicam/uploads
LOCAL_OUTPUT_DIR=/tmp/multicam/output
DEFAULT_CUT_INTERVAL=5.0
DEFAULT_TARGET_WIDTH=1920
DEFAULT_TARGET_HEIGHT=1080
DEFAULT_TARGET_FPS=30
ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
```

### AWS production (`ENV=aws`)

```env
ENV=aws
AWS_REGION=us-east-1
S3_INPUT_BUCKET=my-multicam-input
S3_OUTPUT_BUCKET=my-multicam-output
DYNAMODB_TABLE=multicam-jobs
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/multicam-jobs
ALLOWED_ORIGINS=https://your-frontend-domain.com
```

| Variable | Description | Default |
|---|---|---|
| `ENV` | Runtime mode: `local` or `aws` | `local` |
| `LOCAL_UPLOAD_DIR` | Local upload directory | `/tmp/multicam/uploads` |
| `LOCAL_OUTPUT_DIR` | Local output directory | `/tmp/multicam/output` |
| `AWS_REGION` | AWS region | `us-east-1` |
| `S3_INPUT_BUCKET` | S3 bucket for uploaded videos | — |
| `S3_OUTPUT_BUCKET` | S3 bucket for rendered output | — |
| `DYNAMODB_TABLE` | DynamoDB table for job status | `multicam-jobs` |
| `PROJECTS_DYNAMODB_TABLE` | DynamoDB table for durable projects/invite data | empty (JSON fallback) |
| `SQS_QUEUE_URL` | SQS queue URL for job dispatch | — |
| `SQS_MESSAGE_GROUP_ID` | FIFO message group id (FIFO queues only) | `multicam` |
| `DEFAULT_CUT_INTERVAL` | Seconds between angle switches | `5.0` |
| `DEFAULT_TARGET_WIDTH` | Output video width (px) | `1920` |
| `DEFAULT_TARGET_HEIGHT` | Output video height (px) | `1080` |
| `DEFAULT_TARGET_FPS` | Output frame rate | `30` |
| `MAX_UPLOAD_BYTES` | Max upload size per file | `2147483648` (2GB) |
| `ALLOWED_ORIGINS` | CORS allowed origins (comma-separated) | `http://localhost:3000` |
| `COGNITO_ISSUER` | Cognito token issuer URL for JWT validation | — |
| `COGNITO_CLIENT_ID` | Cognito app client id (audience/client_id claim check) | — |
| `COGNITO_JWKS_URL` | Explicit JWKS endpoint override | derived from issuer |
| `AUTH_ALLOW_UNVERIFIED_TOKENS` | Allow unverified JWT parsing (dev only) | `true` local, `false` aws |
| `STRIPE_SECRET_KEY` | Stripe API secret key | — |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | — |
| `STRIPE_PRICE_IDS_JSON` | JSON map of plan name to Stripe price id | `{}` |
| `STRIPE_DEFAULT_SUCCESS_URL` | Checkout success redirect | `https://razorvid.com/billing/success` |
| `STRIPE_DEFAULT_CANCEL_URL` | Checkout cancel redirect | `https://razorvid.com/billing/cancel` |
| `BILLING_STORE_PATH` | Local JSON persistence path for billing state | `/tmp/multicam/billing.json` |
| `BILLING_DYNAMODB_TABLE` | DynamoDB table for durable billing ledger (AWS mode) | empty (JSON fallback) |
| `ADMIN_USER_IDS` | Comma-separated principal IDs treated as admins | empty |
| `ADMIN_GROUPS` | Comma-separated claim groups treated as admins | `admin` |

If `PROJECTS_DYNAMODB_TABLE` is set in AWS mode, project/invite persistence uses DynamoDB. If omitted, the API falls back to `PROJECT_STORE_PATH` JSON storage.

### Billing DynamoDB table

If `BILLING_DYNAMODB_TABLE` is set in AWS mode, billing state is stored in DynamoDB instead of `/tmp` JSON.

Expected table key schema:

- Partition key: `pk` (String)
- Sort key: `sk` (String)

Stored item shapes:

- User profile: `pk=USER#{user_id}`, `sk=PROFILE`
- Stripe customer map: `pk=CUSTOMER#{customer_id}`, `sk=MAP`
- Billing event log: `pk=EVENTS`, `sk={iso_timestamp}#{uuid}`

Admin observability endpoint for billing backend readiness:

- `GET /admin/observability/billing-health`
  - Verifies selected billing backend reachability
  - In DynamoDB mode, validates expected key schema (`pk`, `sk`)

General health endpoint deep mode:

- `GET /health?deep=1`
  - Includes dependency diagnostics used by runtime billing backend
  - Useful for external monitoring and deployment smoke tests

---

## API Reference

Interactive docs available at:

| URL | Description |
|---|---|
| `http://localhost:8000/docs` | Swagger UI — try every endpoint in the browser |
| `http://localhost:8000/redoc` | ReDoc — clean readable API reference |

### Workflow

```
POST /upload  →  POST /render  →  GET /status/{job_id}  →  GET /download/{job_id}
```

---

### `POST /upload`

Upload one or more video files as `multipart/form-data`.

**Request**
```
Content-Type: multipart/form-data
files: <video file(s)>
```

**Response `200`**
```json
{
  "upload_id": "abc-123",
  "files": [
    "/tmp/multicam/uploads/abc-123_cam1.mp4",
    "/tmp/multicam/uploads/abc-123_cam2.mp4"
  ]
}
```

---

### `POST /render`

Submit a multicam render job. Returns immediately with `202 Accepted`.

**Request**
```json
{
  "files": [
    "/tmp/multicam/uploads/abc-123_cam1.mp4",
    "/tmp/multicam/uploads/abc-123_cam2.mp4"
  ],
  "upload_id": "abc-123",
  "cut_interval": 5.0,
  "target_width": 1920,
  "target_height": 1080,
  "target_fps": 30
}
```

**Response `202`**
```json
{
  "job_id": "def-456",
  "status": "PENDING",
  "output_path": "/tmp/multicam/output/abc-123/output.mp4",
  "error": null,
  "created_at": "2024-01-15T10:30:00+00:00",
  "updated_at": "2024-01-15T10:30:00+00:00"
}
```

---

### `GET /status/{job_id}`

Poll the current status of a render job.

**Response `200`**
```json
{
  "job_id": "def-456",
  "status": "COMPLETE",
  "output_path": "/tmp/multicam/output/abc-123/output.mp4",
  "error": null,
  "created_at": "2024-01-15T10:30:00+00:00",
  "updated_at": "2024-01-15T10:32:45+00:00"
}
```

| Status | Meaning |
|---|---|
| `PENDING` | Job queued, waiting for a worker |
| `PROCESSING` | Pipeline running (sync + render) |
| `COMPLETE` | Render finished, output ready |
| `FAILED` | Pipeline error — see `error` field |

---

### `GET /download/{job_id}`

Download the rendered multicam MP4.

- **Local:** streams the file directly (`200 OK`, `video/mp4`)
- **AWS:** redirects to a pre-signed S3 URL (`302 Redirect`, valid 1 hour)

---

### `GET /jobs`

List all jobs. Local dev only — returns `501` in AWS mode.

---

### `GET /health`

Liveness check.

**Response `200`**
```json
{ "status": "ok", "env": "local" }
```

---

## Pipeline Internals

### 1. Ingestion (`ingestion.py`)

Validates each uploaded file and extracts metadata via FFprobe:
- Duration, FPS, resolution, audio channel count
- Rejects files with no audio stream (required for sync)
- Rejects files shorter than 1 second

### 2. Audio Synchronization (`audio_sync.py`)

Computes time offsets between all video angles using **GCC-PHAT** (Generalized Cross-Correlation with Phase Transform):

1. Extract mono audio at 22,050 Hz from each video via FFmpeg
2. Zero-pad signals to the next power of 2 for efficient FFT
3. Compute cross-power spectrum: `X = FFT(sig) × conj(FFT(ref))`
4. Whiten by magnitude: `X_phat = X / |X|` — sharpens the correlation peak
5. IFFT → find peak lag → convert to seconds

Returns `{ "cam1.mp4": 0.0, "cam2.mp4": 1.243, "cam3.mp4": -0.512 }` where the first video is always the master reference at `0.0`.

### 3. Multicam Cutting (`multicam_cutter.py`)

Builds an ordered `CutSegment` list by walking the master timeline in `cut_interval` windows and round-robin cycling through available angles:

- Skips angles that have no content in a given window (based on offset + duration)
- Clamps segment boundaries to actual content ranges
- Each `CutSegment` exposes `source_start` / `source_end` with offset math pre-applied for the renderer

### 4. Rendering (`rendering.py`)

Builds and executes a single FFmpeg command using a `concat` filtergraph:

- Input-side `-ss`/`-to` per segment (faster than output-side seeking)
- Per-segment: `scale` → `pad` (black bars for aspect ratio) → `fps` → `setsar=1`
- Audio: `aformat` normalization to stereo 44.1kHz
- Hardware encoder auto-detection: VideoToolbox (macOS) → NVENC (Linux) → VAAPI (Linux) → libx264 fallback
- Output: H.264 / AAC MP4 with `-movflags +faststart` for progressive playback

---

## Job Environments

### Local (`ENV=local`)

```
FastAPI → asyncio.create_task() → ProcessPoolExecutor worker → pipeline
                                         ↓
                                  in-memory job store
```

- Non-blocking: web handler returns `job_id` immediately
- Pipeline runs in a separate process (CPU-bound work off the event loop)
- Job status stored in memory — resets on server restart
- Max 4 concurrent jobs (configurable via `ProcessPoolExecutor(max_workers=N)`)

### AWS (`ENV=aws`)

```
FastAPI → SQS.send_message() → Lambda (triggered by SQS)
                                      ↓
                              S3 download → pipeline → S3 upload
                                      ↓
                                  DynamoDB (status)
```

- Fully decoupled — API and processing scale independently
- SQS retries failed messages automatically (via `batchItemFailures`)
- Job status persisted in DynamoDB — survives restarts
- Lambda `/tmp` used for intermediate files (up to 10GB configurable)

---

## Make Commands

```bash
make              # Build image and start API (default)
make build        # Rebuild image from scratch (no cache)
make down         # Stop containers, keep volumes
make restart      # Restart the API container
make logs         # Stream live logs from the API
make shell        # Open bash shell inside the container
make clean        # Stop containers and remove local image
make wipe         # Stop containers, remove image, delete all files

make install      # Install Python deps locally (no Docker)
make dev          # Run API locally with hot-reload (no Docker)
make lint         # Run ruff linter

make help         # List all commands
```

---

## AWS Infrastructure

### Required AWS resources

| Resource | Purpose |
|---|---|
| S3 input bucket | Stores uploaded video files |
| S3 output bucket | Stores rendered MP4 files |
| SQS standard queue | Job dispatch from API to Lambda |
| Lambda function | Runs the pipeline per job |
| DynamoDB table | Job status tracking (partition key: `job_id`) |

### Required IAM permissions (Lambda execution role)

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:PutObject",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:GetItem",
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes"
  ],
  "Resource": "*"
}
```

### Recommended Lambda configuration

| Setting | Recommended value |
|---|---|
| Memory | 3008 MB |
| Timeout | 15 minutes (maximum) |
| Ephemeral storage (`/tmp`) | 5120 MB (5GB) |
| Runtime | Python 3.11 |
| Trigger | SQS event source mapping |

---

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `python-multipart` | Multipart file upload parsing |
| `python-dotenv` | `.env` file loading |
| `aiofiles` | Async static file serving |
| `numpy` | Array math for GCC-PHAT |
| `scipy` | Signal processing |
| `librosa` | Audio loading and resampling |
| `ffmpeg-python` | FFmpeg Python bindings |
| `boto3` | AWS SDK (S3, SQS, DynamoDB, Lambda) |

> FFmpeg must be installed separately — it is included automatically in the Docker image.
> Install locally on macOS: `brew install ffmpeg`
