#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Build, push to ECR, and deploy to App Runner
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Docker running
#   - jq installed (brew install jq)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — edit these once
# ---------------------------------------------------------------------------
AWS_ACCOUNT_ID="058264124581"
AWS_REGION="us-east-1"
ECR_REPO="razorvid-api"
APP_RUNNER_SERVICE="razorvid-api"
IMAGE_TAG="latest"
PROJECTS_DYNAMODB_TABLE="multicam-projects"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Creating ECR repo (skips if exists)..."
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" > /dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"

echo "==> Building Docker image..."
docker build --platform linux/amd64 -t "${ECR_REPO}:${IMAGE_TAG}" .

echo "==> Tagging and pushing to ECR..."
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"

echo "==> Image pushed: ${ECR_URI}:${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# App Runner — create on first deploy, update image URI on subsequent deploys
# ---------------------------------------------------------------------------
SERVICE_ARN=$(aws apprunner list-services --region "$AWS_REGION" \
  | jq -r ".ServiceSummaryList[] | select(.ServiceName==\"${APP_RUNNER_SERVICE}\") | .ServiceArn" 2>/dev/null || true)

if [ -z "$SERVICE_ARN" ]; then
  echo "==> Creating App Runner service (first deploy)..."

  # Create the ECR access role if it doesn't exist
  ROLE_ARN=$(aws iam get-role --role-name AppRunnerECRAccessRole \
    --query "Role.Arn" --output text 2>/dev/null || true)

  if [ -z "$ROLE_ARN" ]; then
    echo "==> Creating AppRunnerECRAccessRole..."
    ROLE_ARN=$(aws iam create-role \
      --role-name AppRunnerECRAccessRole \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"build.apprunner.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' --query "Role.Arn" --output text)

    aws iam attach-role-policy \
      --role-name AppRunnerECRAccessRole \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  fi

  aws apprunner create-service \
    --region "$AWS_REGION" \
    --service-name "$APP_RUNNER_SERVICE" \
    --source-configuration "{
      \"ImageRepository\": {
        \"ImageIdentifier\": \"${ECR_URI}:${IMAGE_TAG}\",
        \"ImageRepositoryType\": \"ECR\",
        \"ImageConfiguration\": {
          \"Port\": \"8000\",
          \"RuntimeEnvironmentVariables\": {
            \"ENV\": \"aws\",
            \"AWS_REGION\": \"${AWS_REGION}\",
            \"DYNAMODB_TABLE\": \"multicam-jobs\",
            \"PROJECTS_DYNAMODB_TABLE\": \"${PROJECTS_DYNAMODB_TABLE}\",
            \"S3_INPUT_BUCKET\": \"multicam-input\",
            \"S3_OUTPUT_BUCKET\": \"multicam-output\",
            \"ALLOWED_ORIGINS\": \"https://razorvid.com,https://www.razorvid.com\",
            \"AUTH_REQUIRED\": \"false\"
          }
        }
      },
      \"AuthenticationConfiguration\": {
        \"AccessRoleArn\": \"${ROLE_ARN}\"
      },
      \"AutoDeploymentsEnabled\": false
    }" \
    --instance-configuration '{"Cpu":"1 vCPU","Memory":"2 GB"}' \
    --health-check-configuration '{"Protocol":"HTTP","Path":"/health","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":3}'

  echo "==> App Runner service created. Check status:"
  echo "    https://console.aws.amazon.com/apprunner/home?region=${AWS_REGION}"
else
  echo "==> Updating existing App Runner service: ${SERVICE_ARN}"
  aws apprunner update-service \
    --region "$AWS_REGION" \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "{
      \"ImageRepository\": {
        \"ImageIdentifier\": \"${ECR_URI}:${IMAGE_TAG}\",
        \"ImageRepositoryType\": \"ECR\",
        \"ImageConfiguration\": {
          \"Port\": \"8000\"
        }
      },
      \"AuthenticationConfiguration\": {}
    }"

  echo "==> Deployment triggered. App Runner will pull the new image."
fi

echo ""
echo "Done! Next steps:"
echo "  1. Wait ~2 min for App Runner to become healthy"
echo "  2. Copy the App Runner service URL from the console"
echo "  3. Add CNAME: api.razorvid.com -> <apprunner-url>"
echo "  4. Set NEXT_PUBLIC_API_URL=https://api.razorvid.com in Vercel"
