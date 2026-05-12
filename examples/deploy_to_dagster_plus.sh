#!/usr/bin/env bash
# Deploy any local demo project to Dagster+ Serverless.
#
# USAGE:
#   ./deploy_to_dagster_plus.sh <project_dir> [--organization X] [--deployment Y]
#
# Example:
#   curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_kitchen_sink_demo.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/deploy_to_dagster_plus.sh | bash -s kitchen-sink-demo
#
# What it does:
#   1. Verifies the project directory exists + is a dagster project
#   2. Adds dagster-cloud-cli as a dev dep (idempotent)
#   3. Runs `dg plus login` if you aren't already logged in
#      (opens a browser tab; one-time setup per machine)
#   4. Runs `dg plus deploy --build-strategy python-executable`
#      (Serverless agent default; no Docker build, much faster)
#
# Requires:
#   - A Dagster+ account at https://dagster.io/plus (free 30-day trial)
#   - `uvx`, `uv` (you already have these from running the demo setup)
#
# For Dagster+ Hybrid (your own k8s) instead of Serverless, change
# --build-strategy to `docker`. You'll need Docker running locally + a
# registry the Hybrid agent can pull from.

set -euo pipefail

PROJECT_DIR=""
DAGSTER_PLUS_ORG=""
DAGSTER_PLUS_DEPLOYMENT="prod"
BUILD_STRATEGY="python-executable"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --organization|--org) DAGSTER_PLUS_ORG="$2"; shift 2;;
    --deployment) DAGSTER_PLUS_DEPLOYMENT="$2"; shift 2;;
    --build-strategy) BUILD_STRATEGY="$2"; shift 2;;
    --help|-h)
      grep -E "^#( |$)" "$0" | sed 's/^# \{0,1\}//'
      exit 0;;
    *) PROJECT_DIR="$1"; shift;;
  esac
done

if [ -z "$PROJECT_DIR" ]; then
  echo "Usage: $0 <project_dir> [--organization X] [--deployment Y]"
  exit 2
fi
if [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: $PROJECT_DIR is not a directory. Run the demo setup first."
  exit 2
fi

cd "$PROJECT_DIR"

# Sanity-check this looks like a dg project
if [ ! -f pyproject.toml ] || ! grep -q "tool.dg" pyproject.toml; then
  echo "ERROR: $PROJECT_DIR doesn't look like a Dagster project (no [tool.dg] in pyproject.toml)."
  exit 2
fi

echo "==> Step 1/4: Ensuring dagster-cloud-cli is installed in dev deps"
if ! grep -q "dagster-cloud-cli" pyproject.toml; then
  uv add --dev -q dagster-cloud-cli
  echo "    ✓ added dagster-cloud-cli"
else
  echo "    ✓ already present"
fi

echo "==> Step 2/4: Checking Dagster+ login status"
# `dg plus config view` errors if not configured
if ! uv run dg plus config view >/dev/null 2>&1; then
  echo "    No Dagster+ config found. Running 'dg plus login' — this opens a browser tab."
  echo "    You'll need a Dagster+ account (free trial: https://dagster.io/plus)."
  echo ""
  uv run dg plus login
else
  echo "    ✓ already configured:"
  uv run dg plus config view 2>&1 | sed 's/^/      /'
fi

echo ""
echo "==> Step 3/4: Confirming deployment target"
ORG_ARG=""
DEPLOY_ARG=""
[ -n "$DAGSTER_PLUS_ORG" ] && ORG_ARG="--organization $DAGSTER_PLUS_ORG"
[ -n "$DAGSTER_PLUS_DEPLOYMENT" ] && DEPLOY_ARG="--deployment $DAGSTER_PLUS_DEPLOYMENT"
echo "    organization: ${DAGSTER_PLUS_ORG:-(from dg plus login)}"
echo "    deployment:   $DAGSTER_PLUS_DEPLOYMENT"
echo "    build:        $BUILD_STRATEGY"

echo ""
echo "==> Step 4/4: Running dg plus deploy"
echo "    (Serverless agent — no Docker required.)"
echo ""
# shellcheck disable=SC2086
uv run dg plus deploy --build-strategy "$BUILD_STRATEGY" $ORG_ARG $DEPLOY_ARG

cat <<MSG

>>> Deploy complete.

Your demo is now running on Dagster+. To open it:

    open "https://${DAGSTER_PLUS_ORG:-your-org}.dagster.cloud/$DAGSTER_PLUS_DEPLOYMENT"

The first run will take longer (cold start). Subsequent runs reuse the
Serverless agent container.

If anything failed:
  - Check 'uv run dg plus config view' to verify org / deployment
  - Re-run 'uv run dg plus login' if your token expired
  - Check the Dagster+ UI logs for build failures
  - See https://docs.dagster.io/api/clis/dg-cli/dg-plus

For Hybrid (your own k8s) instead of Serverless:
    $0 $PROJECT_DIR --build-strategy docker
MSG
