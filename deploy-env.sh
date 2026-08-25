#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF'
RazorVid deployment environment template
=======================================

Set these values in the ECS task definition environment variables:

ENV=aws
AWS_REGION=us-east-1
S3_INPUT_BUCKET=razorvid-input-prod-us-east-1--<account-id>-us-east-1-an
S3_OUTPUT_BUCKET=multicam-output
DYNAMODB_TABLE=multicam-jobs
PROJECTS_DYNAMODB_TABLE=multicam-projects
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/multicam-jobs
ALLOWED_ORIGINS=https://razorvid.com,https://www.razorvid.com
AUTH_REQUIRED=false

Deploy the SQS render worker after the API:
AWS_PROFILE=razorvid ./deploy-render-worker.sh

For the frontend, set:
NEXT_PUBLIC_API_URL=https://api.your-domain.com
NEXT_PUBLIC_APP_URL=https://razorvid.com
EOF
