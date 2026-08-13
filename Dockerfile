# =============================================================================
# Stage 1 — builder
# Installs Python dependencies into an isolated layer so the final image
# only copies the compiled packages, keeping the image lean.
# =============================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed to compile numpy/scipy/librosa C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies into a prefix directory
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# =============================================================================
# Stage 2 — runtime
# Minimal image with FFmpeg + compiled Python packages from the builder stage.
# =============================================================================
FROM python:3.11-slim AS runtime

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
# ffmpeg        — video processing engine (required by the pipeline)
# libsndfile1   — audio file I/O (required by librosa)
# curl          — used by the Docker healthcheck below
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Copy compiled Python packages from builder
# ---------------------------------------------------------------------------
COPY --from=builder /install /usr/local

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
WORKDIR /app

# Copy the full package into the container
COPY . /app/multicam_pipeline

# Create upload and output directories that match the default config paths
RUN mkdir -p /tmp/multicam/uploads /tmp/multicam/output

# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=local \
    LOCAL_UPLOAD_DIR=/tmp/multicam/uploads \
    LOCAL_OUTPUT_DIR=/tmp/multicam/output

# Expose the FastAPI port
EXPOSE 8000

# ---------------------------------------------------------------------------
# Healthcheck
# Polls /health every 30s. Container is marked unhealthy if it fails 3 times.
# The Docker extension in VS Code will show green/red status based on this.
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# --workers 1     keeps job state consistent in local dev (in-memory store)
# --reload        auto-reloads on code changes when volume-mounted
# Change --workers to match CPU count in production (remove --reload)
# Development override: docker run ... uvicorn multicam_pipeline.main:app --reload
CMD ["uvicorn", "multicam_pipeline.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4"]
