#!/usr/bin/env bash
# Deploy fade news monitor as Lambda + EventBridge (runs every 60s)
set -euo pipefail

PROFILE="playground"
REGION="us-east-1"
FUNCTION_NAME="polymarket-fade-monitor"
ROLE_NAME="polymarket-dashboard-lambda-role"  # reuse existing role

echo "=== Building Lambda package ==="
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

uv pip install --target "$BUILD_DIR/package" \
  --python-platform x86_64-manylinux2014 --python-version 3.12 \
  httpx boto3 2>&1 | tail -3

cp scripts/fade_news_monitor.py "$BUILD_DIR/package/"

# Lambda handler wrapper
cat > "$BUILD_DIR/package/lambda_handler.py" << 'PYEOF'
"""Lambda handler for fade news monitor."""
import fade_news_monitor

import time as _time

def handler(event, context):
    table = fade_news_monitor._get_dynamo_table()
    # Scan twice per invocation (30s apart) since EventBridge minimum is 1 min
    results1 = fade_news_monitor.scan_once()
    fade_news_monitor.save_to_dynamo(results1, table)
    fade_news_monitor.check_resolutions(table)
    _time.sleep(30)
    results2 = fade_news_monitor.scan_once()
    fade_news_monitor.save_to_dynamo(results2, table)
    return {"scanned": len(results1) + len(results2)}
PYEOF

cd "$BUILD_DIR/package"
zip -r9 "$BUILD_DIR/lambda.zip" . -x '*.pyc' '__pycache__/*' 'boto3/*' 'botocore/*' 's3transfer/*' 'urllib3/*' 2>&1 | tail -3
cd -

ZIPSIZE=$(du -h "$BUILD_DIR/lambda.zip" | cut -f1)
echo "Package size: $ZIPSIZE"

# Get role ARN
ROLE_ARN=$(aws --profile "$PROFILE" iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)
echo "Using role: $ROLE_ARN"

# Create or update Lambda
echo "=== Deploying Lambda ==="
EXISTING=$(aws --profile "$PROFILE" lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo "")

if [[ -z "$EXISTING" || "$EXISTING" == "None" ]]; then
  LAMBDA_ARN=$(aws --profile "$PROFILE" lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --runtime python3.12 \
    --handler lambda_handler.handler \
    --role "$ROLE_ARN" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --timeout 90 \
    --memory-size 128 \
    --query 'FunctionArn' --output text)
  echo "Created: $LAMBDA_ARN"
else
  aws --profile "$PROFILE" lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --query 'FunctionArn' --output text
  echo "Updated function code"
  LAMBDA_ARN="$EXISTING"
fi

# Create EventBridge rule (every 60 seconds)
echo "=== Setting up EventBridge schedule ==="
RULE_ARN=$(aws --profile "$PROFILE" events put-rule \
  --name "fade-monitor-every-minute" \
  --schedule-expression "rate(1 minute)" \
  --state ENABLED \
  --region "$REGION" \
  --query 'RuleArn' --output text)

# Grant EventBridge permission to invoke Lambda
aws --profile "$PROFILE" lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --statement-id "eventbridge-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "$RULE_ARN" 2>/dev/null || true

# Set Lambda as target
ACCOUNT_ID=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text)
aws --profile "$PROFILE" events put-targets \
  --rule "fade-monitor-every-minute" \
  --region "$REGION" \
  --targets "Id=fade-monitor,Arn=arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME" \
  --query 'FailedEntryCount' --output text

echo ""
echo "=== Deployed ==="
echo "Lambda: $FUNCTION_NAME ($REGION)"
echo "Schedule: every 60 seconds via EventBridge"
echo "DynamoDB: polymarket-bot-fade-news ($REGION)"
echo ""
echo "Check logs: aws logs tail /aws/lambda/$FUNCTION_NAME --profile $PROFILE --region $REGION --follow"
