#!/usr/bin/env bash
# Usage: ./scripts/switch.sh paper|live
#
# Switches the bot between paper and live trading:
# 1. Updates MODE in Secrets Manager
# 2. Stops the running ECS task
# 3. Launches a new task with the updated config
set -euo pipefail

PROFILE="playground"
REGION="eu-west-1"
CLUSTER="polymarket-bot"
SECRET_ID="polymarket-bot-env"
SUBNET="subnet-0ad6ae3ee1277d9f2"
SG="sg-035f8d47dd34cad66"

MODE="${1:-}"
if [[ "$MODE" != "paper" && "$MODE" != "live" ]]; then
    echo "Usage: $0 paper|live"
    exit 1
fi

echo "==> Switching to $MODE mode..."

# 1. Update Secrets Manager
echo "  Updating Secrets Manager..."
CURRENT=$(aws --profile "$PROFILE" secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" --region "$REGION" \
    --query SecretString --output text)

UPDATED=$(echo "$CURRENT" | python3 -c "
import sys, json
val = json.loads(sys.stdin.read())
val['MODE'] = '$MODE'
if '$MODE' == 'paper':
    val['BANKROLL'] = '1000.0'
else:
    val['BANKROLL'] = '43.0'
print(json.dumps(val))
")

aws --profile "$PROFILE" secretsmanager put-secret-value \
    --secret-id "$SECRET_ID" \
    --secret-string "$UPDATED" \
    --region "$REGION" > /dev/null
echo "  MODE=$MODE in Secrets Manager"

# 2. Find and stop running task
echo "  Stopping current task..."
TASK_ARN=$(aws --profile "$PROFILE" ecs list-tasks \
    --cluster "$CLUSTER" --region "$REGION" \
    --query 'taskArns[0]' --output text 2>/dev/null || echo "None")

if [[ "$TASK_ARN" != "None" && "$TASK_ARN" != "" ]]; then
    aws --profile "$PROFILE" ecs stop-task \
        --cluster "$CLUSTER" --task "$TASK_ARN" \
        --reason "switching to $MODE" --region "$REGION" > /dev/null
    echo "  Stopped: ${TASK_ARN##*/}"
    echo "  Waiting for task to drain..."
    sleep 5
else
    echo "  No running task found"
fi

# 3. Get latest task definition
LATEST_TD=$(aws --profile "$PROFILE" ecs list-task-definitions \
    --family-prefix polymarket-bot --sort DESC --max-items 1 \
    --region "$REGION" --query 'taskDefinitionArns[0]' --output text)
TD_REV="${LATEST_TD##*:}"
echo "  Using task definition: polymarket-bot:$TD_REV"

# 4. Launch new task
echo "  Launching new $MODE task..."
NEW_TASK=$(aws --profile "$PROFILE" ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "polymarket-bot:$TD_REV" \
    --launch-type FARGATE \
    --network-configuration "{\"awsvpcConfiguration\":{\"subnets\":[\"$SUBNET\"],\"securityGroups\":[\"$SG\"],\"assignPublicIp\":\"ENABLED\"}}" \
    --region "$REGION" \
    --query 'tasks[0].taskArn' --output text)

# 5. Restart dashboard container with correct MODE
echo "  Updating dashboard..."
INSTANCE_ID="i-0ee28e7e5fab27497"
DASHBOARD_BANKROLL=$( [ "$MODE" = "live" ] && echo "43.0" || echo "1000.0" )
aws --profile "$PROFILE" ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"docker stop dashboard && docker rm dashboard && docker run -d --name dashboard -p 8888:8888 -e MODE=$MODE -e BANKROLL=$DASHBOARD_BANKROLL -e AWS_DEFAULT_REGION=us-east-1 --restart unless-stopped 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-dashboard:latest\"]" \
    --region eu-west-1 > /dev/null
echo "  Dashboard restarted with MODE=$MODE"

echo ""
echo "==> Done! Bot is now in $MODE mode."
echo "    Task: ${NEW_TASK##*/}"
echo "    Dashboard: http://54.155.183.45:8888/"
echo ""
echo "    To switch back: $0 $([ "$MODE" = "live" ] && echo "paper" || echo "live")"
