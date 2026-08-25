#!/usr/bin/env bash
# Create/update the ECS/Fargate service and ALB for an image already pushed to ECR.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO="${ECR_REPO:-razorvid-api}"
ECS_CLUSTER="${ECS_CLUSTER:-razorvid-api}"
ECS_SERVICE="${ECS_SERVICE:-razorvid-api}"
TASK_FAMILY="${TASK_FAMILY:-razorvid-api}"
CONTAINER_NAME="${CONTAINER_NAME:-razorvid-api}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
PROJECTS_DYNAMODB_TABLE="${PROJECTS_DYNAMODB_TABLE:-multicam-projects}"
FEEDBACK_DYNAMODB_TABLE="${FEEDBACK_DYNAMODB_TABLE:-multicam-feedback}"
DYNAMODB_TABLE="${DYNAMODB_TABLE:-multicam-jobs}"
INPUT_BUCKET="${INPUT_BUCKET:-multicam-input}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:-multicam-output}"
SQS_QUEUE="${SQS_QUEUE:-multicam-jobs}"
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-https://razorvid.com,https://www.razorvid.com}"
AUTH_REQUIRED="${AUTH_REQUIRED:-false}"
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
COGNITO_JWKS_URL="${COGNITO_JWKS_URL:-}"
AUTH_ALLOW_UNVERIFIED_TOKENS="${AUTH_ALLOW_UNVERIFIED_TOKENS:-false}"
ADMIN_USER_IDS="${ADMIN_USER_IDS:-}"
ADMIN_GROUPS="${ADMIN_GROUPS:-admin}"
BILLING_DYNAMODB_TABLE="${BILLING_DYNAMODB_TABLE:-}"
LOG_GROUP="${LOG_GROUP:-/ecs/razorvid-api}"
ALB_NAME="${ALB_NAME:-razorvid-api-alb}"
TARGET_GROUP_NAME="${TARGET_GROUP_NAME:-razorvid-api-tg}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-api.razorvid.com}"
FARGATE_CPU="${FARGATE_CPU:-2048}"
FARGATE_MEMORY="${FARGATE_MEMORY:-4096}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"

: "${VPC_ID:?Set VPC_ID to the VPC containing the ECS subnets}"
: "${PUBLIC_SUBNET_1:?Set PUBLIC_SUBNET_1 to a subnet in VPC_ID}"
: "${PUBLIC_SUBNET_2:?Set PUBLIC_SUBNET_2 to a second subnet in VPC_ID}"
: "${ALB_SECURITY_GROUP_ID:?Set ALB_SECURITY_GROUP_ID to the ALB security group}"
: "${TASK_SECURITY_GROUP_ID:?Set TASK_SECURITY_GROUP_ID to the ECS task security group}"
: "${CERTIFICATE_ARN:?Set CERTIFICATE_ARN to the ACM certificate for ${PUBLIC_HOSTNAME}}"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
EXECUTION_ROLE_NAME="${EXECUTION_ROLE_NAME:-RazorVidEcsTaskExecutionRole}"
TASK_ROLE_NAME="${TASK_ROLE_NAME:-RazorVidEcsTaskRole}"

CLUSTER_STATUS=$(aws ecs describe-clusters \
  --clusters "$ECS_CLUSTER" \
  --region "$AWS_REGION" \
  --query 'clusters[0].status' \
  --output text 2>/dev/null || true)
if [[ "$CLUSTER_STATUS" != "ACTIVE" ]]; then
  aws ecs create-cluster \
    --cluster-name "$ECS_CLUSTER" \
    --region "$AWS_REGION" \
    >/dev/null
fi
aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30 --region "$AWS_REGION"

aws dynamodb create-table \
  --table-name "$FEEDBACK_DYNAMODB_TABLE" \
  --attribute-definitions AttributeName=feedback_id,AttributeType=S \
  --key-schema AttributeName=feedback_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$AWS_REGION" 2>/dev/null || true
aws dynamodb wait table-exists --table-name "$FEEDBACK_DYNAMODB_TABLE" --region "$AWS_REGION"

TRUST_POLICY=$(mktemp)
TASK_POLICY=$(mktemp)
TASK_DEFINITION=$(mktemp)
trap 'rm -f "$TRUST_POLICY" "$TASK_POLICY" "$TASK_DEFINITION"' EXIT

cat > "$TRUST_POLICY" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

cat > "$TASK_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::${INPUT_BUCKET}","arn:aws:s3:::${INPUT_BUCKET}/*","arn:aws:s3:::${OUTPUT_BUCKET}","arn:aws:s3:::${OUTPUT_BUCKET}/*"]},
    {"Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:GetItem","dynamodb:UpdateItem","dynamodb:Scan","dynamodb:BatchWriteItem"],"Resource":["arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${DYNAMODB_TABLE}","arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${PROJECTS_DYNAMODB_TABLE}","arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${FEEDBACK_DYNAMODB_TABLE}"]},
    {"Effect":"Allow","Action":["sqs:SendMessage","sqs:GetQueueAttributes"],"Resource":"arn:aws:sqs:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SQS_QUEUE}"},
    {"Effect":"Allow","Action":["rekognition:DetectLabels","rekognition:DetectFaces"],"Resource":"*"}
  ]
}
EOF

if [ -n "$BILLING_DYNAMODB_TABLE" ]; then
  jq --arg table "arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${BILLING_DYNAMODB_TABLE}" \
    '.Statement += [{"Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:GetItem","dynamodb:UpdateItem","dynamodb:Scan"],"Resource":$table}]' \
    "$TASK_POLICY" > "${TASK_POLICY}.new"
  mv "${TASK_POLICY}.new" "$TASK_POLICY"
fi

ensure_role() {
  local role_name="$1"
  local policy_arn="$2"
  local role_arn
  role_arn=$(aws iam get-role --role-name "$role_name" --query 'Role.Arn' --output text 2>/dev/null || true)
  if [ -z "$role_arn" ] || [ "$role_arn" = "None" ]; then
    role_arn=$(aws iam create-role --role-name "$role_name" --assume-role-policy-document "file://${TRUST_POLICY}" --query 'Role.Arn' --output text)
  fi
  if [ -n "$policy_arn" ]; then
    aws iam attach-role-policy --role-name "$role_name" --policy-arn "$policy_arn"
  fi
  printf '%s' "$role_arn"
}

EXECUTION_ROLE_ARN=$(ensure_role "$EXECUTION_ROLE_NAME" arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy)
TASK_ROLE_ARN=$(ensure_role "$TASK_ROLE_NAME" "")
aws iam put-role-policy --role-name "$TASK_ROLE_NAME" --policy-name RazorVidRuntimeAccess --policy-document "file://${TASK_POLICY}"

SECRET_ARNS=$(jq -n --arg stripe "${STRIPE_SECRET_KEY_ARN:-}" --arg webhook "${STRIPE_WEBHOOK_SECRET_ARN:-}" --arg prices "${STRIPE_PRICE_IDS_JSON_ARN:-}" '[ $stripe, $webhook, $prices ] | map(select(length > 0))')
if [ "$SECRET_ARNS" != "[]" ]; then
  jq -n --argjson resources "$SECRET_ARNS" \
    '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:["secretsmanager:GetSecretValue"],Resource:$resources}]}' \
    > "${TASK_POLICY}.execution"
  aws iam put-role-policy --role-name "$EXECUTION_ROLE_NAME" --policy-name RazorVidSecretsAccess --policy-document "file://${TASK_POLICY}.execution"
fi

SQS_QUEUE_URL=$(aws sqs get-queue-url --queue-name "$SQS_QUEUE" --region "$AWS_REGION" --query QueueUrl --output text)

jq -n \
  --arg family "$TASK_FAMILY" --arg execution_role "$EXECUTION_ROLE_ARN" --arg task_role "$TASK_ROLE_ARN" \
  --arg image "${ECR_URI}:${IMAGE_TAG}" --arg region "$AWS_REGION" --arg log_group "$LOG_GROUP" \
  --arg container_name "$CONTAINER_NAME" --arg input_bucket "$INPUT_BUCKET" --arg output_bucket "$OUTPUT_BUCKET" \
  --arg jobs_table "$DYNAMODB_TABLE" --arg projects_table "$PROJECTS_DYNAMODB_TABLE" --arg feedback_table "$FEEDBACK_DYNAMODB_TABLE" --arg queue_url "$SQS_QUEUE_URL" \
  --arg origins "$ALLOWED_ORIGINS" --arg auth_required "$AUTH_REQUIRED" --arg issuer "$COGNITO_ISSUER" \
  --arg client_id "$COGNITO_CLIENT_ID" --arg jwks_url "$COGNITO_JWKS_URL" --arg allow_unverified "$AUTH_ALLOW_UNVERIFIED_TOKENS" \
  --arg admin_ids "$ADMIN_USER_IDS" --arg admin_groups "$ADMIN_GROUPS" --arg billing_table "$BILLING_DYNAMODB_TABLE" \
  --arg cpu "$FARGATE_CPU" --arg memory "$FARGATE_MEMORY" \
  '{family:$family,networkMode:"awsvpc",requiresCompatibilities:["FARGATE"],cpu:$cpu,memory:$memory,executionRoleArn:$execution_role,taskRoleArn:$task_role,containerDefinitions:[{name:$container_name,image:$image,essential:true,portMappings:[{containerPort:8000,hostPort:8000,protocol:"tcp"}],environment:[{name:"ENV",value:"aws"},{name:"AWS_REGION",value:$region},{name:"S3_INPUT_BUCKET",value:$input_bucket},{name:"S3_OUTPUT_BUCKET",value:$output_bucket},{name:"DYNAMODB_TABLE",value:$jobs_table},{name:"PROJECTS_DYNAMODB_TABLE",value:$projects_table},{name:"FEEDBACK_DYNAMODB_TABLE",value:$feedback_table},{name:"SQS_QUEUE_URL",value:$queue_url},{name:"ALLOWED_ORIGINS",value:$origins},{name:"AUTH_REQUIRED",value:$auth_required},{name:"COGNITO_ISSUER",value:$issuer},{name:"COGNITO_CLIENT_ID",value:$client_id},{name:"COGNITO_JWKS_URL",value:$jwks_url},{name:"AUTH_ALLOW_UNVERIFIED_TOKENS",value:$allow_unverified},{name:"ADMIN_USER_IDS",value:$admin_ids},{name:"ADMIN_GROUPS",value:$admin_groups},{name:"BILLING_DYNAMODB_TABLE",value:$billing_table}],secrets:[],logConfiguration:{logDriver:"awslogs",options:{"awslogs-group":$log_group,"awslogs-region":$region,"awslogs-stream-prefix":"api"}}}]}' > "$TASK_DEFINITION"

for secret_spec in \
  "STRIPE_SECRET_KEY:${STRIPE_SECRET_KEY_ARN:-}" \
  "STRIPE_WEBHOOK_SECRET:${STRIPE_WEBHOOK_SECRET_ARN:-}" \
  "STRIPE_PRICE_IDS_JSON:${STRIPE_PRICE_IDS_JSON_ARN:-}"; do
  secret_name="${secret_spec%%:*}"
  secret_arn="${secret_spec#*:}"
  if [ -n "$secret_arn" ]; then
    jq --arg name "$secret_name" --arg arn "$secret_arn" \
      '.containerDefinitions[0].secrets += [{name:$name,valueFrom:$arn}]' \
      "$TASK_DEFINITION" > "${TASK_DEFINITION}.new"
    mv "${TASK_DEFINITION}.new" "$TASK_DEFINITION"
  fi
done

TASK_DEFINITION_ARN=$(aws ecs register-task-definition --cli-input-json "file://${TASK_DEFINITION}" --region "$AWS_REGION" --query 'taskDefinition.taskDefinitionArn' --output text)

ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" --region "$AWS_REGION" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer --name "$ALB_NAME" --subnets "$PUBLIC_SUBNET_1" "$PUBLIC_SUBNET_2" --security-groups "$ALB_SECURITY_GROUP_ID" --scheme internet-facing --type application --region "$AWS_REGION" --query 'LoadBalancers[0].LoadBalancerArn' --output text)
fi

TARGET_GROUP_ARN=$(aws elbv2 describe-target-groups --names "$TARGET_GROUP_NAME" --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
if [ -z "$TARGET_GROUP_ARN" ] || [ "$TARGET_GROUP_ARN" = "None" ]; then
  TARGET_GROUP_ARN=$(aws elbv2 create-target-group --name "$TARGET_GROUP_NAME" --protocol HTTP --port 8000 --target-type ip --vpc-id "$VPC_ID" --health-check-protocol HTTP --health-check-path /health --matcher HttpCode=200 --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text)
fi
aws elbv2 modify-target-group --target-group-arn "$TARGET_GROUP_ARN" --health-check-protocol HTTP --health-check-path /health --matcher HttpCode=200 --health-check-interval-seconds 30 --health-check-timeout-seconds 5 --healthy-threshold-count 2 --unhealthy-threshold-count 3 --region "$AWS_REGION" >/dev/null

LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$AWS_REGION" --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text)
if [ -z "$LISTENER_ARN" ] || [ "$LISTENER_ARN" = "None" ]; then
  aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTPS --port 443 --certificates CertificateArn="$CERTIFICATE_ARN" --default-actions Type=forward,TargetGroupArn="$TARGET_GROUP_ARN" --region "$AWS_REGION" >/dev/null
else
  aws elbv2 modify-listener --listener-arn "$LISTENER_ARN" --default-actions Type=forward,TargetGroupArn="$TARGET_GROUP_ARN" --region "$AWS_REGION" >/dev/null
fi

SERVICE_ARN=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --region "$AWS_REGION" --query 'services[0].serviceArn' --output text 2>/dev/null || true)
if [ -z "$SERVICE_ARN" ] || [ "$SERVICE_ARN" = "None" ]; then
  aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" --task-definition "$TASK_DEFINITION_ARN" --desired-count "$DESIRED_COUNT" --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[$PUBLIC_SUBNET_1,$PUBLIC_SUBNET_2],securityGroups=[$TASK_SECURITY_GROUP_ID],assignPublicIp=ENABLED}" --load-balancers "targetGroupArn=$TARGET_GROUP_ARN,containerName=$CONTAINER_NAME,containerPort=8000" --health-check-grace-period-seconds 60 --region "$AWS_REGION" >/dev/null
else
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --task-definition "$TASK_DEFINITION_ARN" --desired-count "$DESIRED_COUNT" --force-new-deployment --region "$AWS_REGION" >/dev/null
fi

aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --region "$AWS_REGION"
printf '\nECS deployment complete.\nAPI: https://%s\nHealth: https://%s/health\n' "$PUBLIC_HOSTNAME" "$PUBLIC_HOSTNAME"
