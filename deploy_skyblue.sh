#!/usr/bin/env bash
# ============================================================
# Deploy BOMA Agent with sky-blue UI + 1-worker fix
# Run this from your LOCAL machine (not the sandbox)
# Prerequisites: cf CLI v8 installed, logged in to CF
# ============================================================
set -euo pipefail

CF_API="https://api.cf.us10.hana.ondemand.com"
APP_NAME="bom-cost-optimization-agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Logging in..."
cf api "$CF_API"
cf login --sso

echo "==> Deploying $APP_NAME..."
cd "$SCRIPT_DIR"
cf push "$APP_NAME" -f manifest.yml

echo "==> Done!"
cf app "$APP_NAME" | grep routes
