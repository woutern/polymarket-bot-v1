#!/usr/bin/env bash
# Deploy dashboard to a dedicated EC2 t3.nano with Elastic IP.
# Run once — gives you a fixed URL that never changes on bot redeploys.
#
# Usage:
#   bash scripts/deploy_dashboard.sh [--build]   # --build rebuilds & pushes image first
#
# Prerequisites:
#   aws CLI configured with --profile playground, region eu-west-1
#   ECR repo: 688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-dashboard
#   .env file with all required env vars

set -euo pipefail

PROFILE="playground"
REGION="eu-west-1"
ECR="688567279867.dkr.ecr.eu-west-1.amazonaws.com"
IMAGE="${ECR}/polymarket-dashboard:latest"
INSTANCE_NAME="polymarket-dashboard"
INSTANCE_TYPE="t3.nano"
AMI_ID="ami-0c1c30571d2dae5be"  # Amazon Linux 2023, eu-west-1

if [[ "${1:-}" == "--build" ]]; then
  echo "=== Building dashboard image ==="
  aws --profile "$PROFILE" ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR"
  docker build --platform linux/amd64 -f Dockerfile.dashboard -t polymarket-dashboard:latest .
  docker tag polymarket-dashboard:latest "$IMAGE"
  docker push "$IMAGE"
  echo "=== Image pushed: $IMAGE ==="
fi

# Check if instance already exists
INSTANCE_ID=$(aws --profile "$PROFILE" ec2 describe-instances \
  --region "$REGION" \
  --filters "Name=tag:Name,Values=${INSTANCE_NAME}" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' \
  --output text 2>/dev/null)

if [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]]; then
  echo "=== No existing instance, launching new t3.nano ==="

  # Create security group if needed
  SG_ID=$(aws --profile "$PROFILE" ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=group-name,Values=polymarket-dashboard-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

  if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
    SG_ID=$(aws --profile "$PROFILE" ec2 create-security-group \
      --region "$REGION" \
      --group-name "polymarket-dashboard-sg" \
      --description "Polymarket dashboard HTTP access" \
      --query 'GroupId' --output text)
    aws --profile "$PROFILE" ec2 authorize-security-group-ingress \
      --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port 8888 --cidr 0.0.0.0/0
    aws --profile "$PROFILE" ec2 authorize-security-group-ingress \
      --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port 22 --cidr 0.0.0.0/0
    echo "Created security group: $SG_ID"
  fi

  # Load env vars from .env
  source .env 2>/dev/null || true

  USER_DATA=$(cat <<EOF
#!/bin/bash
yum install -y docker
systemctl start docker
systemctl enable docker
aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR}
docker pull ${IMAGE}
docker run -d --restart=always -p 8888:8888 \\
  -e MODE="${MODE:-paper}" \\
  -e BANKROLL="${BANKROLL:-1000}" \\
  -e AWS_DEFAULT_REGION="${REGION}" \\
  --name dashboard \\
  ${IMAGE}
EOF
)

  # Instance profile needs DynamoDB + Bedrock access (same as bot task role)
  INSTANCE_ID=$(aws --profile "$PROFILE" ec2 run-instances \
    --region "$REGION" \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --security-group-ids "$SG_ID" \
    --iam-instance-profile "Name=polymarket-bot-ec2-profile" \
    --user-data "$USER_DATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}}]" \
    --query 'Instances[0].InstanceId' --output text)
  echo "Launched instance: $INSTANCE_ID"
fi

# Allocate and associate Elastic IP if not already done
EIP=$(aws --profile "$PROFILE" ec2 describe-addresses \
  --region "$REGION" \
  --filters "Name=tag:Name,Values=${INSTANCE_NAME}-eip" \
  --query 'Addresses[0].PublicIp' --output text 2>/dev/null)

if [[ "$EIP" == "None" || -z "$EIP" ]]; then
  ALLOC_ID=$(aws --profile "$PROFILE" ec2 allocate-address \
    --region "$REGION" --domain vpc \
    --query 'AllocationId' --output text)
  aws --profile "$PROFILE" ec2 create-tags \
    --region "$REGION" --resources "$ALLOC_ID" \
    --tags "Key=Name,Value=${INSTANCE_NAME}-eip"
  aws --profile "$PROFILE" ec2 associate-address \
    --region "$REGION" \
    --instance-id "$INSTANCE_ID" \
    --allocation-id "$ALLOC_ID"
  EIP=$(aws --profile "$PROFILE" ec2 describe-addresses \
    --region "$REGION" --allocation-ids "$ALLOC_ID" \
    --query 'Addresses[0].PublicIp' --output text)
  echo "Assigned Elastic IP: $EIP"
fi

echo ""
echo "=== Dashboard deployed ==="
echo "URL: http://${EIP}:8888/"
echo "Login: admin / polybot2026"
echo ""
echo "To update dashboard only (no bot redeploy needed):"
echo "  bash scripts/deploy_dashboard.sh --build"
echo "  ssh ec2-user@${EIP} 'docker pull ${IMAGE} && docker restart dashboard'"
