#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
INPUT_BUCKET="${INPUT_BUCKET:-multicam-input}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:-multicam-output}"
DDB_TABLE="${DDB_TABLE:-multicam-jobs}"
PROJECTS_DDB_TABLE="${PROJECTS_DDB_TABLE:-multicam-projects}"
SQS_QUEUE="${SQS_QUEUE:-multicam-jobs}"
ECS_TASK_ROLE_NAME="${ECS_TASK_ROLE_NAME:-RazorVidEcsTaskRole}"

echo "Using region: $AWS_REGION"
echo "Account ID: $ACCOUNT_ID"

echo "[1/6] Creating S3 buckets if needed"
aws s3api create-bucket \
  --bucket "$INPUT_BUCKET" \
  --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION" 2>/dev/null || true

aws s3api create-bucket \
  --bucket "$OUTPUT_BUCKET" \
  --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION" 2>/dev/null || true

echo "[2/6] Enabling block public access for buckets"
aws s3api put-public-access-block \
  --bucket "$INPUT_BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" 2>/dev/null || true

aws s3api put-public-access-block \
  --bucket "$OUTPUT_BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" 2>/dev/null || true

echo "[3/7] Creating DynamoDB tables if needed"
aws dynamodb create-table \
  --table-name "$DDB_TABLE" \
  --attribute-definitions AttributeName=job_id,AttributeType=S \
  --key-schema AttributeName=job_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$AWS_REGION" 2>/dev/null || true

aws dynamodb create-table \
  --table-name "$PROJECTS_DDB_TABLE" \
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region "$AWS_REGION" 2>/dev/null || true

echo "[4/7] Creating SQS queue if needed"
aws sqs create-queue \
  --queue-name "$SQS_QUEUE" \
  --region "$AWS_REGION" 2>/dev/null || true

echo "[5/7] Creating IAM role for ECS tasks"
ROLE_ARN=$(aws iam get-role --role-name "$ECS_TASK_ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || true)
if [ -z "$ROLE_ARN" ]; then
  ROLE_ARN=$(aws iam create-role \
    --role-name "$ECS_TASK_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --query 'Role.Arn' --output text)
fi

cat > /tmp/ecs-task-role-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::$INPUT_BUCKET",
        "arn:aws:s3:::$INPUT_BUCKET/*",
        "arn:aws:s3:::$OUTPUT_BUCKET",
        "arn:aws:s3:::$OUTPUT_BUCKET/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:Scan",
        "dynamodb:UpdateItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:$AWS_REGION:$ACCOUNT_ID:table/$DDB_TABLE",
        "arn:aws:dynamodb:$AWS_REGION:$ACCOUNT_ID:table/$PROJECTS_DDB_TABLE"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:$AWS_REGION:$ACCOUNT_ID:$SQS_QUEUE"
    },
    {
      "Effect": "Allow",
      "Action": [
        "rekognition:DetectLabels",
        "rekognition:DetectFaces"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name "$ECS_TASK_ROLE_NAME" \
  --policy-name RazorVidRuntimeAccess \
  --policy-document file:///tmp/ecs-task-role-policy.json

echo "[6/7] Waiting for projects table to become ACTIVE"
aws dynamodb wait table-exists --table-name "$PROJECTS_DDB_TABLE" --region "$AWS_REGION"

echo "[7/7] Done"
echo ""
echo "Use these values in the ECS task definition environment:"
echo "ENV=aws"
echo "AWS_REGION=$AWS_REGION"
echo "S3_INPUT_BUCKET=$INPUT_BUCKET"
echo "S3_OUTPUT_BUCKET=$OUTPUT_BUCKET"
echo "DYNAMODB_TABLE=$DDB_TABLE"
echo "PROJECTS_DYNAMODB_TABLE=$PROJECTS_DDB_TABLE"
echo "SQS_QUEUE_URL=$(aws sqs get-queue-url --queue-name "$SQS_QUEUE" --region "$AWS_REGION" --query 'QueueUrl' --output text)"
echo "ALLOWED_ORIGINS=https://razorvid.com,https://www.razorvid.com"
echo "AUTH_REQUIRED=false"
