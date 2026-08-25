"""
main.py
-------
FastAPI application entry point for the multicam pipeline backend.

Run locally:
    uvicorn multicam_pipeline.main:app --reload --port 8000

Pages:
    /          → Upload form (frontend)
    /docs      → Custom Swagger UI (API explorer)
    /redoc     → ReDoc API reference
    /health    → Liveness check

Environment:
    Copy .env.example to .env and set ENV=local for dev or ENV=aws for prod.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from multicam_pipeline.config import IS_LOCAL, LOCAL_UPLOAD_DIR, LOCAL_OUTPUT_DIR
from multicam_pipeline.billing_store import billing_backend_health
from multicam_pipeline.routers import (
    upload_router,
    jobs_router,
    download_router,
    projects_router,
    contributions_router,
    billing_router,
    admin_router,
    feedback_router,
)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if IS_LOCAL:
        os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
        os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
        print(f"[startup] Upload dir : {LOCAL_UPLOAD_DIR}")
        print(f"[startup] Output dir : {LOCAL_OUTPUT_DIR}")

    print(f"[startup] Multicam pipeline ready | env={'local' if IS_LOCAL else 'aws'}")
    print(f"[startup] Upload form  → http://localhost:8000/")
    print(f"[startup] Swagger UI   → http://localhost:8000/docs")
    print(f"[startup] ReDoc        → http://localhost:8000/redoc")
    yield
    print("[shutdown] Multicam pipeline shutting down.")


# ---------------------------------------------------------------------------
# App — docs_url/redoc_url set to None so we serve custom versions below
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Multicam Pipeline API",
    version="1.0.0",
    description="""
## Multicam Pipeline API

Upload multi-angle videos, synchronize them by audio track, and render a
single multicam timeline MP4 with automated cuts across angles.

### Workflow

1. **POST /upload** — Upload 2 or more video files (multipart/form-data).
   Returns `upload_id` and a list of file references.

2. **POST /render** — Submit a render job using the file references from step 1.
   Returns a `job_id` immediately (202 Accepted). Processing runs asynchronously.

3. **GET /status/{job_id}** — Poll job status: `PENDING → PROCESSING → COMPLETE / FAILED`.

4. **GET /download/{job_id}** — Stream or redirect to the finished MP4 once `COMPLETE`.

### Environments

| Mode | Dispatch | Status Store | Storage |
|------|----------|--------------|---------|
| `local` | asyncio background task | In-memory | Local disk |
| `aws` | SQS → Lambda | DynamoDB | S3 |

Set `ENV=local` or `ENV=aws` in your `.env` file.
    """,
    docs_url=None,    # disable default — we serve a custom version below
    redoc_url=None,   # disable default — we serve a custom version below
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173,https://razorvid.com,https://www.razorvid.com"
).split(",")
LOCAL_ORIGIN_REGEX = (
    r"^https?://(?:localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})(?::\d+)?$"
    if IS_LOCAL
    else None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static files — serves /static/index.html and any future assets
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(upload_router)    # POST /upload
app.include_router(jobs_router)      # POST /render  GET /status/{id}  GET /jobs
app.include_router(download_router)  # GET  /download/{job_id}
app.include_router(projects_router)  # POST /projects  GET /projects/{id}  etc.
app.include_router(contributions_router)  # GET /api/project/{project}/render/{render}/contributions
app.include_router(billing_router)  # Stripe checkout + webhook + billing status
app.include_router(admin_router)  # Failed-job observability and retry operations
app.include_router(feedback_router)  # Beta feedback submissions

# ---------------------------------------------------------------------------
# Frontend upload form — served at /
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_upload_form():
    """Serve the drag-and-drop upload form."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(), status_code=200)

# ---------------------------------------------------------------------------
# Custom Swagger UI — served at /docs
# ---------------------------------------------------------------------------

@app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def swagger_ui():
    """
    Custom Swagger UI with pre-filled examples and pipeline-specific styling.
    """
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="Multicam Pipeline — API Explorer",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        swagger_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
        swagger_ui_parameters={
            # Expand the first tag group by default so the workflow is obvious
            "docExpansion": "list",
            # Show request duration in the UI
            "displayRequestDuration": True,
            # Keep auth credentials between page refreshes
            "persistAuthorization": True,
            # Show schemas at the bottom expanded
            "defaultModelsExpandDepth": 2,
            # Pre-fill example values from the schema
            "tryItOutEnabled": True,
        },
    )

# ---------------------------------------------------------------------------
# Custom ReDoc — served at /redoc
# ---------------------------------------------------------------------------

@app.get("/redoc", response_class=HTMLResponse, include_in_schema=False)
async def redoc():
    """ReDoc — clean, readable API reference documentation."""
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Multicam Pipeline — API Reference",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js",
        redoc_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
        with_google_fonts=True,
    )

# ---------------------------------------------------------------------------
# Custom OpenAPI schema — adds examples to every endpoint
# ---------------------------------------------------------------------------

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Inject request body examples for POST /upload
    if "/upload" in schema.get("paths", {}):
        schema["paths"]["/upload"]["post"]["summary"] = "Upload video files"
        schema["paths"]["/upload"]["post"]["description"] = (
            "Upload 2 or more video files as `multipart/form-data`. "
            "Returns an `upload_id` and a list of file references to pass into `POST /render`."
        )

    # Inject request body examples for POST /render
    if "/render" in schema.get("paths", {}):
        schema["paths"]["/render"]["post"]["summary"] = "Submit a render job"
        schema["paths"]["/render"]["post"]["description"] = (
            "Submit a multicam render job using file references from `POST /upload`. "
            "Returns `202 Accepted` with a `job_id` immediately — processing is async."
        )
        schema["paths"]["/render"]["post"]["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/RenderRequest"},
                    "example": {
                        "files": [
                            "/tmp/multicam/uploads/abc-123_cam1.mp4",
                            "/tmp/multicam/uploads/abc-123_cam2.mp4",
                            "/tmp/multicam/uploads/abc-123_cam3.mp4",
                        ],
                        "upload_id": "abc-123",
                        "cut_interval": 5.0,
                        "target_width": 1920,
                        "target_height": 1080,
                        "target_fps": 30,
                        "effect_intensity": "balanced",
                        "audio_source_file": "/tmp/multicam/uploads/abc-123_cam2.mp4",
                    },
                }
            },
        }

    # Tag descriptions shown in Swagger UI sidebar
    schema["tags"] = [
        {
            "name": "upload",
            "description": "**Step 1** — Upload video files for processing.",
        },
        {
            "name": "jobs",
            "description": "**Step 2 & 3** — Submit render jobs and poll status.",
        },
        {
            "name": "download",
            "description": "**Step 4** — Download the finished multicam MP4.",
        },
        {
            "name": "health",
            "description": "Liveness check.",
        },
    ]

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
async def health(deep: int = Query(default=0, ge=0, le=1)):
    """
    Liveness check.

    Query params:
        deep: set to 1 to include backend dependency diagnostics.
    """
    payload = {"status": "ok", "env": "local" if IS_LOCAL else "aws", "deep": bool(deep)}
    if deep:
        payload["checks"] = {
            "billing_backend": billing_backend_health(),
        }
    return payload
