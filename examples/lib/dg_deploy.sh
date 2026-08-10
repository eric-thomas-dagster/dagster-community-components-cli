#!/usr/bin/env bash
# dg-deploy — one .py file (or folder) → one Dagster+ code location.
#
# Two modes, one CLI:
#
#   Serverless (default): builds a pex bundle from your code + pyproject
#     auto-detected deps, uploads directly to Dagster+. No Docker involved.
#
#   Hybrid (--hybrid): builds a per-project Docker image with your code
#     baked in, pushes to your registry, registers with Dagster+.
#     Your Hybrid agent pulls that image and runs it. Same one-command
#     ergonomics; different runtime shape.
#
# Input can be a single .py or a folder of .py files. Either way you
# get ONE Dagster project scaffold + ONE code location. Every .py must
# have `defs = dg.Definitions(...)` at module scope — the scaffold's
# definitions.py imports each one and merges them.
#
# Usage:
#
#   # Serverless — auto-detects deps + builds pex + deploys.
#   ./dg-deploy my_flow.py
#   ./dg-deploy flows/                       # a folder of .py files
#   ./dg-deploy my_flow.py --location-name my-flow
#
#   # Hybrid — auto-detects deps + builds Docker image + push + deploy.
#   # --registry is required (customer picks the registry).
#   ./dg-deploy my_flow.py --hybrid --registry ghcr.io/USER/my-flow
#   ./dg-deploy flows/     --hybrid --registry ghcr.io/USER/my-flows
#
#   # Deploy to a specific Dagster+ deployment (default: prod).
#   ./dg-deploy my_flow.py --deployment staging
#
# Options (common):
#   --location-name NAME     Code location name (default: basename of .py / folder)
#   --deployment NAME        Dagster+ deployment (default: whatever's in ~/.config/dagster_cloud)
#   --agent-queue NAME       Route location to a specific Hybrid agent queue
#   --deps 'pkg1 pkg2'       Extra deps beyond auto-detected imports
#   --no-auto-deps           Skip import-based auto-detection
#   --python-version VER     (default: 3.12)
#   --dry-run                Print commands, don't execute
#   --keep-scaffold          Don't rm -rf the scaffold after deploy
#
# Options (--hybrid only):
#   --registry URL           REQUIRED. E.g. ghcr.io/user/name or acr/gcr/ecr equivalent.
#   --tag TAG                Image tag (default: git short SHA or 'latest')
#
# Requires:
#   - `dagster-cloud config setup` done once (org + deployment + token cached)
#   - For --hybrid: local docker + push access to --registry

set -eo pipefail

SOURCE=""
HYBRID=""
LOCATION_NAME=""
REGISTRY=""
TAG=""
AGENT_QUEUE=""
DEPLOYMENT=""
EXTRA_DEPS=""
AUTO_DEPS=1
DRY_RUN=""
KEEP_SCAFFOLD=""
PYTHON_VERSION="3.12"

# First positional arg is the .py path or folder.
if [ $# -gt 0 ] && [ "${1:0:2}" != "--" ]; then
    SOURCE="$1"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --hybrid) HYBRID=1; shift ;;
        --location-name) LOCATION_NAME="$2"; shift 2 ;;
        --registry) REGISTRY="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --agent-queue) AGENT_QUEUE="$2"; shift 2 ;;
        --deployment) DEPLOYMENT="$2"; shift 2 ;;
        --deps) EXTRA_DEPS="$2"; shift 2 ;;
        --no-auto-deps) AUTO_DEPS=0; shift ;;
        --python-version) PYTHON_VERSION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-scaffold) KEEP_SCAFFOLD=1; shift ;;
        -h|--help) grep '^#' "$0" | cut -c 3-; exit 0 ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$SOURCE" ] || [ ! -e "$SOURCE" ]; then
    echo "usage: $0 path/to/file.py [options]"
    echo "  or:  $0 path/to/flows_folder/ [options]"
    echo ""
    echo "  Serverless (default): $0 my_flow.py"
    echo "  Hybrid:               $0 my_flow.py --hybrid --registry ghcr.io/USER/name"
    exit 1
fi

if [ -n "$HYBRID" ] && [ -z "$REGISTRY" ]; then
    echo "✗ --hybrid requires --registry ghcr.io/USER/name (or your ECR/GCR/ACR/etc.)"
    echo "  The CLI needs to push the built image somewhere your Hybrid agent can pull from."
    exit 1
fi

# ── Determine input shape ───────────────────────────────────────────
INPUT_FILES=()
if [ -d "$SOURCE" ]; then
    # Folder input — collect all .py files (non-recursive; user files should be flat)
    while IFS= read -r f; do
        INPUT_FILES+=("$f")
    done < <(find "$SOURCE" -maxdepth 1 -type f -name "*.py" | sort)
    if [ ${#INPUT_FILES[@]} -eq 0 ]; then
        echo "✗ No .py files found in $SOURCE"
        exit 1
    fi
    BASENAME=$(basename "$SOURCE")
else
    INPUT_FILES=("$SOURCE")
    BASENAME=$(basename "$SOURCE" .py)
fi

# Sanitize name for use as Python module.
MODULE_NAME=$(echo "$BASENAME" | tr -- '-. ' '___')
[ -z "$LOCATION_NAME" ] && LOCATION_NAME="$BASENAME"

# ── Resolve tag (Hybrid only) ───────────────────────────────────────
if [ -n "$HYBRID" ] && [ -z "$TAG" ]; then
    if git rev-parse --short HEAD >/dev/null 2>&1; then
        TAG=$(git rev-parse --short HEAD)
    else
        TAG="latest"
    fi
fi

# ── Auto-detect deps from imports ───────────────────────────────────
AUTO_DETECTED_DEPS=""
if [ "$AUTO_DEPS" = "1" ] && command -v python3 >/dev/null 2>&1; then
    AUTO_DETECTED_DEPS=$(python3 - "${INPUT_FILES[@]}" <<'PYEOF'
"""Parse .py files, extract top-level import statements, filter stdlib,
emit non-stdlib top-level module names (one per line)."""
import ast, sys

BASE_DEPS = {"dagster", "dagster_cloud", "dagster_cloud_cli"}
IMPORT_TO_PIP = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "google.cloud.bigquery": "google-cloud-bigquery",
    "google.cloud.storage": "google-cloud-storage",
}

stdlib = getattr(sys, "stdlib_module_names", set())
found = set()
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                found.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module.split(".")[0])

extern = sorted(m for m in found if m not in stdlib and m not in BASE_DEPS and not m.startswith("_"))
for m in extern:
    print(IMPORT_TO_PIP.get(m, m))
PYEOF
    )
fi
COMBINED_DEPS=$(printf '%s\n%s\n' "$AUTO_DETECTED_DEPS" "$EXTRA_DEPS" | tr ' ' '\n' | { grep -v '^$' || true; } | sort -u)

# ── Scaffold ────────────────────────────────────────────────────────
SCAFFOLD_DIR="${MODULE_NAME}_scaffold"
echo ">>> Scaffolding $SCAFFOLD_DIR/ from $SOURCE"
rm -rf "$SCAFFOLD_DIR"
mkdir -p "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/__init__.py"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs/__init__.py"

# Copy user files.
for f in "${INPUT_FILES[@]}"; do
    cp "$f" "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs/"
done

# definitions.py — imports every module in user_defs/, merges their `defs`.
cat > "$SCAFFOLD_DIR/src/$MODULE_NAME/definitions.py" <<PYEOF
"""Auto-generated by dg-deploy. Loads every .py under user_defs/ and
merges their \`defs = dg.Definitions(...)\` module-scope objects into
one Definitions for this code location."""
import importlib
import pkgutil

import dagster as dg

from $MODULE_NAME import user_defs

_defs_list = []
_skipped = []
for _, name, _ in pkgutil.iter_modules(user_defs.__path__):
    module = importlib.import_module(f"$MODULE_NAME.user_defs.{name}")
    if hasattr(module, "defs") and isinstance(module.defs, dg.Definitions):
        _defs_list.append(module.defs)
    else:
        _skipped.append(name)

if _skipped:
    import sys
    print(
        f"[dg-deploy scaffold] skipped modules (no module-scope \`defs = dg.Definitions(...)\`): "
        f"{_skipped}",
        file=sys.stderr,
    )

defs = dg.Definitions.merge(*_defs_list) if _defs_list else dg.Definitions()
PYEOF

# pyproject.toml.
{
    cat <<TOMLEOF
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "$MODULE_NAME"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "dagster>=1.10",
    "dagster-cloud",
TOMLEOF
    if [ -n "$COMBINED_DEPS" ]; then
        while IFS= read -r dep; do
            [ -n "$dep" ] && echo "    \"$dep\","
        done <<< "$COMBINED_DEPS"
    fi
    cat <<'TOMLEOF'
]

[tool.hatch.metadata]
allow-direct-references = true

TOMLEOF
    cat <<TOMLEOF
[tool.hatch.build.targets.wheel]
packages = ["src/$MODULE_NAME"]

[tool.dg]
directory_type = "project"

[tool.dg.project]
root_module = "$MODULE_NAME"
TOMLEOF
} > "$SCAFFOLD_DIR/pyproject.toml"

# dagster_cloud.yaml.
{
    cat <<YAMLEOF
locations:
  - location_name: $LOCATION_NAME
    code_source:
      module_name: $MODULE_NAME.definitions
YAMLEOF
    [ -n "$AGENT_QUEUE" ] && echo "    agent_queue: $AGENT_QUEUE"
} > "$SCAFFOLD_DIR/dagster_cloud.yaml"

# Dockerfile (Hybrid only).
if [ -n "$HYBRID" ]; then
    cat > "$SCAFFOLD_DIR/Dockerfile" <<DOCKEREOF
FROM python:$PYTHON_VERSION-slim

WORKDIR /opt/dagster/app

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip \\
    && pip install --no-cache-dir -e .

ENV DAGSTER_HOME=/opt/dagster/dagster_home
RUN mkdir -p \$DAGSTER_HOME
DOCKEREOF
fi

echo "✓ Scaffold ready:"
echo "    src/$MODULE_NAME/user_defs/    ← ${#INPUT_FILES[@]} file(s)"
echo "    src/$MODULE_NAME/definitions.py  (auto-generated, merges user defs)"
echo "    pyproject.toml"
echo "    dagster_cloud.yaml"
[ -n "$HYBRID" ] && echo "    Dockerfile"
if [ -n "$COMBINED_DEPS" ]; then
    echo "    deps: dagster, dagster-cloud,"
    while IFS= read -r dep; do [ -n "$dep" ] && echo "          $dep"; done <<< "$COMBINED_DEPS"
fi
echo ""

# ── Deploy ──────────────────────────────────────────────────────────
if [ -z "$HYBRID" ]; then
    # ── Serverless: build pex + upload ─────────────────────────────
    DEPLOY_CMD="uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . --location-name $LOCATION_NAME --module-name $MODULE_NAME.definitions --python-version $PYTHON_VERSION"
    [ -n "$DEPLOYMENT" ] && DEPLOY_CMD="$DEPLOY_CMD --deployment $DEPLOYMENT"
    if [ -n "$DRY_RUN" ]; then
        echo "(dry-run) to deploy:"
        echo "  cd $SCAFFOLD_DIR && $DEPLOY_CMD"
        exit 0
    fi
    echo ">>> Deploying to Dagster+ Serverless (pex, no Docker)…"
    (cd "$SCAFFOLD_DIR" && $DEPLOY_CMD)
else
    # ── Hybrid: docker build + push + deploy-docker ────────────────
    IMAGE="$REGISTRY:$TAG"
    echo ">>> Building Docker image: $IMAGE"
    if ! command -v docker >/dev/null 2>&1; then
        echo "✗ docker not found. Install Docker Desktop / colima / podman."
        exit 1
    fi
    if [ -n "$DRY_RUN" ]; then
        echo "(dry-run) to deploy:"
        echo "  cd $SCAFFOLD_DIR"
        echo "  docker build -t $IMAGE ."
        echo "  docker push $IMAGE"
        echo "  uvx --from dagster-cloud-cli dagster-cloud serverless deploy-docker . --image $IMAGE --location-name $LOCATION_NAME${DEPLOYMENT:+ --deployment $DEPLOYMENT}"
        exit 0
    fi
    (cd "$SCAFFOLD_DIR" && docker build --platform linux/amd64 -t "$IMAGE" .)
    echo ">>> Pushing $IMAGE"
    docker push "$IMAGE"
    echo ">>> Registering location with Dagster+…"
    # Use `deployment add-location` (adds OR updates, one location at a
    # time) — NOT `sync-locations`, which is destructive (would delete
    # any location not present in the workspace file).
    #
    # We pass --from with a mini one-location yaml so agent_queue and
    # container_context extensibility work; add-location's flat CLI
    # args don't cover those.
    LOCATION_FILE=$(mktemp -t dg_deploy_loc.XXXXXX)
    trap "rm -f $LOCATION_FILE" EXIT
    {
        cat <<YAMLEOF
locations:
  - location_name: $LOCATION_NAME
    image: $IMAGE
    code_source:
      module_name: $MODULE_NAME.definitions
YAMLEOF
        [ -n "$AGENT_QUEUE" ] && echo "    agent_queue: $AGENT_QUEUE"
    } > "$LOCATION_FILE"
    ADD_ARGS=(deployment add-location --from "$LOCATION_FILE" --location-load-timeout 300)
    [ -n "$DEPLOYMENT" ] && ADD_ARGS+=(--deployment "$DEPLOYMENT")
    uvx --from dagster-cloud-cli dagster-cloud "${ADD_ARGS[@]}"
fi

echo ""
if [ -z "$KEEP_SCAFFOLD" ]; then
    echo ">>> Cleaning up scaffold (--keep-scaffold to preserve for iteration)"
    rm -rf "$SCAFFOLD_DIR"
else
    echo ">>> Scaffold preserved at $SCAFFOLD_DIR/"
fi

echo "✓ Live. Iterate: edit your .py, run dg-deploy again — same one command."
