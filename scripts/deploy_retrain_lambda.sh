#!/usr/bin/env bash
# Deploy retrain Lambda + update EventBridge to target it
set -euo pipefail

PROFILE="playground"
REGION="us-east-1"
FUNCTION_NAME="polymarket-retrain"
ROLE_NAME="polymarket-dashboard-lambda-role"  # reuse — has DynamoDB + S3 + SSM access
RULE_NAME="polymarket-bot-retrain"
RULE_REGION="eu-west-1"  # EventBridge rule is in eu-west-1

echo "=== Building retrain Lambda package ==="
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

# Install dependencies — use Docker for scipy/sklearn cross-compilation
mkdir -p "$BUILD_DIR/package"
# Use uv with manylinux platform for Lambda-compatible binaries
uv pip install --target "$BUILD_DIR/package" \
  --python-platform x86_64-manylinux_2_28 --python-version 3.12 \
  lightgbm scikit-learn numpy 2>&1 | tail -5

# Copy trainer code
cp src/polybot/ml/trainer.py "$BUILD_DIR/package/"

# Lambda handler
cat > "$BUILD_DIR/package/lambda_handler.py" << 'PYEOF'
"""Lambda handler for model retraining."""
import json
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def handler(event, context):
    logger.info("retrain_started")
    start = time.time()

    from trainer import train_all
    results = train_all()

    duration = round(time.time() - start, 1)

    summary = []
    for r in results:
        status = "DEPLOYED" if r.deployed else f"SKIPPED: {r.error}"
        summary.append({
            "pair": r.pair,
            "brier": round(r.val_brier, 4),
            "auc": round(r.val_auc, 4),
            "n_train": r.n_train,
            "deployed": r.deployed,
        })
        logger.info(f"  {r.pair}: brier={r.val_brier:.4f} auc={r.val_auc:.4f} {status}")

    logger.info(f"retrain_complete duration={duration}s deployed={sum(1 for r in results if r.deployed)}/{len(results)}")

    return {
        "statusCode": 200,
        "duration_s": duration,
        "results": summary,
    }
PYEOF

cd "$BUILD_DIR/package"
zip -r9 "$BUILD_DIR/lambda.zip" . -x '*.pyc' '__pycache__/*' 'boto3/*' 'botocore/*' 's3transfer/*' 'urllib3/*' 2>&1 | tail -3
cd -

ZIPSIZE=$(du -h "$BUILD_DIR/lambda.zip" | cut -f1)
echo "Package size: $ZIPSIZE"

# Get role ARN
ROLE_ARN=$(aws --profile "$PROFILE" iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

# Create or update Lambda
echo "=== Deploying Lambda ==="
EXISTING=$(aws --profile "$PROFILE" lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo "")

# Upload zip to S3 first (too large for direct upload)
S3_BUCKET="polymarket-bot-data-688567279867-use1"
S3_KEY="lambda/retrain-package.zip"
aws --profile "$PROFILE" s3 cp "$BUILD_DIR/lambda.zip" "s3://$S3_BUCKET/$S3_KEY" --region "$REGION" 2>&1 | tail -1
echo "Uploaded to s3://$S3_BUCKET/$S3_KEY"

if [[ -z "$EXISTING" || "$EXISTING" == "None" ]]; then
  LAMBDA_ARN=$(aws --profile "$PROFILE" lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --runtime python3.12 \
    --handler lambda_handler.handler \
    --role "$ROLE_ARN" \
    --code "S3Bucket=$S3_BUCKET,S3Key=$S3_KEY" \
    --timeout 900 \
    --memory-size 512 \
    --query 'FunctionArn' --output text)
  echo "Created: $LAMBDA_ARN"
else
  aws --profile "$PROFILE" lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --s3-bucket "$S3_BUCKET" \
    --s3-key "$S3_KEY" \
    --query 'FunctionArn' --output text
  # Update config
  aws --profile "$PROFILE" lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --timeout 900 \
    --memory-size 512 \
    --query 'FunctionArn' --output text 2>/dev/null || true
  LAMBDA_ARN="$EXISTING"
  echo "Updated: $LAMBDA_ARN"
fi

# Update EventBridge rule to target Lambda instead of ECS
echo "=== Updating EventBridge rule ==="

# First remove the old ECS target
aws --profile "$PROFILE" events remove-targets \
  --rule "$RULE_NAME" \
  --ids "retrain-task" \
  --region "$RULE_REGION" 2>/dev/null || true

# Grant EventBridge permission to invoke Lambda (cross-region)
aws --profile "$PROFILE" lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --statement-id "eventbridge-retrain" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "arn:aws:events:$RULE_REGION:$(aws --profile $PROFILE sts get-caller-identity --query Account --output text):rule/$RULE_NAME" 2>/dev/null || true

# Add Lambda as target
ACCOUNT_ID=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text)
aws --profile "$PROFILE" events put-targets \
  --rule "$RULE_NAME" \
  --region "$RULE_REGION" \
  --targets "[{\"Id\":\"retrain-lambda\",\"Arn\":\"arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME\"}]" \
  --query 'FailedEntryCount' --output text

echo ""
echo "=== Deployed ==="
echo "Lambda: $FUNCTION_NAME ($REGION, 512MB, 15min timeout)"
echo "EventBridge: $RULE_NAME ($RULE_REGION) → Lambda"
echo "Schedule: rate(4 hours)"
echo ""
echo "Test: aws lambda invoke --function-name $FUNCTION_NAME --region $REGION --profile $PROFILE /tmp/retrain_result.json && cat /tmp/retrain_result.json"
