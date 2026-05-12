#!/usr/bin/env bash
# Deploy any local demo project to Dagster+ (Serverless or Hybrid auto-detected).
#
# USAGE:
#   ./deploy_to_dagster_plus.sh <project_dir>
#     [--organization X]
#     [--deployment Y]
#     [--build-strategy docker|python-executable]
#     [--git-provider github|gitlab]
#     [--non-interactive]    # skip prompts (CI use)
#     [--with-ci]            # scaffold CI even without prompt
#
# Example (interactive):
#   curl -fsSL .../setup_kitchen_sink_demo.sh | bash
#   curl -fsSL .../deploy_to_dagster_plus.sh | bash -s kitchen-sink-demo
#
# What it does (in order):
#   1. Verifies the project dir is a dg project
#   2. Adds dagster-cloud-cli to dev deps; runs `dg plus login` if not authed
#   3. Asks: scaffold deployment artifacts via `dg plus deploy configure`?
#      → Creates build.yaml, Dockerfile (Hybrid only), .github/workflows/*
#      Auto-detects whether your deployment is Serverless or Hybrid.
#   4. Asks: create a CI API token? → prints once for GitHub secret
#      Optional. The local deploy in step 7 uses your `dg plus login` token,
#      not this one. The CI token is only for GitHub Actions runs.
#   5. Scans your project for env var references (EnvVar, os.environ,
#      *_env_var: X) and prompts you to set values via `dg plus create env`.
#   6. Confirms deployment target.
#   7. Runs `dg plus deploy` (build strategy auto-detected from agent type).
#
# Build strategy is auto-detected — leave --build-strategy unset and dg picks
# PEX for Serverless, Docker for Hybrid. Override if you have a specific reason.

set -euo pipefail

PROJECT_DIR=""
DAGSTER_PLUS_ORG=""
DAGSTER_PLUS_DEPLOYMENT="prod"
BUILD_STRATEGY=""
GIT_PROVIDER=""         # blank = ask interactively (default: github)
GIT_PROVIDER_EXPLICIT=0 # set to 1 if user passed --git-provider
AGENT_TYPE=""           # serverless | hybrid (autodetect if empty)
REGISTRY_URL=""         # required for hybrid
AGENT_PLATFORM=""       # k8s | ecs | docker (hybrid only)
NON_INTERACTIVE=0
WITH_CI=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --organization|--org) DAGSTER_PLUS_ORG="$2"; shift 2;;
    --deployment) DAGSTER_PLUS_DEPLOYMENT="$2"; shift 2;;
    --build-strategy) BUILD_STRATEGY="$2"; shift 2;;
    --git-provider) GIT_PROVIDER="$2"; GIT_PROVIDER_EXPLICIT=1; shift 2;;
    --agent-type) AGENT_TYPE="$2"; shift 2;;
    --registry-url) REGISTRY_URL="$2"; shift 2;;
    --agent-platform) AGENT_PLATFORM="$2"; shift 2;;
    --non-interactive) NON_INTERACTIVE=1; shift;;
    --with-ci) WITH_CI=1; shift;;
    --help|-h)
      grep -E "^#( |$)" "$0" | sed 's/^# \{0,1\}//'
      exit 0;;
    *) PROJECT_DIR="$1"; shift;;
  esac
done

if [ -z "$PROJECT_DIR" ]; then
  echo "Usage: $0 <project_dir> [--organization X] [--deployment Y] [--git-provider github|gitlab] [--non-interactive] [--with-ci]"
  exit 2
fi
if [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: $PROJECT_DIR is not a directory. Run the demo setup first."
  exit 2
fi

cd "$PROJECT_DIR"

if [ ! -f pyproject.toml ] || ! grep -q "tool.dg" pyproject.toml; then
  echo "ERROR: $PROJECT_DIR doesn't look like a Dagster project (no [tool.dg] in pyproject.toml)."
  exit 2
fi

# Interactive prompt that reads from /dev/tty so it works when piped from curl.
ask() {
  local prompt="$1"; local default="${2:-N}"
  if [ "$NON_INTERACTIVE" -eq 1 ] || [ ! -e /dev/tty ]; then
    echo "$default"; return
  fi
  local ans
  echo -n "$prompt " >/dev/tty
  read ans </dev/tty || ans="$default"
  echo "${ans:-$default}"
}
is_yes() { case "$1" in y|Y|yes|YES|Yes) return 0;; *) return 1;; esac }

# ── 1/6: dev deps ─────────────────────────────────────────────────────────────
echo "==> 1/6: Ensuring dagster-cloud-cli is in dev deps"
if ! grep -q "dagster-cloud-cli" pyproject.toml; then
  uv add --dev -q dagster-cloud-cli
  echo "    ✓ added"
else
  echo "    ✓ already present"
fi

# ── 2/6: login ────────────────────────────────────────────────────────────────
echo "==> 2/6: Checking Dagster+ login"
if ! uv run dg plus config view >/dev/null 2>&1; then
  echo "    No saved config. Running 'dg plus login' — opens a browser tab."
  echo "    No account? Free trial: https://dagster.io/plus"
  uv run dg plus login
else
  echo "    ✓ already configured:"
  uv run dg plus config view 2>&1 | sed 's/^/      /'
fi

if [ -z "$DAGSTER_PLUS_ORG" ]; then
  DAGSTER_PLUS_ORG=$(uv run dg plus config view 2>/dev/null | grep -E "^organization" | awk '{print $2}' || true)
fi

# ── 3/6: scaffold deployment artifacts (build.yaml + Dockerfile + CI) ────────
echo "==> 3/6: Deployment artifacts (build.yaml, CI workflows, Dockerfile)"
NEED_CONFIGURE=0
if [ ! -f build.yaml ] && [ ! -f dagster_cloud.yaml ]; then
  NEED_CONFIGURE=1
fi

WANT_CONFIGURE="N"
if [ "$WITH_CI" -eq 1 ] || [ "$NEED_CONFIGURE" -eq 1 ]; then
  WANT_CONFIGURE="y"
else
  WANT_CONFIGURE=$(ask "    Run 'dg plus deploy configure' to scaffold build.yaml + CI workflows? [Y/n]:" "Y")
fi
if is_yes "$WANT_CONFIGURE"; then
  # Ask for git provider if not passed explicitly
  if [ "$GIT_PROVIDER_EXPLICIT" -eq 0 ]; then
    if [ -z "$GIT_PROVIDER" ]; then
      if [ "$NON_INTERACTIVE" -eq 1 ]; then
        GIT_PROVIDER="github"
      else
        # If there's a git remote, sniff it
        DEFAULT_GP="github"
        if [ -f .git/config ] && grep -qi "gitlab" .git/config; then
          DEFAULT_GP="gitlab"
        fi
        GIT_PROVIDER=$(ask "    Git provider for CI workflows [github/gitlab]:" "$DEFAULT_GP")
        if [ "$GIT_PROVIDER" != "github" ] && [ "$GIT_PROVIDER" != "gitlab" ]; then
          echo "    (unknown '$GIT_PROVIDER' — defaulting to github)"
          GIT_PROVIDER="github"
        fi
      fi
    fi
  fi
  # Default to Serverless. The "click-to-deploy from a demo" UX is Serverless-first:
  # Hybrid needs prior infra (agent, registry, credentials) that this script
  # can't bootstrap from scratch. Users who already have Hybrid set up can
  # explicitly pass --agent-type hybrid.
  if [ -z "$AGENT_TYPE" ]; then
    AGENT_TYPE="serverless"
    if [ "$NON_INTERACTIVE" -ne 1 ]; then
      echo ""
      echo "    Defaulting to Serverless agent type."
      echo "    If you have an existing Hybrid agent + registry, re-run with:"
      echo "      $0 $PROJECT_DIR --agent-type hybrid --registry-url <your-registry>"
      echo ""
    fi
  fi

  if [ "$AGENT_TYPE" = "hybrid" ]; then
    # Hybrid needs a container registry + platform.
    if [ -z "$REGISTRY_URL" ]; then
      echo ""
      echo "    Hybrid deployments need a container registry that your agent can pull from."
      echo "    Common forms:"
      echo "      AWS ECR     : 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo"
      echo "      Google GAR  : us-central1-docker.pkg.dev/my-project/my-repo"
      echo "      Azure ACR   : myacr.azurecr.io/my-repo"
      echo "      GitHub GHCR : ghcr.io/my-org/my-repo"
      echo "      Docker Hub  : docker.io/my-user/my-repo"
      echo ""
      REGISTRY_URL=$(ask "    Registry URL:" "")
      if [ -z "$REGISTRY_URL" ]; then
        echo "    ⚠️  No registry URL provided. You'll have to edit build.yaml manually before deploying."
      fi
    fi
    if [ -z "$AGENT_PLATFORM" ]; then
      AGENT_PLATFORM=$(ask "    Agent platform [k8s/ecs/docker]:" "k8s")
    fi

    REG_ARG=""; PLAT_ARG=""
    [ -n "$REGISTRY_URL" ]   && REG_ARG="--registry-url $REGISTRY_URL"
    [ -n "$AGENT_PLATFORM" ] && PLAT_ARG="--agent-platform $AGENT_PLATFORM"
    echo "    Running: dg plus deploy configure hybrid --git-provider $GIT_PROVIDER $REG_ARG $PLAT_ARG"
    # shellcheck disable=SC2086
    uv run dg plus deploy configure hybrid --git-provider "$GIT_PROVIDER" $REG_ARG $PLAT_ARG || true
  else
    # Serverless: just go.
    echo "    Running: dg plus deploy configure serverless --git-provider $GIT_PROVIDER"
    uv run dg plus deploy configure serverless --git-provider "$GIT_PROVIDER" || true
  fi
  echo ""
  echo "    ✓ generated artifacts:"
  ls -la build.yaml Dockerfile container_context.yaml .github/workflows/*.yml 2>/dev/null | sed 's/^/      /' || true

  if [ "$AGENT_TYPE" = "hybrid" ] && [ -n "$REGISTRY_URL" ]; then
    cat >&2 <<HINT

    ⚠️  Hybrid registry auth — you'll need credentials so:
        - This machine can 'docker push' to $REGISTRY_URL
        - Your Dagster+ agent can 'docker pull' from $REGISTRY_URL
        Examples:
          aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY_URL
          gcloud auth configure-docker us-central1-docker.pkg.dev
          echo \$GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
        For Kubernetes agents, set up an imagePullSecret OR attach a workload-identity service account.
HINT
  fi
else
  echo "    skipped"
fi

# ── 4/6: CI API token ────────────────────────────────────────────────────────
if [ -d .github/workflows ]; then
  echo "==> 4/6: GitHub Actions CI API token (optional — only for unattended CI runs)"
  echo "    The local deploy in step 6 uses your 'dg plus login' token from step 2."
  echo "    A separate CI token is needed if you want GitHub Actions to deploy on push."
  WANT_TOKEN=$(ask "    Create a Dagster+ CI API token now? [y/N]:" "N")
  if is_yes "$WANT_TOKEN"; then
    echo ""
    echo "    Token (copy now, it won't be shown again):"
    uv run dg plus create ci-api-token | sed 's/^/      /'
    echo ""
    echo "    Add this in GitHub: Settings → Secrets and variables → Actions → New repository secret"
    echo "      Name:  DAGSTER_CLOUD_API_TOKEN"
    echo "      Value: (the token above)"
  else
    echo "    skipped (you can run 'dg plus create ci-api-token' later)"
  fi
else
  echo "==> 4/6: GitHub Actions CI API token — skipped (no .github/workflows/ to wire up)"
fi

# ── 5/7: scan + provision env vars ───────────────────────────────────────────
echo "==> 5/7: Scanning your project for env var references"
# Greps for the common reference patterns:
#   - Python:  EnvVar("X"), os.environ["X"], os.getenv("X")
#   - YAML:    *_env_var: X     and     ${env:X}
# Filters out shell/dagster/python infrastructure names that the user shouldn't set manually.
EXCLUDE_PATTERN='^(DAGSTER_HOME|DAGSTER_CLOUD_.*|PATH|HOME|USER|PWD|OLDPWD|SHELL|TERM|LANG|LC_.*|TZ|TMPDIR|VIRTUAL_ENV|UV_.*|PIP_.*|PYTHONPATH|PYTHONUNBUFFERED|CI|GITHUB_.*|GITLAB_.*|GIT_.*)$'

ENV_VARS=$(
  {
    grep -rhoE 'EnvVar\("([A-Z_][A-Z0-9_]*)"\)' src/ 2>/dev/null | sed -E 's/EnvVar\("([^"]+)"\)/\1/'
    grep -rhoE 'os\.(environ\[|getenv\()"([A-Z_][A-Z0-9_]*)"' src/ 2>/dev/null | sed -E 's/.*"([A-Z_][A-Z0-9_]*)"/\1/'
    grep -rhoE '_env_var:\s*([A-Z_][A-Z0-9_]*)' src/ 2>/dev/null | sed -E 's/.*:\s*//'
    grep -rhoE '\$\{env:([A-Z_][A-Z0-9_]*)\}' src/ 2>/dev/null | sed -E 's/\$\{env:([^}]+)\}/\1/'
  } | sort -u | grep -vE "$EXCLUDE_PATTERN" || true
)

if [ -z "$ENV_VARS" ]; then
  echo "    ✓ No env-var references found. Skipping env-var provisioning."
  echo ""
  echo "    Note: this scan only catches the most common patterns. If you've added"
  echo "    custom code with other env-var references (constructed names, dynamic"
  echo "    lookup, etc.), set them manually:"
  echo "      uv run dg plus create env MY_VAR --value '...' --deployment $DAGSTER_PLUS_DEPLOYMENT"
else
  echo "    Detected references:"
  echo "$ENV_VARS" | sed 's/^/      - /'
  echo ""
  CREATED=0
  SHELL_USED=()
  PROMPTED=()
  SKIPPED=()
  while IFS= read -r var; do
    [ -z "$var" ] && continue
    current_val="${!var:-}"
    val=""
    origin=""

    if [ -n "$current_val" ]; then
      # Truncate the displayed preview if value looks long (likely a secret)
      preview="$current_val"
      [ ${#preview} -gt 60 ] && preview="${preview:0:57}…"
      if [ "$NON_INTERACTIVE" -eq 1 ]; then
        val="$current_val"; origin="from your shell environment"
      else
        confirm=$(ask "      $var is set in your shell ('$preview'). Use this value? [Y/n/skip]:" "Y")
        case "$confirm" in
          y|Y|yes|YES|Yes|"")
            val="$current_val"; origin="from your shell environment"
            ;;
          s|skip|SKIP)
            SKIPPED+=("$var"); continue
            ;;
          *)
            # No / anything else → ask for a new one
            val=$(ask "      $var = (paste new value, or Enter to skip):" "")
            if [ -z "$val" ]; then
              SKIPPED+=("$var"); continue
            fi
            origin="entered now (overrides shell value)"
            ;;
        esac
      fi
      [ "$origin" = "from your shell environment" ] && SHELL_USED+=("$var") || PROMPTED+=("$var")
    elif [ "$NON_INTERACTIVE" -eq 1 ]; then
      SKIPPED+=("$var"); continue
    else
      val=$(ask "      $var = (not set in your shell — paste value or Enter to skip):" "")
      if [ -z "$val" ]; then
        SKIPPED+=("$var"); continue
      fi
      PROMPTED+=("$var"); origin="entered now"
    fi

    if uv run dg plus create env "$var" --value "$val" --deployment "$DAGSTER_PLUS_DEPLOYMENT" >/dev/null 2>&1; then
      echo "      ✓ $var set ($origin) → deployment '$DAGSTER_PLUS_DEPLOYMENT'"
      CREATED=$((CREATED + 1))
    else
      echo "      ✗ $var: failed to set (insufficient permissions, already exists, or other — set via the UI)"
    fi
  done <<<"$ENV_VARS"
  echo ""
  echo "    Summary: $CREATED set"
  [ ${#SHELL_USED[@]} -gt 0 ] && echo "      used your shell values for:  ${SHELL_USED[*]}"
  [ ${#PROMPTED[@]} -gt 0 ]   && echo "      took values you entered for: ${PROMPTED[*]}"
  [ ${#SKIPPED[@]} -gt 0 ]    && echo "      skipped:                     ${SKIPPED[*]}"
  echo ""
  echo "    Override or set additional vars anytime:"
  echo "      uv run dg plus create env <NAME> --value '...' --deployment $DAGSTER_PLUS_DEPLOYMENT"
  echo ""
  echo "    Note: the scan catches the most common patterns. If your project uses"
  echo "    dynamically-constructed env-var names, set those manually."
fi

# ── 6/7: deployment target summary ───────────────────────────────────────────
echo "==> 6/7: Deployment target"
ORG_ARG=""; DEPLOY_ARG=""; BUILD_ARG=""
[ -n "$DAGSTER_PLUS_ORG" ]        && ORG_ARG="--organization $DAGSTER_PLUS_ORG"
[ -n "$DAGSTER_PLUS_DEPLOYMENT" ] && DEPLOY_ARG="--deployment $DAGSTER_PLUS_DEPLOYMENT"
[ -n "$BUILD_STRATEGY" ]          && BUILD_ARG="--build-strategy $BUILD_STRATEGY"
echo "    organization: ${DAGSTER_PLUS_ORG:-(from dg plus login)}"
echo "    deployment:   $DAGSTER_PLUS_DEPLOYMENT"
echo "    build:        ${BUILD_STRATEGY:-(auto: PEX for Serverless, Docker for Hybrid)}"

# ── 7/7: deploy ──────────────────────────────────────────────────────────────
echo ""
WANT_DEPLOY=$(ask "==> 7/7: Run 'dg plus deploy' now? [Y/n]:" "Y")
if is_yes "$WANT_DEPLOY"; then
  # shellcheck disable=SC2086
  uv run dg plus deploy $BUILD_ARG $ORG_ARG $DEPLOY_ARG
else
  echo "    Skipped. Run manually:"
  echo "      cd $PROJECT_DIR"
  echo "      uv run dg plus deploy $BUILD_ARG $ORG_ARG $DEPLOY_ARG"
  exit 0
fi

cat <<MSG

>>> Deploy complete.

Open your workspace:
    open "https://${DAGSTER_PLUS_ORG:-your-org}.dagster.cloud/$DAGSTER_PLUS_DEPLOYMENT"

──────────────────────────────────────────────────────────────────────────────
Next time — redeploy this project (after a code change)

  cd $(pwd)
  uv run dg plus deploy --deployment $DAGSTER_PLUS_DEPLOYMENT

That's it. Your login token + build.yaml + env vars are already saved, so
subsequent deploys just push the latest code.

If your token has expired (after ~30 days):
  uv run dg plus login
  uv run dg plus deploy --deployment $DAGSTER_PLUS_DEPLOYMENT

Add or update an env var anytime:
  uv run dg plus create env MY_KEY --value "..." --deployment $DAGSTER_PLUS_DEPLOYMENT

If you scaffolded GitHub Actions in step 3, pushing to 'main' will redeploy
automatically — no local command needed.
──────────────────────────────────────────────────────────────────────────────

Docs: https://docs.dagster.io/api/clis/dg-cli/dg-plus
MSG
