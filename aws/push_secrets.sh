#!/usr/bin/env bash
# Reads all key=value pairs from .env and uploads them as a single JSON secret
# to AWS Secrets Manager at the path used by the ECS task definition.
set -euo pipefail

REGION="eu-west-1"
PROFILE="playground"
SECRET_NAME="polymarket-bot-env"
ENV_FILE="$(dirname "$(dirname "$0")")/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: .env file not found at ${ENV_FILE}"
  exit 1
fi

echo "==> Reading .env from ${ENV_FILE}"

# Build a JSON object from the .env file.
# Lines starting with # or blank lines are skipped.
json_payload="{"
first=true
while IFS='=' read -r key rest; do
  # Skip comments and blank lines
  [[ -z "${key}" || "${key}" == \#* ]] && continue
  # Trim leading/trailing whitespace from key
  key="${key//[[:space:]]/}"
  [[ -z "${key}" ]] && continue
  # Reconstruct value (handle values that contain '=')
  value="${rest}"
  # Escape double quotes and backslashes in the value for JSON
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  if [[ "${first}" == true ]]; then
    first=false
  else
    json_payload+=","
  fi
  json_payload+="\"${key}\":\"${value}\""
done < "${ENV_FILE}"
json_payload+="}"

echo "==> Checking if secret already exists..."
if aws secretsmanager describe-secret \
    --secret-id "${SECRET_NAME}" \
    --region "${REGION}" \
    --profile "${PROFILE}" \
    > /dev/null 2>&1; then
  echo "==> Secret exists — updating..."
  aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" \
    --secret-string "${json_payload}" \
    --region "${REGION}" \
    --profile "${PROFILE}"
else
  echo "==> Secret does not exist — creating..."
  aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "Environment variables for polymarket-bot ECS task" \
    --secret-string "${json_payload}" \
    --region "${REGION}" \
    --profile "${PROFILE}"
fi

echo "==> Secret '${SECRET_NAME}' pushed successfully."
