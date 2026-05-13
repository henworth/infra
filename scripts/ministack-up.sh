#!/usr/bin/env bash
# Start Ministack and prepare ECR repos + initial images for local CDK deploys.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(cd "$HERE/.." && pwd)"
# We expect pantry and shopping-list to be checked out as siblings of infra/.
WORKSPACE_DIR="$(cd "$INFRA_DIR/.." && pwd)"

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_ENDPOINT_URL="http://localhost:4566"
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-000000000000}"
export CDK_DEFAULT_REGION="$AWS_DEFAULT_REGION"

echo "==> starting ministack"
docker compose -f "$INFRA_DIR/docker-compose.ministack.yaml" up -d ministack

echo "==> waiting for ministack to be ready"
for _ in $(seq 1 30); do
	if curl -fsS "$AWS_ENDPOINT_URL/_localstack/health" >/dev/null 2>&1 ||
		curl -fsS "$AWS_ENDPOINT_URL/" >/dev/null 2>&1; then
		break
	fi
	sleep 1
done

echo "==> creating ECR repos"
for repo in pantry shopping-list; do
	aws --endpoint-url "$AWS_ENDPOINT_URL" ecr describe-repositories \
		--repository-names "$repo" >/dev/null 2>&1 ||
		aws --endpoint-url "$AWS_ENDPOINT_URL" ecr create-repository \
			--repository-name "$repo" >/dev/null
done

echo "==> building and pushing app images (tag: main)"
ECR_HOST="000000000000.dkr.ecr.${AWS_DEFAULT_REGION}.localhost.localstack.cloud:4566"
PANTRY_URI="${ECR_HOST}/pantry"
SHOPPING_URI="${ECR_HOST}/shopping-list"
docker build -t "$PANTRY_URI:main" "$WORKSPACE_DIR/pantry"
docker build -t "$SHOPPING_URI:main" "$WORKSPACE_DIR/shopping-list"

aws --endpoint-url "$AWS_ENDPOINT_URL" ecr get-login-password |
	docker login --username AWS --password-stdin "$ECR_HOST" ||
	true
docker push "$PANTRY_URI:main" || true
docker push "$SHOPPING_URI:main" || true

echo "==> ministack ready at $AWS_ENDPOINT_URL"
