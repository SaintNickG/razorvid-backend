#!/usr/bin/env bash
# Build and deploy the Lambda worker that consumes multicam render jobs from SQS.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO="${ECR_REPO:-razorvid-render-worker}"
FUNCTION_NAME="${FUNCTION_NAME:-RazorVidRenderWorker}"
WORKER_ROLE_NAME="${WORKER_ROLE_NAME:-RazorVidRenderWorkerRole}"
SQS_QUEUE="${SQS_QUEUE:-multicam-jobs}"
DLQ_NAME="${DLQ_NAME:-${SQS_QUEUE}-dlq}"
INPUT_BUCKET="${INPUT_BUCKET:-razorvid-input-prod-us-east-1--${AWS_ACCOUNT_ID}-us-east-1-an}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:-multicam-output}"
INPUT_RETENTION_DAYS="${INPUT_RETENTION_DAYS:-30}"
OUTPUT_RETENTION_DAYS="${OUTPUT_RETENTION_DAYS:-14}"
DYNAMODB_TABLE="${DYNAMODB_TABLE:-multicam-jobs}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)-worker-$(date +%Y%m%d%H%M%S)}"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

QUEUE_URL=$(aws sqs get-queue-url --queue-name "$SQS_QUEUE" --region "$AWS_REGION" --query QueueUrl --output text)
QUEUE_ARN=$(aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)
DLQ_URL=$(aws sqs get-queue-url --queue-name "$DLQ_NAME" --region "$AWS_REGION" --query QueueUrl --output text 2>/dev/null || true)
if [[ -z "$DLQ_URL" || "$DLQ_URL" == "None" ]]; then
  DLQ_URL=$(aws sqs create-queue --queue-name "$DLQ_NAME" --region "$AWS_REGION" --query QueueUrl --output text)
fi
DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

aws s3api head-bucket --bucket "$OUTPUT_BUCKET" 2>/dev/null \
  || aws s3api create-bucket --bucket "$OUTPUT_BUCKET" --region "$AWS_REGION"
aws s3api put-public-access-block --bucket "$OUTPUT_BUCKET" \
  --public-access-block-configuration 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
INPUT_LIFECYCLE=$(mktemp)
OUTPUT_LIFECYCLE=$(mktemp)
jq -n --argjson days "$INPUT_RETENTION_DAYS" '{Rules:[{ID:"ExpireInputMedia",Status:"Enabled",Filter:{Prefix:"uploads/"},Expiration:{Days:$days}}]}' > "$INPUT_LIFECYCLE"
jq -n --argjson days "$OUTPUT_RETENTION_DAYS" '{Rules:[{ID:"ExpireRenderedMedia",Status:"Enabled",Filter:{Prefix:""},Expiration:{Days:$days}}]}' > "$OUTPUT_LIFECYCLE"
aws s3api put-bucket-lifecycle-configuration --bucket "$INPUT_BUCKET" \
  --lifecycle-configuration "file://${INPUT_LIFECYCLE}"
aws s3api put-bucket-lifecycle-configuration --bucket "$OUTPUT_BUCKET" \
  --lifecycle-configuration "file://${OUTPUT_LIFECYCLE}"
rm -f "$INPUT_LIFECYCLE" "$OUTPUT_LIFECYCLE"
aws sqs set-queue-attributes --queue-url "$QUEUE_URL" --attributes VisibilityTimeout=960
REDRIVE_POLICY=$(jq -cn --arg arn "$DLQ_ARN" '{deadLetterTargetArn:$arn,maxReceiveCount:"3"}')
REDRIVE_ATTRIBUTES=$(mktemp)
printf '%s' "$REDRIVE_POLICY" | jq '{RedrivePolicy: tojson}' > "$REDRIVE_ATTRIBUTES"
aws sqs set-queue-attributes --queue-url "$QUEUE_URL" \
  --attributes "file://${REDRIVE_ATTRIBUTES}"

TRUST_POLICY=$(mktemp)
ACCESS_POLICY=$(mktemp)
trap 'rm -f "$TRUST_POLICY" "$ACCESS_POLICY" "$REDRIVE_ATTRIBUTES"' EXIT

printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > "$TRUST_POLICY"
ROLE_ARN=$(aws iam get-role --role-name "$WORKER_ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || true)
if [[ -z "$ROLE_ARN" || "$ROLE_ARN" == "None" ]]; then
  ROLE_ARN=$(aws iam create-role --role-name "$WORKER_ROLE_NAME" --assume-role-policy-document "file://${TRUST_POLICY}" --query 'Role.Arn' --output text)
fi

aws iam attach-role-policy --role-name "$WORKER_ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
jq -n --arg input "$INPUT_BUCKET" --arg output "$OUTPUT_BUCKET" --arg queue "$QUEUE_ARN" --arg region "$AWS_REGION" --arg account "$AWS_ACCOUNT_ID" '
  {Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:["s3:GetObject","s3:ListBucket"],Resource:[("arn:aws:s3:::" + $input), ("arn:aws:s3:::" + $input + "/*")]},
    {Effect:"Allow",Action:["s3:PutObject","s3:GetObject","s3:ListBucket"],Resource:[("arn:aws:s3:::" + $output), ("arn:aws:s3:::" + $output + "/*")]},
    {Effect:"Allow",Action:["dynamodb:PutItem","dynamodb:GetItem","dynamodb:UpdateItem"],Resource:("arn:aws:dynamodb:" + $region + ":" + $account + ":table/multicam-jobs")},
    {Effect:"Allow",Action:["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes","sqs:ChangeMessageVisibility"],Resource:$queue}
  ]}' > "$ACCESS_POLICY"
aws iam put-role-policy --role-name "$WORKER_ROLE_NAME" --policy-name RazorVidRenderWorkerAccess --policy-document "file://${ACCESS_POLICY}"

aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build --platform linux/amd64 --provenance=false -f Dockerfile.worker -t "${ECR_URI}:${IMAGE_TAG}" .
docker push "${ECR_URI}:${IMAGE_TAG}"

ENVIRONMENT="Variables={ENV=aws,NUMBA_CACHE_DIR=/tmp/numba-cache,DYNAMODB_TABLE=${DYNAMODB_TABLE},S3_INPUT_BUCKET=${INPUT_BUCKET},S3_OUTPUT_BUCKET=${OUTPUT_BUCKET}}"
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --image-uri "${ECR_URI}:${IMAGE_TAG}" --region "$AWS_REGION" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --timeout 900 --memory-size 4096 --ephemeral-storage Size=10240 --environment "$ENVIRONMENT" --region "$AWS_REGION" >/dev/null
else
  aws lambda create-function --function-name "$FUNCTION_NAME" --package-type Image --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" --role "$ROLE_ARN" --timeout 900 --memory-size 4096 --ephemeral-storage Size=10240 --environment "$ENVIRONMENT" --region "$AWS_REGION" >/dev/null
fi
aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME" --region "$AWS_REGION"

MAPPING_ID=$(aws lambda list-event-source-mappings --event-source-arn "$QUEUE_ARN" --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --query 'EventSourceMappings[0].UUID' --output text)
if [[ -z "$MAPPING_ID" || "$MAPPING_ID" == "None" ]]; then
  aws lambda create-event-source-mapping --event-source-arn "$QUEUE_ARN" --function-name "$FUNCTION_NAME" --batch-size 1 --enabled --region "$AWS_REGION" >/dev/null
fi

aws cloudwatch put-metric-alarm \
  --alarm-name RazorVidRenderWorkerErrors \
  --alarm-description "Lambda render worker reported an error" \
  --namespace AWS/Lambda --metric-name Errors --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=FunctionName,Value=${FUNCTION_NAME}" \
  --treat-missing-data notBreaching --region "$AWS_REGION"
aws cloudwatch put-metric-alarm \
  --alarm-name RazorVidRenderQueueBacklog \
  --alarm-description "Render jobs have been waiting in SQS for at least 15 minutes" \
  --namespace AWS/SQS --metric-name ApproximateAgeOfOldestMessage --statistic Maximum --period 300 --evaluation-periods 3 \
  --threshold 900 --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=QueueName,Value=${SQS_QUEUE}" \
  --treat-missing-data notBreaching --region "$AWS_REGION"
aws cloudwatch put-metric-alarm \
  --alarm-name RazorVidRenderDeadLetterQueue \
  --alarm-description "A render job exhausted retries and moved to the dead-letter queue" \
  --namespace AWS/SQS --metric-name ApproximateNumberOfMessagesVisible --statistic Maximum --period 60 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions "Name=QueueName,Value=${DLQ_NAME}" \
  --treat-missing-data notBreaching --region "$AWS_REGION"

printf 'Render worker deployed: %s\n' "$FUNCTION_NAME"