#!/usr/bin/env bash
# Deploy the CDK app to Ministack with `-c ministack=true`.
#
# Steps:
#   1. cdk bootstrap (idempotent)
#   2. cdk deploy BaselineStack -c ministack=true
#      (Brings up the Aurora-less baseline: VPC, ALB, ECS cluster, ECR, etc.
#       Aurora is omitted because Ministack's CFN engine only provisions
#       `AWS::RDS::DBCluster`, not DBSubnetGroup or DBInstance.)
#   3. scripts/ministack-create-db.py
#      (Creates a real Postgres instance via the RDS API, prints DB_HOST/PORT.)
#   4. scripts/bootstrap-dbs.py create --env-name $ENV_NAME
#      (Creates the per-env logical databases on that Postgres instance.)
#   5. cdk deploy EnvironmentStack-<envName> -c ministack=true -c dbHost -c dbPort
#
# shellcheck disable=SC2016
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(cd "$HERE/.." && pwd)"

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_ENDPOINT_URL="http://localhost:4566"
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-000000000000}"
export CDK_DEFAULT_REGION="$AWS_DEFAULT_REGION"

ENV_NAME="${1:-fg-demo}"
PANTRY_TAG="${PANTRY_TAG:-main}"
SHOPPING_TAG="${SHOPPING_TAG:-main}"

cd "$INFRA_DIR"

CDK_BIN="${CDK_BIN:-}"
if [ -z "$CDK_BIN" ]; then
	if command -v cdklocal >/dev/null 2>&1; then
		CDK_BIN="cdklocal"
	else
		CDK_BIN="npx -y aws-cdk"
	fi
fi

echo "==> bootstrapping CDK in Ministack"
$CDK_BIN bootstrap "aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION}" || true

echo "==> deploying BaselineStack (ministack mode)"
# Intentionally NOT passing -c envName here: the env stack needs dbHost/dbPort
# which only get computed after BaselineStack is up and ministack-create-db.py
# has run. Synthesising the env stack now would fail.
$CDK_BIN deploy --require-approval never BaselineStack -c ministack=true

echo "==> creating Ministack RDS Postgres instance via the RDS API"
DB_INFO="$(uv run python scripts/ministack-create-db.py --baseline-stack BaselineStack)"
echo "$DB_INFO"
DB_HOST="$(echo "$DB_INFO" | grep '^DB_HOST=' | tail -1 | cut -d= -f2)"
DB_PORT="$(echo "$DB_INFO" | grep '^DB_PORT=' | tail -1 | cut -d= -f2)"
DB_HOST_CONTAINER="$(echo "$DB_INFO" | grep '^DB_HOST_CONTAINER=' | tail -1 | cut -d= -f2)"

if [ -z "$DB_HOST" ] || [ -z "$DB_PORT" ]; then
  echo "ERROR: failed to discover DB endpoint from ministack-create-db.py output" >&2
  exit 1
fi
echo "==> host-side DB endpoint: $DB_HOST:$DB_PORT"
echo "==> ECS-container DB endpoint: $DB_HOST_CONTAINER:$DB_PORT"

echo "==> creating per-env logical databases (host-side)"
uv run python scripts/bootstrap-dbs.py create --env-name "$ENV_NAME" \
  --baseline-stack BaselineStack \
  --db-host "$DB_HOST" --db-port "$DB_PORT"

echo "==> deploying EnvironmentStack-$ENV_NAME (ministack mode)"
$CDK_BIN deploy --require-approval never "EnvironmentStack-${ENV_NAME}" \
  -c ministack=true \
  -c envName="$ENV_NAME" \
  -c pantryImageTag="$PANTRY_TAG" \
  -c shoppingImageTag="$SHOPPING_TAG" \
  -c dbHost="$DB_HOST_CONTAINER" \
  -c dbPort="$DB_PORT"

echo "==> done. Preview URLs:"
aws --endpoint-url "$AWS_ENDPOINT_URL" cloudformation describe-stacks \
	--stack-name "EnvironmentStack-${ENV_NAME}" \
	--query 'Stacks[0].Outputs[?starts_with(OutputKey, `Preview`)].[OutputKey,OutputValue]' \
	--output table
