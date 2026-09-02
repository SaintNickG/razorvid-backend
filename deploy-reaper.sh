#!/usr/bin/env bash
# Deploy the stuck-job reaper Lambda + EventBridge schedule + OOM CloudWatch alarm.
#
# The reaper shares the same container image as the render worker (already in ECR).
# It uses a separate Lambda function with a lightweight handler (reaper.lambda_handler)
# and is triggered every 5 minutes by EventBridge.
#
# Usage:
#   AWS_PROFILE=iamsaintnick bash deploy-reaper.sh
#
# Optional overrides (all have sensible defaults):
#   REAPER_FUNCTION_NAME, WORKER_FUNCTION_NAME, ECR_REPO, DYNAMODB_TABLE,
#   STUCK_THRESHOLD_MINUTES, ALERT_EMAIL, AWS_REGION, AWS_ACCOUNT_ID
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-iamsaintnick}"
export AWS_PROFILE

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO="${ECR_REPO:-razorvid-render-worker}"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

REAPER_FUNCTION_NAME="${REAPER_FUNCTION_NAME:-RazorVidStuckJobReaper}"
WORKER_FUNCTION_NAME="${WORKER_FUNCTION_NAME:-RazorVidRenderWorker}"
REAPER_ROLE_NAME="${REAPER_ROLE_NAME:-RazorVidStuckJobReaperRole}"
DYNAMODB_TABLE="${DYNAMODB_TABLE:-multicam-jobs}"
STUCK_THRESHOLD_MINUTES="${STUCK_THRESHOLD_MINUTES:-20}"

# Optional: set ALERT_EMAIL to create an SNS subscription for OOM alerts.
ALERT_EMAIL="${ALERT_EMAIL:-}"

# ---------------------------------------------------------------------------
# Resolve the image URI currently deployed to the render worker
# ---------------------------------------------------------------------------
IMAGE_URI=$(aws lambda get-function \
  --function-name "$WORKER_FUNCTION_NAME" \
  --region "$AWS_REGION" \
  --query 'Code.ImageUri' \
  --output text)

echo "Using render worker image: $IMAGE_URI"

# ---------------------------------------------------------------------------
# IAM role for the reaper (DynamoDB scan + update + CloudWatch Logs)
# ---------------------------------------------------------------------------
TRUST_POLICY=$(mktemp)
ACCESS_POLICY=$(mktemp)
trap 'rm -f "$TRUST_POLICY" "$ACCESS_POLICY"' EXIT

printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  > "$TRUST_POLICY"

ROLE_ARN=$(aws iam get-role --role-name "$REAPER_ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null || true)
if [[ -z "$ROLE_ARN" || "$ROLE_ARN" == "None" ]]; then
  ROLE_ARN=$(aws iam create-role \
    --role-name "$REAPER_ROLE_NAME" \
    --assume-role-policy-document "file://${TRUST_POLICY}" \
    --query 'Role.Arn' --output text)
  echo "Created IAM role: $ROLE_ARN"
fi

aws iam attach-role-policy \
  --role-name "$REAPER_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

jq -n \
  --arg region "$AWS_REGION" \
  --arg account "$AWS_ACCOUNT_ID" \
  --arg table "$DYNAMODB_TABLE" \
  '{Version:"2012-10-17",Statement:[{
    Effect:"Allow",
    Action:["dynamodb:Scan","dynamodb:UpdateItem"],
    Resource:("arn:aws:dynamodb:" + $region + ":" + $account + ":table/" + $table)
  }]}' > "$ACCESS_POLICY"

aws iam put-role-policy \
  --role-name "$REAPER_ROLE_NAME" \
  --policy-name RazorVidReaperAccess \
  --policy-document "file://${ACCESS_POLICY}"

# ---------------------------------------------------------------------------
# Create or update the reaper Lambda function
# ---------------------------------------------------------------------------
ENVIRONMENT="Variables={ENV=aws,DYNAMODB_TABLE=${DYNAMODB_TABLE},STUCK_THRESHOLD_MINUTES=${STUCK_THRESHOLD_MINUTES}}"

if aws lambda get-function --function-name "$REAPER_FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws lambda update-function-code \
    --function-name "$REAPER_FUNCTION_NAME" \
    --image-uri "$IMAGE_URI" \
    --region "$AWS_REGION" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$REAPER_FUNCTION_NAME" --region "$AWS_REGION"
  aws lambda update-function-configuration \
    --function-name "$REAPER_FUNCTION_NAME" \
    --timeout 60 \
    --memory-size 256 \
    --environment "$ENVIRONMENT" \
    --region "$AWS_REGION" >/dev/null
  echo "Updated reaper function: $REAPER_FUNCTION_NAME"
else
  # Brief sleep so the new role is consistent before Lambda uses it
  sleep 10
  aws lambda create-function \
    --function-name "$REAPER_FUNCTION_NAME" \
    --package-type Image \
    --code "ImageUri=${IMAGE_URI}" \
    --role "$ROLE_ARN" \
    --timeout 60 \
    --memory-size 256 \
    --environment "$ENVIRONMENT" \
    --region "$AWS_REGION" >/dev/null
  echo "Created reaper function: $REAPER_FUNCTION_NAME"
fi

aws lambda wait function-active-v2 --function-name "$REAPER_FUNCTION_NAME" --region "$AWS_REGION"

# Override the CMD to point at the reaper handler (image default is aws_handler.lambda_handler)
aws lambda update-function-configuration \
  --function-name "$REAPER_FUNCTION_NAME" \
  --image-config '{"Command":["multicam_pipeline.reaper.lambda_handler"]}' \
  --region "$AWS_REGION" >/dev/null
aws lambda wait function-updated-v2 --function-name "$REAPER_FUNCTION_NAME" --region "$AWS_REGION"

# ---------------------------------------------------------------------------
# EventBridge rule — run every 5 minutes
# ---------------------------------------------------------------------------
RULE_ARN=$(aws events put-rule \
  --name RazorVidStuckJobReaperSchedule \
  --schedule-expression "rate(5 minutes)" \
  --state ENABLED \
  --region "$AWS_REGION" \
  --query RuleArn --output text)

REAPER_ARN=$(aws lambda get-function \
  --function-name "$REAPER_FUNCTION_NAME" \
  --region "$AWS_REGION" \
  --query 'Configuration.FunctionArn' \
  --output text)

# Grant EventBridge permission to invoke the reaper (idempotent)
aws lambda remove-permission \
  --function-name "$REAPER_FUNCTION_NAME" \
  --statement-id EventBridgeReaperInvoke \
  --region "$AWS_REGION" 2>/dev/null || true

aws lambda add-permission \
  --function-name "$REAPER_FUNCTION_NAME" \
  --statement-id EventBridgeReaperInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "$RULE_ARN" \
  --region "$AWS_REGION" >/dev/null

aws events put-targets \
  --rule RazorVidStuckJobReaperSchedule \
  --targets "Id=ReaperTarget,Arn=${REAPER_ARN}" \
  --region "$AWS_REGION" >/dev/null

echo "EventBridge schedule set: every 5 minutes → $REAPER_FUNCTION_NAME"

# ---------------------------------------------------------------------------
# SNS topic for OOM / reaper alerts
# ---------------------------------------------------------------------------
TOPIC_ARN=$(aws sns create-topic \
  --name RazorVidRenderAlerts \
  --region "$AWS_REGION" \
  --query TopicArn --output text)

if [[ -n "$ALERT_EMAIL" ]]; then
  aws sns subscribe \
    --topic-arn "$TOPIC_ARN" \
    --protocol email \
    --notification-endpoint "$ALERT_EMAIL" \
    --region "$AWS_REGION" >/dev/null
  echo "SNS subscription pending confirmation for: $ALERT_EMAIL"
fi

# ---------------------------------------------------------------------------
# CloudWatch alarm — OOM log filter on the render worker log group
# ---------------------------------------------------------------------------
LOG_GROUP="/aws/lambda/${WORKER_FUNCTION_NAME}"

# Metric filter to count OOM occurrences in Lambda logs
aws logs put-metric-filter \
  --log-group-name "$LOG_GROUP" \
  --filter-name RazorVidRenderWorkerOOM \
  --filter-pattern "Runtime.OutOfMemory" \
  --metric-transformations \
    metricName=RenderWorkerOOM,metricNamespace=RazorVid,metricValue=1,defaultValue=0 \
  --region "$AWS_REGION"

aws cloudwatch put-metric-alarm \
  --alarm-name RazorVidRenderWorkerOOM \
  --alarm-description "Lambda render worker hit Runtime.OutOfMemory — job left stuck in PROCESSING" \
  --namespace RazorVid \
  --metric-name RenderWorkerOOM \
  --statistic Sum \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" \
  --region "$AWS_REGION"

# Alarm for reaper finding stuck jobs (means something slipped through)
aws cloudwatch put-metric-alarm \
  --alarm-name RazorVidStuckJobsRecovered \
  --alarm-description "Reaper recovered stuck PROCESSING jobs — investigate Lambda hard-kills" \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --statistic Sum \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=FunctionName,Value=${REAPER_FUNCTION_NAME}" \
  --treat-missing-data notBreaching \
  --region "$AWS_REGION"

echo ""
echo "Reaper deployed successfully."
echo "  Function : $REAPER_FUNCTION_NAME"
echo "  Schedule : every 5 minutes"
echo "  Threshold: ${STUCK_THRESHOLD_MINUTES} minutes"
echo "  OOM alarm: RazorVidRenderWorkerOOM → SNS $TOPIC_ARN"
echo ""
echo "To receive email alerts, re-run with ALERT_EMAIL=you@example.com"
