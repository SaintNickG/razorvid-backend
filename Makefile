# =============================================================================
# Multicam Pipeline — Makefile
#
# Usage:
#   make          → same as make up (default)
#   make help     → list all available commands
# =============================================================================

.DEFAULT_GOAL := help
.PHONY: up compose build down restart logs shell clean wipe install install-dev dev web api lint test test-integration test-integration-precheck test-integration-hints test-integration-all qa-smoke qa-init-session help

PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# ---------------------------------------------------------------------------
# Dev — start both frontend and backend with one command
# ---------------------------------------------------------------------------

## Start API + Web locally (no Docker, fastest for dev)
dev:
	@echo "Starting API on :8000 and Web on :3000..."
	@trap 'kill 0' INT; \
		$(PYTHON) -m uvicorn --app-dir .. multicam_pipeline.main:app --reload --port 8000 & \
		cd ../razorvid-web && npm run dev & \
		wait

## Start only the FastAPI backend
api:
	$(PYTHON) -m uvicorn --app-dir .. multicam_pipeline.main:app --reload --port 8000

## Start only the Next.js frontend
web:
	cd ../razorvid-web && npm run dev

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

## Start API + Web in Docker (full stack)
compose:
	docker-compose up --build

## Same as compose (legacy alias)
up:
	docker-compose up --build

## Build the image without starting
build:
	docker-compose build --no-cache

## Stop containers (keeps volumes)
down:
	docker-compose down

## Restart the API container
restart:
	docker-compose restart api

## Stream live logs from the API container
logs:
	docker-compose logs -f api

## Open a shell inside the running API container
shell:
	docker exec -it multicam_api /bin/bash

## Stop containers and remove images
clean:
	docker-compose down --rmi local

## Stop containers, remove images AND wipe all upload/output volumes
wipe:
	docker-compose down -v --rmi local
	@echo "⚠️  All uploaded and rendered files have been deleted."

# ---------------------------------------------------------------------------
# Local dev (without Docker)
# ---------------------------------------------------------------------------

## Install Python dependencies locally
install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

## Install runtime + test dependencies locally
install-dev:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt

## Run linting (requires ruff: pip install ruff)
lint:
	ruff check multicam_pipeline/

## Run fast test suite
test:
	$(PYTHON) -m pytest -q tests/test_projects_persistence.py

## Validate integration-test prerequisites (ffmpeg/ffprobe + DSP libs)
test-integration-precheck:
	@missing=0; if ! command -v ffmpeg >/dev/null 2>&1; then echo "missing prerequisite: ffmpeg binary"; missing=1; fi; if ! command -v ffprobe >/dev/null 2>&1; then echo "missing prerequisite: ffprobe binary"; missing=1; fi; $(PYTHON) -c "import importlib.util, sys; req=['numpy','scipy','librosa']; miss=[m for m in req if importlib.util.find_spec(m) is None]; [print(f\"missing prerequisite: python module '{m}'\") for m in miss]; print('python prerequisites OK: numpy, scipy, librosa') if not miss else None; sys.exit(1 if miss else 0)" || missing=1; if [ $$missing -ne 0 ]; then echo "integration precheck failed"; exit 1; fi; echo "integration precheck passed"

## Print setup hints for integration-test prerequisites
test-integration-hints:
	@echo "Integration test setup hints"
	@echo "----------------------------"
	@echo "Detected Python: $(PYTHON)"
	@echo ""
	@echo "1) Python dependencies"
	@echo "   Run: make install-dev"
	@echo ""
	@echo "2) ffmpeg + ffprobe"
	@echo "   macOS (Homebrew): brew install ffmpeg"
	@echo "   Ubuntu/Debian:    sudo apt-get update && sudo apt-get install -y ffmpeg"
	@echo "   CI (GitHub):      add apt-get install -y ffmpeg in workflow"
	@echo ""
	@echo "3) Verify"
	@echo "   Run: make test-integration-precheck"
	@echo "   Then: make test-integration"

## Run media integration test (requires ffmpeg/ffprobe)
test-integration: test-integration-precheck
	$(PYTHON) -m pytest -q tests/test_pipeline_integration.py

## Run hints, precheck, then integration test (fail-fast)
test-integration-all: test-integration-hints test-integration-precheck test-integration

## Run backend smoke checks before manual QA
qa-smoke: test test-integration-all

## Initialize a timestamped QA report folder from templates
qa-init-session:
	@ts=$$(date +%Y%m%d-%H%M%S); \
	mkdir -p qa/reports/$$ts; \
	cp qa/templates/results_template.csv qa/reports/$$ts/results.csv; \
	cp qa/templates/session_notes_template.md qa/reports/$$ts/notes.md; \
	echo "Created QA report folder: qa/reports/$$ts"

# ---------------------------------------------------------------------------
# Help — lists all targets with their descriptions
# ---------------------------------------------------------------------------

## Show this help message
help:
	@echo ""
	@echo "Multicam Pipeline — available commands:"
	@echo ""
	@grep -E '^##' Makefile | sed 's/## //' | while IFS= read -r desc; do \
		target=$$(grep -E "^[a-zA-Z_-]+:" Makefile | grep -A1 "## $$desc" | head -1 | cut -d: -f1); \
		printf "  \033[36m%-15s\033[0m %s\n" "$$target" "$$desc"; \
	done
	@echo ""
