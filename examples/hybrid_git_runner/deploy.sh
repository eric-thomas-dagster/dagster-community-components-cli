#!/usr/bin/env bash
# deploy.sh — build (optional) + push (optional) + register the hybrid_git_runner
# image with Dagster+ Hybrid.
#
# Three modes, in order of least-effort first:
#
# 1. PREBUILT (no --registry, no --image): use the maintainer-published image
#    at `ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest`. No local
#    Docker needed. Deploys straight to Dagster+.
#
# 2. YOUR REGISTRY (--registry your.registry/org/name): build locally, push
#    to your registry, deploy the pushed image. Requires local Docker + push
#    access to the registry. Best when you want to review / patch the
#    image before shipping.
#
# 3. YOUR IMAGE (--image other.registry/prebuilt:tag): use a specific image
#    you've already pushed elsewhere. No build. No push. Just register with
#    Dagster+.
#
# Usage:
#   ./deploy.sh                                                           # mode 1
#   ./deploy.sh --registry ghcr.io/your-org/hybrid-git-runner              # mode 2
#   ./deploy.sh --image ghcr.io/other/prebuilt-image:v1.2.3                # mode 3
#
# Common options:
#   --location-name NAME       (default: hybrid_git_runner)
#   --deployment NAME          (default: prod — pass staging/dev/branch-<pr>/etc.)
#   --tag TAG                  (default: git short SHA if in git repo, else 'latest')

set -eo pipefail

PREBUILT_IMAGE="ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest"
REGISTRY=""
IMAGE=""
LOCATION_NAME="hybrid_git_runner"
DEPLOYMENT=""   # empty = use whatever config.yaml says
TAG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --registry) REGISTRY="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --location-name) LOCATION_NAME="$2"; shift 2 ;;
        --deployment) DEPLOYMENT="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | cut -c 3-
            exit 0
            ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

# Resolve TAG default — git short SHA when available, else 'latest'.
if [ -z "$TAG" ]; then
    if git rev-parse --short HEAD >/dev/null 2>&1; then
        TAG=$(git rev-parse --short HEAD)
    else
        TAG="latest"
    fi
fi

# ── Resolve which image to deploy ──────────────────────────────────────
if [ -n "$IMAGE" ] && [ -n "$REGISTRY" ]; then
    echo "✗ --image and --registry are mutually exclusive."
    echo "  --image = use a specific prebuilt image (no build)"
    echo "  --registry = build locally + push to this registry"
    exit 1
fi

if [ -n "$IMAGE" ]; then
    # Mode 3 — user-provided prebuilt image.
    FINAL_IMAGE="$IMAGE"
    MODE="prebuilt (--image)"
elif [ -n "$REGISTRY" ]; then
    # Mode 2 — build + push.
    FINAL_IMAGE="$REGISTRY:$TAG"
    MODE="build+push (--registry)"
    echo ">>> [Mode 2] Building $FINAL_IMAGE from local Dockerfile"
    if ! command -v docker >/dev/null 2>&1; then
        echo "✗ docker not found — install Docker Desktop / colima / podman first, or use mode 1/3."
        exit 1
    fi
    docker build -t "$FINAL_IMAGE" .
    echo ">>> Pushing $FINAL_IMAGE"
    docker push "$FINAL_IMAGE"
else
    # Mode 1 — prebuilt default.
    FINAL_IMAGE="$PREBUILT_IMAGE"
    MODE="prebuilt (default)"
fi

echo ""
echo "══════════════════════════════════════════════════════════════════════"
echo "  Mode:            $MODE"
echo "  Image:           $FINAL_IMAGE"
echo "  Location name:   $LOCATION_NAME"
[ -n "$DEPLOYMENT" ] && echo "  Deployment:      $DEPLOYMENT"
echo "══════════════════════════════════════════════════════════════════════"
echo ""

# ── Deploy to Dagster+ ─────────────────────────────────────────────────
DEPLOY_ARGS=(
    "serverless" "deploy-docker" "."
    "--image" "$FINAL_IMAGE"
    "--location-name" "$LOCATION_NAME"
)
[ -n "$DEPLOYMENT" ] && DEPLOY_ARGS+=("--deployment" "$DEPLOYMENT")

echo ">>> Registering with Dagster+..."
echo "    (uses whatever org/deployment/token is cached in ~/.config/dagster_cloud/)"
uvx --with pex --from dagster-cloud-cli dagster-cloud "${DEPLOY_ARGS[@]}"

echo ""
echo "✓ Registered $LOCATION_NAME on Dagster+."
echo "  Your Hybrid agent will pull $FINAL_IMAGE on next code-location load."
echo "  Iterate by pushing flow .py files to your SCRIPTS_REPO_URL repo —"
echo "  no need to rerun this script until you want to change the runner"
echo "  image itself."
