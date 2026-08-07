#!/usr/bin/env bash
# dg-deploy — unified single-file deploy to Dagster+ Serverless OR Hybrid.
#
# Serverless (default): pex bundle, no Docker, uploaded to Dagster+.
# Hybrid: git-push your .py to a flows repo the runner container watches.
#
# Both paths deliver "one .py file → live Dagster+ code location" in a
# single command, matching Prefect's Method 2 ergonomics.
#
# Usage:
#
#   # Serverless — auto-detects deps from imports, builds pex, deploys.
#   ./dg-deploy my_flow.py
#   ./dg-deploy my_flow.py --location-name my-flow
#
#   # Hybrid path A — .py lives INSIDE a git repo (most Prefect-like).
#   cd my-flows-repo/
#   ./dg-deploy my_flow.py --hybrid
#     # detects .git/, adds flows/my_flow.py, git commit + push, triggers reload
#
#   # Hybrid path B — .py somewhere, push to an existing flows repo.
#   ./dg-deploy ~/scratch/my_flow.py --hybrid \
#       --flows-repo eric-thomas-dagster/my-flows
#
#   # Hybrid path C — create a fresh flows repo + push.
#   ./dg-deploy my_flow.py --hybrid \
#       --create-repo eric-thomas-dagster/my-new-flows-repo
#
# Options:
#   --hybrid                  Deploy via Hybrid runner (default: Serverless)
#   --location-name NAME      Serverless: location name (default: basename .py)
#                             Hybrid: name of the runner code location (default: hybrid_git_runner)
#   --flows-repo USER/REPO    Hybrid: target flows repo (required if not cwd)
#   --create-repo USER/REPO   Hybrid: create a new public repo + use as --flows-repo
#   --flows-branch BRANCH     Hybrid: target branch (default: main)
#   --scripts-dir DIR         Hybrid: subdir inside flows repo (default: flows)
#   --runner-image URL        Hybrid: override runner image (default: public prebuilt)
#   --deployment NAME         Dagster+ deployment (default: prod)
#   --deps 'pkg1 pkg2'        Serverless: extra deps to pyproject.toml
#   --no-auto-deps            Serverless: skip import parsing
#   --python-version VER      Serverless: (default: 3.12)
#   --dry-run                 Print commands, don't execute
#
# Requires:
#   - `dagster-cloud config setup` done once (org + deployment + token cached)
#   - For --hybrid: gh CLI authenticated (git push uses your gh credential helper)

set -eo pipefail

DEFAULT_RUNNER_IMAGE="ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest"

SOURCE_PY=""
HYBRID=""
LOCATION_NAME=""
FLOWS_REPO=""
CREATE_REPO=""
FLOWS_BRANCH="main"
SCRIPTS_DIR="flows"
RUNNER_IMAGE="$DEFAULT_RUNNER_IMAGE"
DEPLOYMENT=""
EXTRA_DEPS=""
FLOWS_DEPS=""
AUTO_DEPS=1
DRY_RUN=""
PYTHON_VERSION="3.12"

# First positional arg is the .py path.
if [ $# -gt 0 ] && [ "${1:0:2}" != "--" ]; then
    SOURCE_PY="$1"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --hybrid) HYBRID=1; shift ;;
        --location-name) LOCATION_NAME="$2"; shift 2 ;;
        --flows-repo) FLOWS_REPO="$2"; shift 2 ;;
        --create-repo) CREATE_REPO="$2"; shift 2 ;;
        --flows-branch) FLOWS_BRANCH="$2"; shift 2 ;;
        --scripts-dir) SCRIPTS_DIR="$2"; shift 2 ;;
        --runner-image) RUNNER_IMAGE="$2"; shift 2 ;;
        --deployment) DEPLOYMENT="$2"; shift 2 ;;
        --deps) EXTRA_DEPS="$2"; shift 2 ;;
        --flows-deps) FLOWS_DEPS="$2"; shift 2 ;;
        --no-auto-deps) AUTO_DEPS=0; shift ;;
        --python-version) PYTHON_VERSION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) grep '^#' "$0" | cut -c 3-; exit 0 ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$SOURCE_PY" ] || [ ! -f "$SOURCE_PY" ]; then
    echo "usage: $0 path/to/file.py [options]"
    echo "  Serverless (default): $0 my_flow.py"
    echo "  Hybrid:               $0 my_flow.py --hybrid [--flows-repo user/repo | --create-repo user/repo]"
    exit 1
fi

# ── Serverless path — delegate to dg_deploy_one_file.sh ─────────────
if [ -z "$HYBRID" ]; then
    echo ">>> Serverless deploy (pex, no Docker)"
    HERE_DIR="$(dirname "$0")"
    ARGS=("$SOURCE_PY")
    [ -n "$LOCATION_NAME" ] && ARGS+=("--location-name" "$LOCATION_NAME")
    [ -n "$EXTRA_DEPS" ] && ARGS+=("--deps" "$EXTRA_DEPS")
    [ "$AUTO_DEPS" = "0" ] && ARGS+=("--no-auto-deps")
    [ -n "$DRY_RUN" ] && ARGS+=("--dry-run")
    ARGS+=("--python-version" "$PYTHON_VERSION")
    exec bash "$HERE_DIR/dg_deploy_one_file.sh" "${ARGS[@]}"
fi

# ── Hybrid path ─────────────────────────────────────────────────────
echo ">>> Hybrid deploy (git-push flow to runner-watched repo)"
if ! command -v gh >/dev/null 2>&1; then
    echo "✗ gh CLI required for --hybrid (git-repo operations)"
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "✗ git required for --hybrid"
    exit 1
fi

BASENAME=$(basename "$SOURCE_PY" .py)
[ -z "$LOCATION_NAME" ] && LOCATION_NAME="hybrid_git_runner"

# Resolve flows repo. Three paths:
#   1. --create-repo → create it, then treat as --flows-repo
#   2. --flows-repo  → use it (clone temp, add, push)
#   3. no flag       → detect cwd git repo (add + push in place)
if [ -n "$CREATE_REPO" ]; then
    echo ">>> Creating flows repo $CREATE_REPO..."
    if [ -z "$DRY_RUN" ]; then
        gh repo create "$CREATE_REPO" --public --confirm >/dev/null 2>&1 || {
            echo "  (repo may already exist — continuing)"
        }
    fi
    FLOWS_REPO="$CREATE_REPO"
fi

if [ -n "$FLOWS_REPO" ]; then
    # Path B — clone target repo to temp dir + push flow to it
    WORK=$(mktemp -d)
    trap "rm -rf $WORK" EXIT
    REPO_URL="https://github.com/$FLOWS_REPO.git"
    echo ">>> Cloning $REPO_URL to a temp dir"
    if [ -z "$DRY_RUN" ]; then
        gh repo clone "$FLOWS_REPO" "$WORK/repo" 2>&1 | tail -3
        # If repo is fresh (just created), init it with a first commit
        cd "$WORK/repo"
        if [ ! -f README.md ] && [ -z "$(ls -A .)" ]; then
            echo "# Dagster+ Hybrid flows" > README.md
            git add README.md
            git -c user.email=none@local -c user.name=dg-deploy commit -m "initial commit" 2>&1 | tail -2
        fi
        mkdir -p "$SCRIPTS_DIR"
        cp "$SOURCE_PY" "$SCRIPTS_DIR/"
        # If --flows-deps given, append to requirements.txt at repo root
        if [ -n "$FLOWS_DEPS" ]; then
            for pkg in $FLOWS_DEPS; do
                echo "$pkg" >> requirements.txt
            done
            # De-dup while preserving order (keep last occurrence of each package name)
            awk '!seen[$0]++' requirements.txt > requirements.txt.tmp && mv requirements.txt.tmp requirements.txt
            git add requirements.txt
        fi
        git add "$SCRIPTS_DIR/$BASENAME.py"
        git -c user.email=none@local -c user.name=dg-deploy commit -m "add $BASENAME via dg-deploy" 2>&1 | tail -2
        git push origin "$FLOWS_BRANCH" 2>&1 | tail -3
    else
        echo "  (dry-run: would clone, cp $SOURCE_PY to $SCRIPTS_DIR/, commit + push to $FLOWS_BRANCH)"
    fi
    cd - >/dev/null
else
    # Path A — detect cwd git repo + add + push in place
    if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "✗ Not inside a git repo. Options:"
        echo "  - cd into your flows repo and re-run"
        echo "  - pass --flows-repo user/repo (existing repo)"
        echo "  - pass --create-repo user/new-repo (create fresh)"
        exit 1
    fi
    REPO_ROOT=$(git rev-parse --show-toplevel)
    # If not already there, copy the .py into the flows subdir.
    if [ ! -f "$REPO_ROOT/$SCRIPTS_DIR/$BASENAME.py" ]; then
        mkdir -p "$REPO_ROOT/$SCRIPTS_DIR"
        cp "$SOURCE_PY" "$REPO_ROOT/$SCRIPTS_DIR/"
    fi
    ORIGIN=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || echo "")
    if [ -z "$ORIGIN" ]; then
        echo "✗ Repo has no 'origin' remote configured."
        exit 1
    fi
    # Derive USER/REPO from origin URL.
    FLOWS_REPO=$(echo "$ORIGIN" | sed -E 's|.*github\.com[:/]([^/]+/[^/]+)(\.git)?$|\1|')
    echo ">>> Using current-cwd flows repo: $FLOWS_REPO"
    if [ -z "$DRY_RUN" ]; then
        cd "$REPO_ROOT"
        # If --flows-deps given, append to requirements.txt at repo root
        if [ -n "$FLOWS_DEPS" ]; then
            for pkg in $FLOWS_DEPS; do
                echo "$pkg" >> requirements.txt
            done
            awk '!seen[$0]++' requirements.txt > requirements.txt.tmp && mv requirements.txt.tmp requirements.txt
            git add requirements.txt
        fi
        git add "$SCRIPTS_DIR/$BASENAME.py"
        git -c user.email=none@local -c user.name=dg-deploy commit -m "add $BASENAME via dg-deploy" 2>&1 | tail -2
        git push origin "$FLOWS_BRANCH" 2>&1 | tail -3
        cd - >/dev/null
    else
        echo "  (dry-run: would git add $SCRIPTS_DIR/$BASENAME.py + commit + push)"
    fi
fi

# ── Deploy or reload the runner code location ───────────────────────
echo ""
echo ">>> Deploying / refreshing runner location: $LOCATION_NAME"
echo "    Image:       $RUNNER_IMAGE"
echo "    Flows repo:  https://github.com/$FLOWS_REPO"
echo "    Branch:      $FLOWS_BRANCH"
echo "    Scripts dir: $SCRIPTS_DIR"

DEPLOY_ARGS=(
    "serverless" "deploy-docker" "."
    "--image" "$RUNNER_IMAGE"
    "--location-name" "$LOCATION_NAME"
    "--env" "SCRIPTS_REPO_URL=https://github.com/$FLOWS_REPO"
    "--env" "SCRIPTS_REPO_BRANCH=$FLOWS_BRANCH"
    "--env" "SCRIPTS_DIR=$SCRIPTS_DIR"
)
[ -n "$DEPLOYMENT" ] && DEPLOY_ARGS+=("--deployment" "$DEPLOYMENT")

if [ -n "$DRY_RUN" ]; then
    echo "(dry-run) to deploy:"
    echo "  uvx --with pex --from dagster-cloud-cli dagster-cloud ${DEPLOY_ARGS[*]}"
    exit 0
fi

# `deploy-docker` needs a project dir to derive the location name + module.
# We use the hybrid_git_runner project as the source-of-truth for the
# runner shape. First run will register the location; subsequent runs will
# just update it.
HYBRID_PROJECT="$(dirname "$0")/../hybrid_git_runner"
if [ ! -d "$HYBRID_PROJECT" ]; then
    echo "✗ Cannot find hybrid_git_runner project at $HYBRID_PROJECT"
    echo "  This CLI expects to live in examples/lib/ alongside examples/hybrid_git_runner/."
    exit 1
fi

(cd "$HYBRID_PROJECT" && uvx --with pex --from dagster-cloud-cli dagster-cloud "${DEPLOY_ARGS[@]}")

echo ""
echo "✓ Live. Your flow is at $FLOWS_REPO/$SCRIPTS_DIR/$BASENAME.py"
echo "  The runner ($LOCATION_NAME) will pick it up on next code-location load."
echo "  Iterate: edit $BASENAME.py locally, run dg-deploy again — same one command."
