#!/usr/bin/env bash
# dg_deploy_one_file.sh — Prefect-style "one .py file → Dagster+ Serverless" deploy.
#
# Takes a single .py file with `defs = dg.Definitions(...)` at module scope,
# scaffolds the minimum Dagster project layout around it (pyproject.toml +
# dagster_cloud.yaml + src/<name>/), and deploys to Dagster+ Serverless.
#
# Usage:
#
#   ./dg_deploy_one_file.sh path/to/my_flow.py \
#       --location-name my-flow \
#       --deps 'litellm requests pandas' \
#       --python-version 3.12
#
# Assumes you've already done a one-time `dagster-cloud config setup` so the
# org + deployment + user token are cached in ~/.config/dagster_cloud/.
#
# Options:
#   --location-name NAME     (default: basename of .py file)
#   --deps 'pkg1 pkg2'       (extra deps beyond dagster + dagster-cloud)
#   --python-version VER     (default: 3.12)
#   --dry-run                (scaffold + print deploy command, don't run it)
#   --keep-scaffold          (don't rm -rf the scaffold dir after deploy)

set -eo pipefail

SOURCE_PY="$1"
if [ -z "$SOURCE_PY" ] || [ ! -f "$SOURCE_PY" ]; then
    echo "usage: $0 path/to/file.py [--location-name NAME] [--deps 'pkg1 pkg2'] [--dry-run] [--keep-scaffold]"
    exit 1
fi

BASENAME=$(basename "$SOURCE_PY" .py)
# Sanitize for use as a Python module name (kebab → snake).
MODULE_NAME=$(echo "$BASENAME" | tr '-' '_')
LOCATION_NAME="$BASENAME"
EXTRA_DEPS=""
DRY_RUN=""
KEEP_SCAFFOLD=""
PYTHON_VERSION="3.12"

shift
while [ $# -gt 0 ]; do
    case "$1" in
        --location-name) LOCATION_NAME="$2"; shift 2 ;;
        --deps) EXTRA_DEPS="$2"; shift 2 ;;
        --python-version) PYTHON_VERSION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-scaffold) KEEP_SCAFFOLD=1; shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

SCAFFOLD_DIR="${MODULE_NAME}_serverless_scaffold"
echo ">>> Scaffolding $SCAFFOLD_DIR/ around $SOURCE_PY"
rm -rf "$SCAFFOLD_DIR"
mkdir -p "$SCAFFOLD_DIR/src/$MODULE_NAME"
cp "$SOURCE_PY" "$SCAFFOLD_DIR/src/$MODULE_NAME/definitions.py"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/__init__.py"

# ── pyproject.toml ──
{
    cat <<PYPROJ
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
PYPROJ
    for dep in $EXTRA_DEPS; do
        echo "    \"$dep\","
    done
    cat <<'PYPROJ'
]

[tool.hatch.metadata]
allow-direct-references = true

PYPROJ
    cat <<PYPROJ
[tool.hatch.build.targets.wheel]
packages = ["src/$MODULE_NAME"]

[tool.dg]
directory_type = "project"

[tool.dg.project]
root_module = "$MODULE_NAME"
PYPROJ
} > "$SCAFFOLD_DIR/pyproject.toml"

# ── dagster_cloud.yaml ──
cat > "$SCAFFOLD_DIR/dagster_cloud.yaml" <<EOF
locations:
  - location_name: $LOCATION_NAME
    code_source:
      module_name: $MODULE_NAME.definitions
EOF

echo "✓ Scaffold ready: $SCAFFOLD_DIR/"
echo "    src/$MODULE_NAME/definitions.py   ← your $SOURCE_PY"
echo "    pyproject.toml                    (deps: dagster, dagster-cloud$([ -n "$EXTRA_DEPS" ] && echo -n ", $EXTRA_DEPS"))"
echo "    dagster_cloud.yaml                (location_name=$LOCATION_NAME)"
echo ""

DEPLOY_CMD="uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . --location-name $LOCATION_NAME --module-name $MODULE_NAME.definitions --python-version $PYTHON_VERSION"

if [ -n "$DRY_RUN" ]; then
    echo "(dry-run) to deploy:"
    echo "  cd $SCAFFOLD_DIR"
    echo "  $DEPLOY_CMD"
    exit 0
fi

echo ">>> Deploying to Dagster+ Serverless..."
cd "$SCAFFOLD_DIR"
$DEPLOY_CMD

if [ -z "$KEEP_SCAFFOLD" ]; then
    echo ""
    echo ">>> Cleaning up scaffold (--keep-scaffold to preserve for iteration)"
    cd ..
    rm -rf "$SCAFFOLD_DIR"
fi
