#!/usr/bin/env bash
# Deploy dashboard as Lambda + API Gateway + CloudFront.
# Replaces the EC2 dashboard — cheaper, HTTPS, no server to maintain.
#
# Usage:
#   bash scripts/deploy_dashboard_lambda.sh
#
# Prerequisites:
#   aws CLI configured with --profile playground
#   uv installed (for building Lambda layer)

set -euo pipefail

PROFILE="playground"
REGION="us-east-1"  # Same region as DynamoDB
FUNCTION_NAME="polymarket-dashboard"
ROLE_NAME="polymarket-dashboard-lambda-role"
API_NAME="polymarket-dashboard-api"
CF_COMMENT="Polymarket Bot Dashboard"

echo "=== Building Lambda deployment package ==="

# Create temp build dir
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

# Install dependencies into package
# Build for Lambda (Linux x86_64) — critical for C extensions like pydantic_core
uv pip install --target "$BUILD_DIR/package" \
  --python-platform x86_64-manylinux2014 --python-version 3.12 \
  fastapi mangum httpx uvicorn pydantic pydantic-settings starlette anyio structlog 2>&1 | tail -5

# Copy dashboard code
cp scripts/dashboard.py "$BUILD_DIR/package/"
cp scripts/dashboard_lambda.py "$BUILD_DIR/package/"

# Create zip
cd "$BUILD_DIR/package"
zip -r9 "$BUILD_DIR/lambda.zip" . -x '*.pyc' '__pycache__/*' 'boto3/*' 'botocore/*' 's3transfer/*' 'urllib3/*' 2>&1 | tail -3
cd -

ZIPSIZE=$(du -h "$BUILD_DIR/lambda.zip" | cut -f1)
echo "Package size: $ZIPSIZE"

# ── IAM Role ──────────────────────────────────────────────────────────────────

echo "=== Setting up IAM role ==="

ROLE_ARN=$(aws --profile "$PROFILE" iam get-role \
  --role-name "$ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [[ -z "$ROLE_ARN" || "$ROLE_ARN" == "None" ]]; then
  TRUST_POLICY='{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
  ROLE_ARN=$(aws --profile "$PROFILE" iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --query 'Role.Arn' --output text)

  # Attach policies
  aws --profile "$PROFILE" iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

  # DynamoDB read access + CloudWatch Logs read (for bot logs)
  POLICY='{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["dynamodb:Scan", "dynamodb:Query", "dynamodb:GetItem", "dynamodb:BatchGetItem"],
        "Resource": "arn:aws:dynamodb:us-east-1:*:table/polymarket-bot-*"
      },
      {
        "Effect": "Allow",
        "Action": ["logs:DescribeLogStreams", "logs:GetLogEvents", "logs:FilterLogEvents"],
        "Resource": "arn:aws:logs:eu-west-1:*:log-group:/polymarket-bot:*"
      }
    ]
  }'
  aws --profile "$PROFILE" iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "dashboard-data-access" \
    --policy-document "$POLICY"

  echo "Created role: $ROLE_ARN"
  echo "Waiting 10s for role propagation..."
  sleep 10
else
  echo "Role exists: $ROLE_ARN"
fi

# ── Lambda Function ───────────────────────────────────────────────────────────

echo "=== Deploying Lambda function ==="

EXISTING=$(aws --profile "$PROFILE" lambda get-function \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo "")

if [[ -z "$EXISTING" || "$EXISTING" == "None" ]]; then
  LAMBDA_ARN=$(aws --profile "$PROFILE" lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --runtime python3.12 \
    --handler dashboard_lambda.handler \
    --role "$ROLE_ARN" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={MODE=live,DASHBOARD_USER=admin,DASHBOARD_PASSWORD=polybot2026,TOTAL_DEPOSITED=265.72,POLYMARKET_FUNDER=0x5ca439d661c9b44337E91fC681ec4b006C473610}" \
    --query 'FunctionArn' --output text)
  echo "Created function: $LAMBDA_ARN"
else
  aws --profile "$PROFILE" lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --query 'FunctionArn' --output text
  echo "Updated function code"
  # Also update config
  aws --profile "$PROFILE" lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={MODE=live,DASHBOARD_USER=admin,DASHBOARD_PASSWORD=polybot2026,TOTAL_DEPOSITED=265.72,POLYMARKET_FUNDER=0x5ca439d661c9b44337E91fC681ec4b006C473610}" \
    --query 'FunctionArn' --output text 2>/dev/null || true
  LAMBDA_ARN="$EXISTING"
fi

# ── API Gateway (HTTP API) ────────────────────────────────────────────────────

echo "=== Setting up API Gateway ==="

API_ID=$(aws --profile "$PROFILE" apigatewayv2 get-apis \
  --region "$REGION" \
  --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text 2>/dev/null || echo "")

if [[ -z "$API_ID" || "$API_ID" == "None" ]]; then
  API_ID=$(aws --profile "$PROFILE" apigatewayv2 create-api \
    --region "$REGION" \
    --name "$API_NAME" \
    --protocol-type HTTP \
    --query 'ApiId' --output text)

  # Lambda integration
  INTEGRATION_ID=$(aws --profile "$PROFILE" apigatewayv2 create-integration \
    --region "$REGION" \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "arn:aws:lambda:$REGION:$(aws --profile $PROFILE sts get-caller-identity --query Account --output text):function:$FUNCTION_NAME" \
    --payload-format-version "2.0" \
    --query 'IntegrationId' --output text)

  # Default route (catch-all)
  aws --profile "$PROFILE" apigatewayv2 create-route \
    --region "$REGION" \
    --api-id "$API_ID" \
    --route-key '$default' \
    --target "integrations/$INTEGRATION_ID" \
    --query 'RouteId' --output text

  # Auto-deploy stage
  aws --profile "$PROFILE" apigatewayv2 create-stage \
    --region "$REGION" \
    --api-id "$API_ID" \
    --stage-name '$default' \
    --auto-deploy \
    --query 'StageName' --output text

  # Grant API Gateway permission to invoke Lambda
  ACCOUNT_ID=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text)
  aws --profile "$PROFILE" lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --statement-id "apigateway-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*" 2>/dev/null || true

  echo "Created API Gateway: $API_ID"
else
  echo "API Gateway exists: $API_ID"
fi

API_ENDPOINT=$(aws --profile "$PROFILE" apigatewayv2 get-api \
  --region "$REGION" \
  --api-id "$API_ID" \
  --query 'ApiEndpoint' --output text)

echo ""
echo "=== Dashboard deployed ==="
echo "API Gateway URL: $API_ENDPOINT"
echo "Login: admin / polybot2026"
echo ""
echo "Next steps:"
echo "  1. Add CloudFront distribution for HTTPS + caching"
echo "  2. Terminate EC2 instance i-0ee28e7e5fab27497 to save costs"
echo "  3. Update CLAUDE.md with new dashboard URL"
echo ""
echo "To update dashboard:"
echo "  bash scripts/deploy_dashboard_lambda.sh"
