#!/usr/bin/env bash
# Build the db_bootstrap Lambda asset.
# pg8000 is pure-Python so no platform-specific wheels are needed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LAMBDA_DIR="$HERE/../lambdas/db_bootstrap"
BUILD_DIR="$LAMBDA_DIR/build"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

python3 -m pip install --quiet --upgrade --target "$BUILD_DIR" \
	-r "$LAMBDA_DIR/requirements.txt"
cp "$LAMBDA_DIR/handler.py" "$BUILD_DIR/"

echo "Built Lambda bundle at $BUILD_DIR"
