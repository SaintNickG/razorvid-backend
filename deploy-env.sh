#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF'
RazorVid deployment environment template
=======================================

Set these values in your App Runner service runtime environment variables:

ENV=aws
AWS_REGION=us-east-1
S3_INPUT_BUCKET=multicam-input
S3_OUTPUT_BUCKET=multicam-output
DYNAMODB_TABLE=multicam-jobs
PROJECTS_DYNAMODB_TABLE=multicam-projects
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/multicam-jobs
ALLOWED_ORIGINS=https://razorvid.com,https://www.razorvid.com
AUTH_REQUIRED=false

For the frontend, set:
NEXT_PUBLIC_API_URL=https://api.your-domain.com
NEXT_PUBLIC_APP_URL=https://razorvid.com
EOF
