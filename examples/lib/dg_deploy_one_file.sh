#!/usr/bin/env bash
# dg_deploy_one_file.sh — Prefect-style "one .py file → Dagster+ Serverless" deploy.
#
# Takes a single .py file with `defs = dg.Definitions(...)` at module scope,
# scaffolds the minimum Dagster project layout around it (pyproject.toml +
# dagster_cloud.yaml + src/<name>/), and deploys to Dagster+ Serverless.
#
# Usage:
#
#   ./dg_deploy_one_file.sh path/to/my_flow.py                        # simplest form
#   ./dg_deploy_one_file.sh path/to/my_flow.py --location-name my-flow
#   ./dg_deploy_one_file.sh --from user/repo/path/to/my_flow.py       # pull from GitHub main branch
#   ./dg_deploy_one_file.sh path/to/my_flow.py --deps 'pandas litellm' # explicit extra deps
#
# By default we auto-detect deps by parsing the .py for top-level `import`
# statements + filtering the stdlib. Pass --no-auto-deps to skip.
#
# Assumes you've already done a one-time `dagster-cloud config setup` so the
# org + deployment + user token are cached in ~/.config/dagster_cloud/.
#
# Options:
#   --location-name NAME     (default: basename of .py file)
#   --from USER/REPO/PATH    (fetch .py from https://raw.githubusercontent.com/USER/REPO/main/PATH)
#   --branch BRANCH          (used with --from; default: main)
#   --deps 'pkg1 pkg2'       (extra deps in addition to auto-detected ones)
#   --no-auto-deps           (disable import-based auto-detection)
#   --python-version VER     (default: 3.12)
#   --dry-run                (scaffold + print deploy command, don't run it)
#   --keep-scaffold          (don't rm -rf the scaffold dir after deploy)

set -eo pipefail

SOURCE_PY=""
GITHUB_SPEC=""
BRANCH="main"
LOCATION_NAME=""
EXTRA_DEPS=""
AUTO_DEPS=1
DRY_RUN=""
KEEP_SCAFFOLD=""
PYTHON_VERSION="3.12"

# First positional arg is the .py path (unless --from provided).
if [ $# -gt 0 ] && [ "${1:0:2}" != "--" ]; then
    SOURCE_PY="$1"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --location-name) LOCATION_NAME="$2"; shift 2 ;;
        --from) GITHUB_SPEC="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --deps) EXTRA_DEPS="$2"; shift 2 ;;
        --no-auto-deps) AUTO_DEPS=0; shift ;;
        --python-version) PYTHON_VERSION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-scaffold) KEEP_SCAFFOLD=1; shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

# --from USER/REPO/PATH → fetch from GitHub raw + treat as SOURCE_PY.
if [ -n "$GITHUB_SPEC" ]; then
    # Parse USER/REPO/PATH into components.
    USER_REPO="${GITHUB_SPEC%%/*}"
    REST="${GITHUB_SPEC#*/}"
    REPO="${REST%%/*}"
    PATH_IN_REPO="${REST#*/}"
    URL="https://raw.githubusercontent.com/$USER_REPO/$REPO/$BRANCH/$PATH_IN_REPO"

    echo ">>> Fetching $URL"
    FETCHED_FILE=".dg_deploy_fetched_$(basename "$PATH_IN_REPO")"
    if ! curl -sfL -o "$FETCHED_FILE" "$URL"; then
        echo "✗ Failed to fetch $URL (bad user/repo/path? branch?)"
        exit 1
    fi
    SOURCE_PY="$FETCHED_FILE"
fi

if [ -z "$SOURCE_PY" ] || [ ! -f "$SOURCE_PY" ]; then
    echo "usage: $0 path/to/file.py [options]"
    echo "  or:  $0 --from USER/REPO/PATH [options]"
    echo "run with --help-style options: --location-name / --deps / --dry-run / --keep-scaffold"
    exit 1
fi

BASENAME=$(basename "$SOURCE_PY" .py)
# Strip the ".dg_deploy_fetched_" prefix if we fetched from GitHub.
BASENAME="${BASENAME#.dg_deploy_fetched_}"
# Sanitize for use as a Python module name (kebab → snake).
MODULE_NAME=$(echo "$BASENAME" | tr '-' '_')
[ -z "$LOCATION_NAME" ] && LOCATION_NAME="$BASENAME"

# ── Auto-detect deps from `import` statements ──
AUTO_DETECTED_DEPS=""
if [ "$AUTO_DEPS" = "1" ]; then
    if command -v python3 >/dev/null 2>&1; then
        AUTO_DETECTED_DEPS=$(python3 - <<PYEOF "$SOURCE_PY"
"""Parse a .py file, extract top-level import statements, filter stdlib,
emit non-stdlib top-level module names (one per line). Uses Python 3.10+
sys.stdlib_module_names.
"""
import ast, sys

# Modules we always exclude even if not in stdlib_module_names (e.g. dagster
# is already a base dep of the scaffold, no need to re-declare).
BASE_DEPS = {"dagster", "dagster_cloud", "dagster_cloud_cli"}

# Common import → pip package name mapping (add as needed).
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

path = sys.argv[1]
try:
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
except SyntaxError:
    sys.exit(0)  # skip auto-detect if the file is malformed

stdlib = getattr(sys, "stdlib_module_names", set())
found = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for n in node.names:
            top = n.name.split(".")[0]
            found.add(top)
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.level == 0:
            top = node.module.split(".")[0]
            found.add(top)

extern = sorted(m for m in found if m not in stdlib and m not in BASE_DEPS and not m.startswith("_"))
for m in extern:
    print(IMPORT_TO_PIP.get(m, m))
PYEOF
        )
    fi
fi

# Combine auto-detected + explicit --deps.
# `|| true` because grep -v '^$' returns 1 when the input is empty
# (no deps detected + no --deps), and set -eo pipefail would kill us.
COMBINED_DEPS=$(printf '%s\n%s\n' "$AUTO_DETECTED_DEPS" "$EXTRA_DEPS" | tr ' ' '\n' | { grep -v '^$' || true; } | sort -u)

SCAFFOLD_DIR="${MODULE_NAME}_serverless_scaffold"
echo ">>> Scaffolding $SCAFFOLD_DIR/ around $SOURCE_PY"
rm -rf "$SCAFFOLD_DIR"
mkdir -p "$SCAFFOLD_DIR/src/$MODULE_NAME"
cp "$SOURCE_PY" "$SCAFFOLD_DIR/src/$MODULE_NAME/definitions.py"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/__init__.py"

# Clean up the fetched file (if we downloaded one).
[ -n "$GITHUB_SPEC" ] && rm -f "$SOURCE_PY"

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
    if [ -n "$COMBINED_DEPS" ]; then
        while IFS= read -r dep; do
            [ -n "$dep" ] && echo "    \"$dep\","
        done <<< "$COMBINED_DEPS"
    fi
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
if [ -n "$COMBINED_DEPS" ]; then
    echo "    pyproject.toml                    (deps: dagster, dagster-cloud,"
    while IFS= read -r dep; do
        [ -n "$dep" ] && echo "                                       $dep,"
    done <<< "$COMBINED_DEPS"
    echo "                                       )"
else
    echo "    pyproject.toml                    (deps: dagster, dagster-cloud)"
fi
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
