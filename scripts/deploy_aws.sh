#!/usr/bin/env bash
set -euo pipefail

REGION="eu-west-1"
PROFILE="playground"
ACCOUNT_ID="688567279867"
REPO="polymarket-bot"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
IMAGE_TAG="latest"
ECS_CLUSTER="polymarket-bot"
ECS_SERVICE="polymarket-bot-service"
ROOT_DIR="$(dirname "$(dirname "$0")")"
TASK_DEF_FILE="${ROOT_DIR}/aws/task-definition.json"

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" --profile "${PROFILE}" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Building image for linux/amd64..."
docker build --platform linux/amd64 \
  -t "${REPO}:${IMAGE_TAG}" \
  "${ROOT_DIR}"

echo "==> Tagging image..."
docker tag "${REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"

echo "==> Pushing image to ECR..."
docker push "${ECR_URI}:${IMAGE_TAG}"

echo "==> Registering ECS task definition revision..."
TASK_DEF_ARN=$(aws ecs register-task-definition \
  --cli-input-json "file://${TASK_DEF_FILE}" \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)
echo "==> Registered ${TASK_DEF_ARN}"

echo "==> Forcing new ECS deployment..."
aws ecs update-service \
  --cluster "${ECS_CLUSTER}" \
  --service "${ECS_SERVICE}" \
  --task-definition "${TASK_DEF_ARN}" \
  --force-new-deployment \
  --region "${REGION}" \
  --profile "${PROFILE}"

echo "==> Deployment triggered successfully."
