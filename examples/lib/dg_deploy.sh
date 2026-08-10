#!/usr/bin/env bash
# dg-deploy — one .py file (or folder) → one Dagster+ code location.
#
# Full CLI reference (rendered markdown): ./dg_deploy.md
# Umbrella deploy concept doc:            ../deploying.md
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
#   # Local dev — same scaffold, no deploy. `dg dev` on http://localhost:3000.
#   ./dg-deploy my_flow.py --dev
#   ./dg-deploy flows/     --dev
#
# Options (common):
#   --dev                    Scaffold + `dg dev` locally instead of deploying (UI at :3000)
#   --location-name NAME     Code location name (default: basename of .py / folder)
#   --deployment NAME        Dagster+ deployment (default: whatever's in ~/.config/dg or $DAGSTER_CLOUD_DEPLOYMENT)
#   --agent-queue NAME       Route location to a specific Hybrid agent queue
#   --deps 'pkg1 pkg2'       Extra deps to add (always appended, on top of any explicit or detected source)
#   --no-auto-deps           Skip AST import parsing when no explicit deps source found
#   --python-version VER     (default: 3.12)
#   --dry-run                Print commands, don't execute
#   --keep-scaffold          Don't rm -rf the scaffold after deploy (implicit with --dev)
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
DEV=""
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

LOCATION_NAME_EXPLICIT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --hybrid) HYBRID=1; shift ;;
        --dev) DEV=1; KEEP_SCAFFOLD=1; shift ;;
        --location-name) LOCATION_NAME="$2"; LOCATION_NAME_EXPLICIT=1; shift 2 ;;
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
        # Recurse up to 5 levels deep — covers realistic multi-file layouts
        # (flat, one-subfolder, small-package-tree) without going wild. Skip
        # __pycache__, hidden dirs (.venv, .git, .tox, …), and __init__.py
        # files (those get regenerated).
        while IFS= read -r f; do
            INPUT_FILES+=("$f")
        done < <(find "$SOURCE" \
            -maxdepth 5 \
            -type f -name "*.py" \
            -not -path '*/__pycache__/*' \
            -not -path '*/.*/*' \
            -not -name '__init__.py' \
            | sort)
        if [ ${#INPUT_FILES[@]} -eq 0 ]; then
            echo "✗ No .py files found in $SOURCE (and no pyproject.toml or dagster_cloud.yaml either)"
            exit 1
        fi
        BASENAME=$(basename "$SOURCE")
        SOURCE_ROOT="$SOURCE"
    else
        INPUT_FILES=("$SOURCE")
        BASENAME=$(basename "$SOURCE" .py)
        SOURCE_ROOT=""
    fi
    # Sanitize name for use as Python module.
    MODULE_NAME=$(echo "$BASENAME" | tr -- '-. ' '___')
    [ -z "$LOCATION_NAME" ] && LOCATION_NAME="$BASENAME"
fi

# ── In-place branches skip the scaffold ───────────────────────────
if [ "$INPLACE" = "dg-native" ]; then
    echo ">>> Detected existing dg-native project at $INPLACE_DIR"
    echo "    Deploying in place (no scaffold, no clobber)."
    # `uvx create-dagster project` doesn't include `dagster-cloud` in the
    # default deps — but the pex/docker build path needs it. Warn early
    # with a clear fix rather than letting the deploy fail 30s in with a
    # cryptic "dagster_cloud package dependency was expected but not
    # found" ValueError from the pex builder.
    if ! grep -qE '(^|[",\s])dagster-cloud[">=<~,\s]|^dagster-cloud$' "$INPLACE_DIR/pyproject.toml"; then
        echo ""
        echo "⚠ pyproject.toml doesn't list \`dagster-cloud\` as a dependency."
        echo "  The pex/docker build for Dagster+ requires it. Add via:"
        echo "    cd $INPLACE_DIR && uv add dagster-cloud"
        echo "  (or hand-edit [project] dependencies to include \"dagster-cloud\")"
        echo "  Then re-run dg-deploy. Not auto-adding — that would mutate your pyproject.toml."
        echo ""
        exit 1
    fi
    if [ -n "$HYBRID" ]; then
        # Ensure build.yaml exists with the registry the user asked for.
        if [ -f "$INPLACE_DIR/build.yaml" ]; then
            EXISTING_REG=$(grep -E '^registry:' "$INPLACE_DIR/build.yaml" | head -1 | awk '{print $2}' | tr -d '"' || echo "")
            if [ -n "$EXISTING_REG" ] && [ "$EXISTING_REG" != "$REGISTRY" ]; then
                echo "⚠ build.yaml registry is '$EXISTING_REG', --registry was '$REGISTRY' → using existing"
            fi
        else
            echo "    ✎ Writing $INPLACE_DIR/build.yaml (registry: $REGISTRY) — only new file created"
            echo "registry: $REGISTRY" > "$INPLACE_DIR/build.yaml"
        fi
    fi
    SCAFFOLD_DIR="$INPLACE_DIR"
    KEEP_SCAFFOLD=1  # never rm -rf a user's own project

elif [ "$INPLACE" = "legacy" ]; then
    echo ">>> Detected legacy dagster_cloud.yaml at $INPLACE_DIR"
    echo "    MUTATING your files (see summary below):"
    echo "      ✎ pyproject.toml   (append [tool.dg] + [tool.dg.project] block)"
    echo "      ✎ build.yaml       (write — new file)"
    echo "      ↻ dagster_cloud.yaml → dagster_cloud.yaml.legacy-bak  (renamed, backup preserved)"
    echo "    On any failure the .legacy-bak is restored automatically."
    echo ""
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

# ── Dep resolution: explicit files first, AST fallback ─────────────
# Precedence (highest → lowest):
#   1. --deps 'pkg1 pkg2'                    (always additive on top)
#   2. requirements.txt in the input dir     (authoritative — skip AST)
#   3. pyproject.toml [project] dependencies (authoritative — skip AST)
#   4. AST auto-detection                    (fallback)
# Explicit sources are preferred because they carry version pins the AST
# can't produce and match what the user has already declared.
EXPLICIT_DEPS=""
DEPS_SOURCE=""
if [ -z "$INPLACE" ]; then
    if [ -d "$SOURCE" ]; then
        DEP_SEARCH_DIR="$SOURCE"
    else
        DEP_SEARCH_DIR="$(dirname "$SOURCE")"
    fi
    if [ -f "$DEP_SEARCH_DIR/requirements.txt" ]; then
        # Read requirements.txt: strip comments + blanks + inline-# comments
        # + `-r other.txt` recurse directives (unsupported here — user should
        # inline). Preserve version pins as-is.
        EXPLICIT_DEPS=$(sed -E 's/[[:space:]]*#.*$//' "$DEP_SEARCH_DIR/requirements.txt" \
            | grep -vE '^\s*(-r|-c|--|$)' \
            | sed -E 's/^[[:space:]]+|[[:space:]]+$//g' \
            | grep -v '^$' \
            | sort -u)
        DEPS_SOURCE="requirements.txt at $DEP_SEARCH_DIR/"
    elif [ -f "$DEP_SEARCH_DIR/pyproject.toml" ] && command -v python3 >/dev/null 2>&1; then
        EXPLICIT_DEPS=$(python3 - "$DEP_SEARCH_DIR/pyproject.toml" <<'PYEOF' 2>/dev/null || true
"""Extract [project] dependencies from pyproject.toml. Requires tomllib
(Python 3.11+) or the tomli fallback; silently no-ops if neither."""
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit(0)
try:
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)
BASE = {"dagster", "dagster-cloud"}
for dep in data.get("project", {}).get("dependencies", []) or []:
    # Skip the baseline (we always add these); rest pass through with pins.
    name = dep.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0].split(";")[0].strip().lower()
    if name not in BASE:
        print(dep)
PYEOF
        )
        [ -n "$EXPLICIT_DEPS" ] && DEPS_SOURCE="pyproject.toml [project.dependencies] at $DEP_SEARCH_DIR/"
    fi
    if [ -n "$EXPLICIT_DEPS" ]; then
        echo ">>> Using explicit deps from $DEPS_SOURCE — skipping AST auto-detect"
    fi
fi

# AST auto-detect: only if no explicit source AND --no-auto-deps not set.
AUTO_DETECTED_DEPS=""
if [ -z "$INPLACE" ] && [ -z "$EXPLICIT_DEPS" ] && [ "$AUTO_DEPS" = "1" ] && command -v python3 >/dev/null 2>&1; then
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
COMBINED_DEPS=$(printf '%s\n%s\n%s\n' "$EXPLICIT_DEPS" "$AUTO_DETECTED_DEPS" "$EXTRA_DEPS" | tr ' ' '\n' | { grep -v '^$' || true; } | sort -u)

# ── Warn on version-pin conflicts within COMBINED_DEPS ──────────────
# If two entries specify the same package with different pins (e.g.
# `pandas==1.5.3` and `pandas>=2.0`), pip will error at install time —
# surface the conflict here with a clear message.
if command -v python3 >/dev/null 2>&1; then
    _CONFLICTS=$(python3 - <<PYEOF
import re, sys
deps = """$COMBINED_DEPS""".strip().splitlines()
buckets = {}
for d in deps:
    d = d.strip()
    if not d:
        continue
    m = re.match(r'([A-Za-z0-9_.\-]+)(.*)$', d)
    if not m:
        continue
    name, spec = m.group(1).lower().replace("_", "-"), m.group(2).strip()
    buckets.setdefault(name, set()).add(spec or "(unpinned)")
conflicts = {n: sorted(s) for n, s in buckets.items() if len(s) > 1}
for n, s in conflicts.items():
    print(f"{n}: {s}")
PYEOF
    )
    if [ -n "$_CONFLICTS" ]; then
        echo "⚠ Conflicting deps detected — pip will error at install:"
        echo "$_CONFLICTS" | sed 's/^/    /'
        echo "  Resolve by editing --deps or (in dg-native mode) your pyproject.toml."
        echo ""
    fi
fi

# ── Scaffold (loose-file path only) ────────────────────────────────
if [ -z "$INPLACE" ]; then
# Default scaffold in $TMPDIR (deterministic per module name — same input →
# same location, easy to re-inspect). Only put it in cwd when the user asks
# to iterate on the scaffold (--keep-scaffold) or run `dg dev` locally (--dev).
# Never pollutes the user's git repo unless they opt in.
if [ -n "$KEEP_SCAFFOLD" ]; then
    SCAFFOLD_DIR="./${MODULE_NAME}_scaffold"
else
    SCAFFOLD_DIR="${TMPDIR:-/tmp}/dg-deploy-${MODULE_NAME}"
fi
echo ">>> Scaffolding at $SCAFFOLD_DIR/ from $SOURCE"
rm -rf "$SCAFFOLD_DIR"
mkdir -p "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/__init__.py"
touch "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs/__init__.py"

# Copy user files, preserving relative paths (so nested subfolders survive).
for f in "${INPUT_FILES[@]}"; do
    if [ -n "$SOURCE_ROOT" ]; then
        rel="${f#$SOURCE_ROOT/}"
    else
        rel=$(basename "$f")
    fi
    target="$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs/$rel"
    mkdir -p "$(dirname "$target")"
    cp "$f" "$target"
done
# Every subdirectory under user_defs/ needs __init__.py so it's a proper
# Python subpackage — cross-file imports (from .other import x) and
# pkgutil.walk_packages recursion both depend on this.
find "$SCAFFOLD_DIR/src/$MODULE_NAME/user_defs" -type d -exec touch {}/__init__.py \;

# definitions.py — walks EVERY module under user_defs/ (any depth), merges
# their `defs` objects. Uses walk_packages so nested subfolders are picked
# up automatically; each file gets imported with its full dotted name so
# cross-file imports (from .other import x) resolve correctly.
cat > "$SCAFFOLD_DIR/src/$MODULE_NAME/definitions.py" <<PYEOF
"""Auto-generated by dg-deploy. Walks every .py under user_defs/ (recursively)
and merges their \`defs = dg.Definitions(...)\` module-scope objects into
one Definitions for this code location."""
import importlib
import pkgutil

import dagster as dg

from $MODULE_NAME import user_defs

_defs_list = []
_skipped = []
_errored = []
_prefix = "$MODULE_NAME.user_defs."
for _, name, is_pkg in pkgutil.walk_packages(user_defs.__path__, prefix=_prefix):
    if is_pkg:
        # subpackage __init__.py — usually empty; a Definitions here would
        # still be picked up if present, but the module itself is fine to skip.
        continue
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        _errored.append(f"{name}: {type(exc).__name__}: {exc}")
        continue
    if hasattr(module, "defs") and isinstance(module.defs, dg.Definitions):
        _defs_list.append(module.defs)
    else:
        _skipped.append(name)

if _skipped or _errored:
    import sys
    if _skipped:
        print(f"[dg-deploy] skipped (no \`defs = dg.Definitions(...)\`): {_skipped}", file=sys.stderr)
    if _errored:
        print(f"[dg-deploy] import errors: {_errored}", file=sys.stderr)

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

# ── --dev short-circuit: run `dg dev` locally, no deploy ───────────
if [ -n "$DEV" ]; then
    DG_DEV_INVOCATION=(uvx --from dagster-dg-cli --with dagster --with dagster-webserver dg)
    if [ -n "$DRY_RUN" ]; then
        echo "(dry-run) to run locally:"
        echo "  cd $SCAFFOLD_DIR && ${DG_DEV_INVOCATION[*]} dev"
        exit 0
    fi
    echo ">>> Launching \`dg dev\` locally at $SCAFFOLD_DIR/"
    echo "    UI: http://localhost:3000  (Ctrl-C to stop)"
    echo "    Scaffold preserved — edit files + reload the browser to iterate."
    echo ""
    (cd "$SCAFFOLD_DIR" && "${DG_DEV_INVOCATION[@]}" dev)
    exit 0
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

# NOTE: `dg plus deploy --location-name` SELECTS which locations to
# deploy in a multi-location workspace — it does NOT rename a location.
# In loose-file mode, the location name lives in the scaffold's
# pyproject.toml `[tool.dg.project] code_location_name` which we write.
# In in-place mode, it lives in the user's own pyproject.toml — the
# wrapper can't rename without mutating their file. If they want a
# different name for one deploy, they need to edit code_location_name
# (or use the scaffold path). We warn if a mismatch is detected.
if [ -n "$LOCATION_NAME_EXPLICIT" ] && [ -n "$INPLACE" ]; then
    _EXISTING_LOC=$(grep -E '^code_location_name' "$INPLACE_DIR/pyproject.toml" 2>/dev/null \
        | head -1 | sed -E 's/^code_location_name[[:space:]]*=[[:space:]]*"?([^"]*)"?.*$/\1/')
    if [ -n "$_EXISTING_LOC" ] && [ "$_EXISTING_LOC" != "$LOCATION_NAME" ]; then
        echo "⚠ --location-name '$LOCATION_NAME' can't override the project's own"
        echo "  code_location_name = \"$_EXISTING_LOC\" from pyproject.toml. Edit"
        echo "  pyproject.toml (or use the scaffold path with a loose .py) if you"
        echo "  need a different name. Deploying as '$_EXISTING_LOC'."
        echo ""
    fi
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
# Don't rm INPLACE dirs (dg-native / legacy migration) — those are the user's
# own project directory. Only sweep up the scaffold when we created it.
if [ -z "$KEEP_SCAFFOLD" ] && [ -z "$INPLACE" ]; then
    echo ">>> Cleaning up scaffold at $SCAFFOLD_DIR/  (--keep-scaffold to preserve)"
    rm -rf "$SCAFFOLD_DIR"
elif [ -n "$INPLACE" ]; then
    :  # in-place — nothing to clean, no message needed
else
    echo ">>> Scaffold preserved at $SCAFFOLD_DIR/"
fi

echo "✓ Live. Iterate: edit your .py, run dg-deploy again — same one command."
