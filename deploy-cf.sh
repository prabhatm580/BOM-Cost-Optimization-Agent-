#!/usr/bin/env bash
# =============================================================================
# CF Deploy Script — bom-cost-optimization-agent
# Run this from your local machine (not the sandbox).
#
# Prerequisites:
#   - CF CLI v8 installed: https://github.com/cloudfoundry/cli/releases
#   - Logged in: cf login -a <CF_API> --sso
#   - Service instances exist:
#       cf create-service destination lite proj-vector-destination-service
#       cf create-service connectivity lite proj-vector-connectivity-service
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CF_API="${CF_API:-[REDACTED]}"
CF_ORG="${CF_ORG:-}"
CF_SPACE="${CF_SPACE:-}"
APP_NAME="bom-cost-optimization-agent"

# ---------------------------------------------------------------------------
# 1. Ensure logged in
# ---------------------------------------------------------------------------
echo "==> Checking CF login..."
cf target 2>/dev/null || {
  echo "Not logged in. Running: cf login -a $CF_API --sso"
  cf login -a "$CF_API" --sso
}

if [ -n "$CF_ORG" ] && [ -n "$CF_SPACE" ]; then
  cf target -o "$CF_ORG" -s "$CF_SPACE"
fi

# ---------------------------------------------------------------------------
# 2. Ensure service instances exist
# ---------------------------------------------------------------------------
echo "==> Checking service instances..."
cf service proj-vector-destination-service 2>/dev/null || \
  cf create-service destination lite proj-vector-destination-service

cf service proj-vector-connectivity-service 2>/dev/null || \
  cf create-service connectivity lite proj-vector-connectivity-service

# ---------------------------------------------------------------------------
# 3. Push the app (no-start first to bind services)
# ---------------------------------------------------------------------------
echo "==> Pushing $APP_NAME (no-start)..."
cd "$SCRIPT_DIR"
cf push "$APP_NAME" -f manifest.yml --no-start

# ---------------------------------------------------------------------------
# 4. Start the app
# ---------------------------------------------------------------------------
echo "==> Starting $APP_NAME..."
cf start "$APP_NAME"

# ---------------------------------------------------------------------------
# 5. Show app URL
# ---------------------------------------------------------------------------
echo ""
echo "==> App deployed successfully!"
cf app "$APP_NAME" | grep -E "routes:|state"
echo ""
echo "==> Health check (agent card):"
APP_ROUTE=$(cf app "$APP_NAME" | grep "routes:" | awk '{print $2}')
echo "  https://$APP_ROUTE/.well-known/agent.json"
echo ""
echo "Set AGENT_PUBLIC_URL in manifest.yml if the agent card URL needs updating:"
echo "  cf set-env $APP_NAME AGENT_PUBLIC_URL https://$APP_ROUTE/"
echo "  cf restart $APP_NAME"
