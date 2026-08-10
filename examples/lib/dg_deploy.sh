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
# Or, if the input directory ALREADY has a project config, the wrapper
# uses it instead of scaffolding:
#   - pyproject.toml with [tool.dg.project]  → deploy in place (dg-native)
#   - dagster_cloud.yaml (legacy CLI)        → auto-migrate to
#     [tool.dg.project] + build.yaml (original saved as .legacy-bak),
#     then deploy. Multi-location yaml migrates the FIRST location only.
#
# Uses the modern `dg plus deploy` CLI under the hood (session-based, safe
# by default — no destructive workspace-mirror behavior). Not the legacy
# `dagster-cloud` CLI.
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
#   --deployment NAME        Dagster+ deployment (default: whatever's in ~/.config/dg or $DAGSTER_CLOUD_DEPLOYMENT)
#   --agent-queue NAME       Route location to a specific Hybrid agent queue
#   --deps 'pkg1 pkg2'       Extra deps beyond auto-detected imports
#   --no-auto-deps           Skip import-based auto-detection
#   --python-version VER     (default: 3.12)
#   --dry-run                Print commands, don't execute
#   --keep-scaffold          Don't rm -rf the scaffold after deploy
#
# Options (--hybrid only):
#   --registry URL           REQUIRED. E.g. ghcr.io/user/name or acr/gcr/ecr equivalent.
#
# Requires:
#   - `dg plus login` done once (org + deployment + token cached), or DAGSTER_CLOUD_API_TOKEN + DAGSTER_CLOUD_ORGANIZATION exported.
#   - For --hybrid: local docker + push access to --registry.
#   - For --hybrid: a Hybrid agent serving the target queue. Every code
#     location is aligned to an agent — no matching agent means the location
#     lands in an error state until one comes online. The CLI doesn't deploy
#     agents (they're your infrastructure — Docker / ECS / K8s / Azure). Pick
#     a runtime + follow: https://docs.dagster.io/deployment/dagster-plus/hybrid
#     The wrapper pre-checks and warns if no agent is running for the target
#     queue so you find out before the Docker build burns.

set -eo pipefail

SOURCE=""
HYBRID=""
LOCATION_NAME=""
REGISTRY=""
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

# ── Detect existing project shape ───────────────────────────────────
# Three cases:
#   1. dg-native project (pyproject.toml with [tool.dg.project]) → deploy in place
#   2. Legacy dagster_cloud.yaml project (no [tool.dg.project]) → migrate then deploy
#   3. Loose .py file(s) → scaffold + deploy (default)
INPLACE=""
INPLACE_DIR=""
if [ -d "$SOURCE" ]; then
    if [ -f "$SOURCE/pyproject.toml" ] && grep -qE '^\[tool\.dg\.project\]' "$SOURCE/pyproject.toml"; then
        INPLACE="dg-native"
        INPLACE_DIR="$SOURCE"
    elif [ -f "$SOURCE/dagster_cloud.yaml" ]; then
        INPLACE="legacy"
        INPLACE_DIR="$SOURCE"
    fi
fi

# ── Determine input shape (for the scaffold path) ──────────────────
INPUT_FILES=()
if [ -z "$INPLACE" ]; then
    if [ -d "$SOURCE" ]; then
        while IFS= read -r f; do
            INPUT_FILES+=("$f")
        done < <(find "$SOURCE" -maxdepth 1 -type f -name "*.py" | sort)
        if [ ${#INPUT_FILES[@]} -eq 0 ]; then
            echo "✗ No .py files found in $SOURCE (and no pyproject.toml or dagster_cloud.yaml either)"
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
fi

# ── In-place branches skip the scaffold ───────────────────────────
if [ "$INPLACE" = "dg-native" ]; then
    echo ">>> Detected existing dg-native project at $INPLACE_DIR"
    echo "    Using it as-is (pyproject.toml [tool.dg.project] found)"
    if [ -n "$HYBRID" ]; then
        # Ensure build.yaml exists with the registry the user asked for.
        if [ -f "$INPLACE_DIR/build.yaml" ]; then
            EXISTING_REG=$(grep -E '^registry:' "$INPLACE_DIR/build.yaml" | head -1 | awk '{print $2}' | tr -d '"' || echo "")
            if [ -n "$EXISTING_REG" ] && [ "$EXISTING_REG" != "$REGISTRY" ]; then
                echo "⚠ build.yaml registry is '$EXISTING_REG', --registry was '$REGISTRY' → using existing"
            fi
        else
            echo "    build.yaml missing — writing one with registry: $REGISTRY"
            echo "registry: $REGISTRY" > "$INPLACE_DIR/build.yaml"
        fi
    fi
    SCAFFOLD_DIR="$INPLACE_DIR"
    KEEP_SCAFFOLD=1  # never rm -rf a user's own project

elif [ "$INPLACE" = "legacy" ]; then
    echo ">>> Detected legacy dagster_cloud.yaml at $INPLACE_DIR"
    echo "    Migrating to modern [tool.dg.project] + build.yaml pattern…"
    # Backup first — never mutate user files without a safety net.
    cp "$INPLACE_DIR/dagster_cloud.yaml" "$INPLACE_DIR/dagster_cloud.yaml.legacy-bak"
    # Translate.
    _MIGRATED=$(uvx --with pyyaml --quiet python - "$INPLACE_DIR" "$REGISTRY" <<'PYEOF'
"""Read legacy dagster_cloud.yaml, translate first location's fields into
[tool.dg.project] block + build.yaml. Prints paths of written/updated
files (one per line) for the shell to display."""
import os, re, sys
import yaml

proj_dir, cli_registry = sys.argv[1], sys.argv[2]
with open(f"{proj_dir}/dagster_cloud.yaml") as f:
    cfg = yaml.safe_load(f) or {}
locations = cfg.get("locations") or []
if not locations:
    print("ERR:no locations found in dagster_cloud.yaml", file=sys.stderr)
    sys.exit(1)
if len(locations) > 1:
    print(f"WARN:{len(locations)} locations found; using only the first ('{locations[0].get('location_name')}')", file=sys.stderr)
loc = locations[0]
location_name = loc.get("location_name") or ""
code_source = loc.get("code_source") or {}
module_name = code_source.get("module_name") or ""
python_file = code_source.get("python_file") or ""
package_name = code_source.get("package_name") or ""
image = loc.get("image") or ""
agent_queue = loc.get("agent_queue") or ""

if not location_name:
    print("ERR:location has no location_name", file=sys.stderr); sys.exit(1)
if not (module_name or python_file or package_name):
    print("ERR:location has no code_source.module_name / python_file / package_name", file=sys.stderr); sys.exit(1)

# Derive root_module. If module_name is "pkg.definitions" → pkg. Else use package_name or a slug of location_name.
if module_name:
    root_module = module_name.split(".")[0]
    target_module = module_name
elif package_name:
    root_module = package_name
    target_module = f"{package_name}.definitions"
else:
    root_module = re.sub(r"[^a-z0-9_]", "_", location_name.lower())
    target_module = None  # python_file case — dg won't handle this cleanly; user must convert

# Append to (or create) pyproject.toml [tool.dg.project].
pyproject_path = f"{proj_dir}/pyproject.toml"
existing = ""
if os.path.exists(pyproject_path):
    with open(pyproject_path) as f: existing = f.read()
if re.search(r"^\[tool\.dg\.project\]", existing, re.MULTILINE):
    print("ERR:[tool.dg.project] already present — this project isn't actually legacy", file=sys.stderr)
    sys.exit(1)

block = ["", "[tool.dg]", 'directory_type = "project"', "", "[tool.dg.project]",
         f'root_module = "{root_module}"',
         f'code_location_name = "{location_name}"']
if target_module: block.append(f'code_location_target_module = "{target_module}"')
if agent_queue:   block.append(f'agent_queue = "{agent_queue}"')
block.append("")
with open(pyproject_path, "a" if existing else "w") as f:
    if not existing:
        f.write('[project]\nname = "%s"\nversion = "0.1.0"\nrequires-python = ">=3.10"\ndependencies = ["dagster>=1.10", "dagster-cloud"]\n' % root_module)
    f.write("\n".join(block))
print(f"UPDATED:{pyproject_path}")

# build.yaml — extract registry from `image` field (image = registry:tag) or use --registry.
registry = ""
if image and ":" in image:
    registry = image.rsplit(":", 1)[0]
elif image:
    registry = image
elif cli_registry:
    registry = cli_registry
if registry:
    build_path = f"{proj_dir}/build.yaml"
    with open(build_path, "w") as f:
        f.write(f"registry: {registry}\n")
    print(f"WROTE:{build_path}")

# Rename original.
os.rename(f"{proj_dir}/dagster_cloud.yaml", f"{proj_dir}/dagster_cloud.yaml.legacy-bak")
print(f"BACKED_UP:{proj_dir}/dagster_cloud.yaml → dagster_cloud.yaml.legacy-bak")
if python_file and not target_module:
    print("WARN:legacy used python_file — dg needs code_location_target_module. Edit pyproject.toml manually to point at your module.", file=sys.stderr)
PYEOF
)
    _MIG_STATUS=$?
    echo "$_MIGRATED"
    if [ "$_MIG_STATUS" -ne 0 ]; then
        # Restore backup on failure.
        [ -f "$INPLACE_DIR/dagster_cloud.yaml.legacy-bak" ] && mv "$INPLACE_DIR/dagster_cloud.yaml.legacy-bak" "$INPLACE_DIR/dagster_cloud.yaml"
        echo "✗ Migration failed — restored original dagster_cloud.yaml. Fix the issue and rerun."
        exit 1
    fi
    SCAFFOLD_DIR="$INPLACE_DIR"
    KEEP_SCAFFOLD=1
fi

# ── Auto-detect deps from imports (scaffold path only) ─────────────
AUTO_DETECTED_DEPS=""
if [ -z "$INPLACE" ] && [ "$AUTO_DEPS" = "1" ] && command -v python3 >/dev/null 2>&1; then
    AUTO_DETECTED_DEPS=$(python3 - "${INPUT_FILES[@]}" <<'PYEOF'
"""Parse .py files, extract top-level import statements, filter stdlib,
emit non-stdlib top-level module names (one per line)."""
import ast, sys

BASE_DEPS = {"dagster", "dagster_cloud", "dagster_cloud_cli", "dagster_dg_cli"}
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

# ── Scaffold (loose-file path only) ────────────────────────────────
if [ -z "$INPLACE" ]; then
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

# pyproject.toml — `dg plus deploy` reads [tool.dg.project] (code
# location + defs module), and [tool.dg.build] (registry for Hybrid).
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
code_location_target_module = "$MODULE_NAME.definitions"
code_location_name = "$LOCATION_NAME"
TOMLEOF
    [ -n "$AGENT_QUEUE" ] && echo "agent_queue = \"$AGENT_QUEUE\""
} > "$SCAFFOLD_DIR/pyproject.toml"

# build.yaml (Hybrid only) — `dg plus deploy` reads registry + Dockerfile
# location from here. Not pyproject.toml.
if [ -n "$HYBRID" ]; then
    cat > "$SCAFFOLD_DIR/build.yaml" <<YAMLEOF
registry: $REGISTRY
YAMLEOF
fi

# Dockerfile (Hybrid only) — sits at project root, `dg plus deploy` finds it.
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
if [ -n "$HYBRID" ]; then
    echo "    build.yaml"
    echo "    Dockerfile"
fi
if [ -n "$COMBINED_DEPS" ]; then
    echo "    deps: dagster, dagster-cloud,"
    while IFS= read -r dep; do [ -n "$dep" ] && echo "          $dep"; done <<< "$COMBINED_DEPS"
fi
echo ""
fi  # end scaffold-only block

# ── Auth: forward legacy dagster_cloud config to dg if needed ──────
# `dg plus deploy` looks at ~/.dagster_cloud_cli/config or ~/.config/dg.toml.
# If the user still has the older ~/.config/dagster_cloud/config.yaml (from
# the legacy `dagster-cloud` CLI), read it once and export the values as env
# vars so we don't force a config migration. Skipped if DAGSTER_CLOUD_API_TOKEN
# is already exported.
LEGACY_CFG="$HOME/.config/dagster_cloud/config.yaml"
if [ -z "$DAGSTER_CLOUD_API_TOKEN" ] && [ -f "$LEGACY_CFG" ] && command -v python3 >/dev/null 2>&1; then
    eval "$(python3 - "$LEGACY_CFG" <<'PYEOF'
import sys, re, shlex
try:
    text = open(sys.argv[1]).read()
except Exception:
    sys.exit(0)
def pick(key):
    m = re.search(rf'^{key}\s*:\s*(.+)$', text, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else None
tok = pick("api_token") or pick("user_token")
org = pick("organization")
dep = pick("deployment") or pick("default_deployment")
url = pick("url")
if tok: print(f"export DAGSTER_CLOUD_API_TOKEN={shlex.quote(tok)}")
if org: print(f"export DAGSTER_CLOUD_ORGANIZATION={shlex.quote(org)}")
if dep: print(f"export _DGDEPLOY_LEGACY_DEPLOYMENT={shlex.quote(dep)}")
if url: print(f"export _DGDEPLOY_LEGACY_URL={shlex.quote(url)}")
PYEOF
    )"
fi

# ── Hybrid agent pre-check ─────────────────────────────────────────
# Code locations in Hybrid land on whichever agent picks up their queue.
# If no Hybrid agent is running, the image gets pushed + the location gets
# registered, but nothing runs it. Query the deployment before we start
# 5 minutes of Docker work — better to warn early than to build+push into
# a void.
if [ -n "$HYBRID" ] && [ -n "$DAGSTER_CLOUD_API_TOKEN" ] && [ -n "$DAGSTER_CLOUD_ORGANIZATION" ] && command -v python3 >/dev/null 2>&1; then
    _AGENT_CHECK_DEPLOYMENT="${DEPLOYMENT:-${_DGDEPLOY_LEGACY_DEPLOYMENT:-prod}}"
    _AGENT_CHECK_URL="${_DGDEPLOY_LEGACY_URL:-https://${DAGSTER_CLOUD_ORGANIZATION}.dagster.plus}"
    _AGENT_STATUS=$(
        DAGSTER_CLOUD_API_TOKEN="$DAGSTER_CLOUD_API_TOKEN" \
        _DEPLOYMENT="$_AGENT_CHECK_DEPLOYMENT" \
        _URL="$_AGENT_CHECK_URL" \
        _WANT_QUEUE="$AGENT_QUEUE" \
        python3 - <<'PYEOF' 2>/dev/null || true
"""Query Dagster+ GraphQL for running non-Serverless agents on the target
deployment. Print one of: OK, NO_AGENTS, QUEUE_MISMATCH."""
import json, os, sys, urllib.request

url = os.environ["_URL"].rstrip("/") + "/" + os.environ["_DEPLOYMENT"] + "/graphql"
req = urllib.request.Request(
    url,
    data=json.dumps({"query": "{ agents { status metadata { key value } } }"}).encode(),
    headers={
        "Content-Type": "application/json",
        "Dagster-Cloud-Api-Token": os.environ["DAGSTER_CLOUD_API_TOKEN"],
    },
)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
except Exception:
    sys.exit(0)  # skip check on any error
agents = (resp.get("data") or {}).get("agents") or []
hybrid_queues = []
for a in agents:
    if a.get("status") != "RUNNING":
        continue
    meta = {m["key"]: m["value"] for m in a.get("metadata", [])}
    typ = meta.get("type", "").strip('"')
    if "Serverless" in typ:  # skip built-in Serverless agent
        continue
    try:
        qs = json.loads(meta.get("queues", "[]"))
        hybrid_queues += qs
    except Exception:
        pass
want = os.environ.get("_WANT_QUEUE", "")
if not hybrid_queues:
    print("NO_AGENTS")
elif want and want not in hybrid_queues:
    print(f"QUEUE_MISMATCH|{','.join(sorted(set(hybrid_queues)))}")
else:
    print(f"OK|{','.join(sorted(set(hybrid_queues)))}")
PYEOF
    )
    case "${_AGENT_STATUS%%|*}" in
        NO_AGENTS)
            echo "⚠ No running Hybrid agent on '$_AGENT_CHECK_DEPLOYMENT'."
            echo "  Every code location is served by an agent — with none running, this"
            echo "  location will be pushed + registered but sit in an error state until"
            echo "  an agent comes online serving its queue."
            echo "  Deploy one before or after this deploy (all runtimes documented here):"
            echo "    https://docs.dagster.io/deployment/dagster-plus/hybrid"
            echo ""
            ;;
        QUEUE_MISMATCH)
            _HAVE_QUEUES="${_AGENT_STATUS#*|}"
            echo "⚠ --agent-queue '$AGENT_QUEUE' isn't served by any running agent."
            echo "  Running Hybrid agent queues: $_HAVE_QUEUES"
            echo "  Location will register but stay in an error state until an agent picks"
            echo "  up '$AGENT_QUEUE'. Either drop --agent-queue (routes to default), or"
            echo "  bring up an agent configured for '$AGENT_QUEUE'."
            echo ""
            ;;
        OK)
            _HAVE_QUEUES="${_AGENT_STATUS#*|}"
            echo "✓ Hybrid agent running (queues: $_HAVE_QUEUES)"
            echo ""
            ;;
    esac
fi

# ── Deploy via `dg plus deploy` ─────────────────────────────────────
# Session-based, safe by default. No destructive workspace-mirror.
DEPLOY_ARGS=(plus deploy -y)
if [ -n "$HYBRID" ]; then
    DEPLOY_ARGS+=(--agent-type hybrid --build-strategy docker)
else
    DEPLOY_ARGS+=(--agent-type serverless --build-strategy python-executable)
fi
DEPLOY_ARGS+=(--python-version "$PYTHON_VERSION")

# Deployment resolution: explicit flag > legacy config > dg's own default.
if [ -n "$DEPLOYMENT" ]; then
    DEPLOY_ARGS+=(--deployment "$DEPLOYMENT")
elif [ -n "$_DGDEPLOY_LEGACY_DEPLOYMENT" ]; then
    DEPLOY_ARGS+=(--deployment "$_DGDEPLOY_LEGACY_DEPLOYMENT")
fi

# We shell out via `uvx --from dagster-dg-cli dg` so the caller doesn't
# need dg preinstalled. Same UX shape as before. For Serverless we also
# `--with pex` because dg shells out to `python -m pex` when building the
# pex bundle (`dg plus deploy --build-strategy python-executable`).
DG_INVOCATION=(uvx --from dagster-dg-cli)
[ -z "$HYBRID" ] && DG_INVOCATION+=(--with pex)
DG_INVOCATION+=(dg)

if [ -n "$DRY_RUN" ]; then
    echo "(dry-run) to deploy:"
    echo "  cd $SCAFFOLD_DIR && ${DG_INVOCATION[*]} ${DEPLOY_ARGS[*]}"
    exit 0
fi

if [ -n "$HYBRID" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "✗ docker not found. --hybrid needs Docker to build+push the image."
        echo "  Install Docker Desktop / colima / podman."
        exit 1
    fi
    echo ">>> Deploying to Dagster+ Hybrid via \`dg plus deploy\` (docker build+push+register)…"
else
    echo ">>> Deploying to Dagster+ Serverless via \`dg plus deploy\` (pex build+upload)…"
fi

(cd "$SCAFFOLD_DIR" && "${DG_INVOCATION[@]}" "${DEPLOY_ARGS[@]}")

echo ""
if [ -z "$KEEP_SCAFFOLD" ]; then
    echo ">>> Cleaning up scaffold (--keep-scaffold to preserve for iteration)"
    rm -rf "$SCAFFOLD_DIR"
else
    echo ">>> Scaffold preserved at $SCAFFOLD_DIR/"
fi

echo "✓ Live. Iterate: edit your .py, run dg-deploy again — same one command."
