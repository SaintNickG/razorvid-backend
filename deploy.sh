#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Build, push to ECR, and deploy to ECS/Fargate
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Docker running
#   - jq installed (brew install jq)
#   - ECS/ALB variables documented in ecs-deploy.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — edit these once
# ---------------------------------------------------------------------------
AWS_ACCOUNT_ID="058264124581"
AWS_REGION="us-east-1"
ECR_REPO="razorvid-api"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
PROJECTS_DYNAMODB_TABLE="multicam-projects"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Creating ECR repo (skips if exists)..."
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" > /dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"

echo "==> Building Docker image..."
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --output=type=image,push=true,oci-mediatypes=false \
  -t "${ECR_URI}:${IMAGE_TAG}" .

echo "==> Image pushed: ${ECR_URI}:${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# ECS/Fargate and ALB
# ---------------------------------------------------------------------------
exec "$(dirname "$0")/ecs-deploy.sh"
