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

echo "==> Forcing new ECS deployment with desired-count=1..."
aws ecs update-service \
  --cluster "${ECS_CLUSTER}" \
  --service "${ECS_SERVICE}" \
  --task-definition "${TASK_DEF_ARN}" \
  --desired-count 1 \
  --force-new-deployment \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --query 'service.serviceName' \
  --output text

echo "==> Waiting for rollout to complete (max 5 minutes)..."
TIMEOUT=300
INTERVAL=15
ELAPSED=0
OLD_RUNNING=""

while [ "${ELAPSED}" -lt "${TIMEOUT}" ]; do
  sleep "${INTERVAL}"
  ELAPSED=$((ELAPSED + INTERVAL))

  STATUS=$(aws ecs describe-services \
    --cluster "${ECS_CLUSTER}" \
    --services "${ECS_SERVICE}" \
    --region "${REGION}" \
    --profile "${PROFILE}" \
    --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,taskDef:taskDefinition}' \
    --output json 2>/dev/null || echo '{}')

  DESIRED=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('desired',0))" 2>/dev/null || echo 0)
  RUNNING=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('running',0))" 2>/dev/null || echo 0)
  PENDING=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('pending',0))" 2>/dev/null || echo 0)
  TASK_DEF=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('taskDef','?'))" 2>/dev/null || echo "?")

  echo "    [${ELAPSED}s] desired=${DESIRED} running=${RUNNING} pending=${PENDING} taskDef=${TASK_DEF##*:}"

  if [ "${RUNNING}" -eq 1 ] && [ "${PENDING}" -eq 0 ]; then
    echo "==> Rollout complete: 1 task running on ${TASK_DEF##*:}"
    break
  fi

  # Safety: if we see 2 running tasks, warn about overlap
  if [ "${RUNNING}" -gt 1 ]; then
    echo "    ⚠ WARNING: ${RUNNING} tasks running — ECS rollover overlap"
  fi
done

if [ "${ELAPSED}" -ge "${TIMEOUT}" ]; then
  echo "==> ⚠ Rollout did not complete within ${TIMEOUT}s — check ECS manually"
fi

# Verify the running task is on the new task definition
echo ""
echo "==> Verifying deployment health..."
sleep 5

FINAL_STATUS=$(aws ecs describe-services \
  --cluster "${ECS_CLUSTER}" \
  --services "${ECS_SERVICE}" \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,taskDef:taskDefinition}' \
  --output json 2>/dev/null || echo '{}')

FINAL_DESIRED=$(echo "${FINAL_STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('desired',0))" 2>/dev/null || echo 0)
FINAL_RUNNING=$(echo "${FINAL_STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('running',0))" 2>/dev/null || echo 0)
FINAL_TASKDEF=$(echo "${FINAL_STATUS}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('taskDef','?'))" 2>/dev/null || echo "?")

echo ""
echo "╭────────────────────────────────────────────╮"
echo "│  Deploy Summary                            │"
echo "├────────────────────────────────────────────┤"
printf "│  Task definition: %-23s │\n" "${TASK_DEF_ARN##*:}"
printf "│  Desired: %-31s │\n" "${FINAL_DESIRED}"
printf "│  Running: %-31s │\n" "${FINAL_RUNNING}"

if [ "${FINAL_RUNNING}" -eq 1 ] && [ "${FINAL_DESIRED}" -eq 1 ]; then
  echo "│  Status:  ✅ HEALTHY                       │"
else
  echo "│  Status:  ❌ CHECK MANUALLY                 │"
fi

echo "╰────────────────────────────────────────────╯"

# Check for startup logs from the new task
TASK_ARN=$(aws ecs list-tasks \
  --cluster "${ECS_CLUSTER}" \
  --service-name "${ECS_SERVICE}" \
  --desired-status RUNNING \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --query 'taskArns[0]' \
  --output text 2>/dev/null || echo "None")

if [ "${TASK_ARN}" != "None" ] && [ -n "${TASK_ARN}" ]; then
  TASK_ID="${TASK_ARN##*/}"
  echo ""
  echo "==> Checking startup logs for task ${TASK_ID:0:12}..."
  sleep 10

  # Look for pairs_enabled or loop_starting in logs
  STARTUP=$(aws logs filter-log-events \
    --log-group-name /polymarket-bot \
    --log-stream-names "polybot/polymarket-bot/${TASK_ID}" \
    --filter-pattern "pairs_enabled" \
    --limit 1 \
    --region "${REGION}" \
    --profile "${PROFILE}" \
    --query 'events[0].message' \
    --output text 2>/dev/null || echo "None")

  if [ "${STARTUP}" != "None" ] && [ -n "${STARTUP}" ]; then
    echo "==> ✅ Bot loop started. Pairs:"
    echo "${STARTUP}" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read().strip())
    pairs = d.get('pairs', [])
    for p in pairs:
        print(f'    - {p}')
except:
    print('    (could not parse)')
" 2>/dev/null || echo "    (could not parse)"
  else
    echo "==> ⏳ No startup log yet — bot may still be initializing"
  fi
fi

echo ""
echo "==> Deploy complete."
