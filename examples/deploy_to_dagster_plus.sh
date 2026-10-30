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
#     [--publish-to-github]  # create/push a GitHub repo even without prompt
#     [--github-repo NAME]   # repo name to create (default: project dir name)
#     [--github-visibility private|public]  # default: private
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
#   4. Asks: create a CI API token? → can be auto-set as a GitHub secret in step 5
#      Optional. The local deploy in step 8 uses your `dg plus login` token,
#      not this one. The CI token is only for GitHub Actions runs.
#   5. Asks: publish this project to GitHub now (via the `gh` CLI)?
#      → `git init` if needed, `gh repo create` if there's no remote yet,
#        `gh secret set DAGSTER_CLOUD_API_TOKEN` (if step 4's token exists),
#        then commit + push — so the Action scaffolded in step 3 can actually run.
#      Skipped entirely if `gh` isn't installed/authed, or git-provider isn't github.
#   6. Confirms deployment target.
#   7. Scans your project for env var references (EnvVar, os.environ,
#      *_env_var: X) and prompts you to set values via `dg plus create env`.
#   8. Runs `dg plus deploy` (build strategy auto-detected from agent type).
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
PUBLISH_TO_GITHUB=0
GITHUB_REPO=""
GITHUB_VISIBILITY=""
CI_TOKEN=""
GH_PUBLISHED=0  # set to 1 if step 5 actually pushes -- the scaffolded Action
                # will deploy from that, so step 8 shouldn't also deploy locally
CONFIGURED_WORKFLOW_FILES=()  # exactly which .github/workflows/*.yml this run's
                               # `dg plus deploy configure` touched -- so step 5
                               # never has to `git add` the whole directory and
                               # risk sweeping up an unrelated workflow file
CONFIGURE_RAN=0  # set to 1 only if `dg plus deploy configure` actually ran this
                 # invocation -- Dockerfile/build.yaml are generic enough names
                 # that "the file exists" isn't enough reason to stage it; this
                 # run needs to actually be the one responsible for it

# A dg project is marked by EITHER a pyproject.toml with a [tool.dg] table OR a
# standalone dg.toml file -- both are valid per dg's own config-discovery docs
# (https://docs.dagster.io/api/clis/dg-cli/dg-cli-configuration), pyproject.toml
# is just the more common convention. Checking only pyproject.toml would wrongly
# reject a real dg.toml-based project.
is_dg_project() {
  [ -f dg.toml ] || { [ -f pyproject.toml ] && grep -q "tool.dg" pyproject.toml; }
}

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
    --publish-to-github) PUBLISH_TO_GITHUB=1; shift;;
    --github-repo) GITHUB_REPO="$2"; shift 2;;
    --github-visibility) GITHUB_VISIBILITY="$2"; shift 2;;
    --help|-h)
      grep -E "^#( |$)" "$0" | sed 's/^# \{0,1\}//'
      exit 0;;
    *) PROJECT_DIR="$1"; shift;;
  esac
done

if [ -z "$PROJECT_DIR" ]; then
  # No positional arg given -- if the current directory is itself a dg project
  # (e.g. you cd'd into it before running this), just use it instead of
  # forcing `| bash -s <dir>`.
  if is_dg_project; then
    PROJECT_DIR="."
    echo "No project directory given -- using the current directory (it looks like a dg project)."
  else
    echo "Usage: $0 <project_dir> [--organization X] [--deployment Y] [--git-provider github|gitlab] [--non-interactive] [--with-ci] [--publish-to-github] [--github-repo NAME] [--github-visibility private|public]"
    echo "(Or just cd into your Dagster project first and run this with no arguments.)"
    exit 2
  fi
fi
if [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: $PROJECT_DIR is not a directory. Run the demo setup first."
  exit 2
fi

cd "$PROJECT_DIR"
PROJECT_DIR="$(pwd)"  # absolute, so later instructions (cd $PROJECT_DIR, etc.) are copy-pasteable regardless of how this was invoked

if ! is_dg_project; then
  echo "ERROR: $PROJECT_DIR doesn't look like a Dagster project (no dg.toml, and no [tool.dg] in pyproject.toml)."
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

# ── 1/8: dev deps ─────────────────────────────────────────────────────────────
echo "==> 1/8: Ensuring dagster-cloud-cli is in dev deps"
if ! grep -q "dagster-cloud-cli" pyproject.toml; then
  uv add --dev -q dagster-cloud-cli
  echo "    ✓ added"
else
  echo "    ✓ already present"
fi

# ── 2/8: login ────────────────────────────────────────────────────────────────
echo "==> 2/8: Checking Dagster+ login"
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

# ── 3/8: scaffold deployment artifacts (build.yaml + Dockerfile + CI) ────────
echo "==> 3/8: Deployment artifacts (build.yaml, CI workflows, Dockerfile)"
NEED_CONFIGURE=0
if [ ! -f build.yaml ] && [ ! -f dagster_cloud.yaml ]; then
  NEED_CONFIGURE=1
fi

if [ "$NEED_CONFIGURE" -eq 1 ]; then
  # Nothing here yet -- safe to default yes, and safe for --with-ci/--non-interactive
  # to force it, since there's nothing existing to overwrite.
  WANT_CONFIGURE="Y"
  if [ "$WITH_CI" -ne 1 ]; then
    WANT_CONFIGURE=$(ask "    Run 'dg plus deploy configure' to scaffold build.yaml + CI workflows? [Y/n]:" "Y")
  fi
else
  # build.yaml or dagster_cloud.yaml already exists -- this project was configured
  # before, possibly by hand, possibly customized. Re-running configure CAN
  # overwrite build.yaml/Dockerfile/.github/workflows (not every file dg writes
  # is guaranteed to prompt before overwriting), so --with-ci and
  # --non-interactive must NOT silently force this -- default to skipping, and
  # say exactly why, even when nobody's watching the terminal interactively.
  echo "    build.yaml or dagster_cloud.yaml already exists -- this project looks"
  echo "    already configured. Re-running 'dg plus deploy configure' could overwrite"
  echo "    build.yaml, Dockerfile, or .github/workflows if you've customized them."
  WANT_CONFIGURE=$(ask "    Re-run 'dg plus deploy configure' anyway? [y/N]:" "N")
fi
if is_yes "$WANT_CONFIGURE"; then
  CONFIGURE_RAN=1
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

    # --organization/--deployment matter a lot here: `dg plus deploy configure`
    # bakes the org name, Cloud URL, and deployment name LITERALLY into the
    # generated .github/workflows/dagster-plus-deploy.yml at scaffold time (not
    # resolved dynamically when the Action runs later) -- confirmed by reading
    # the actual dagster-dg-cli source (configure_ci.py's TEMPLATE_ORGANIZATION_NAME
    # / TEMPLATE_DEFAULT_DEPLOYMENT_NAME substitution). If we don't pass these
    # explicitly, `dg` falls back to whatever's in the user's own `dg plus login`
    # config, or a bare "prod" -- which may not be this customer's real
    # deployment name. Pass the exact same org/deployment this script already
    # resolved (and that step 6 later confirms with the user), so the CI-
    # triggered deploy and the local deploy can't silently target different
    # places. Also pass -y: we've already gotten explicit consent to run
    # configure at all (see the NEED_CONFIGURE/"already exists" check above),
    # so there's no reason to risk dg's OWN internal confirmation/prompt
    # questions (deployment name, overwrite-this-file, etc.) hanging or
    # aborting when there's no real TTY attached (curl | bash).
    REG_ARG=""; PLAT_ARG=""; ORG_CONFIG_ARG=""
    [ -n "$REGISTRY_URL" ]    && REG_ARG="--registry-url $REGISTRY_URL"
    [ -n "$AGENT_PLATFORM" ]  && PLAT_ARG="--agent-platform $AGENT_PLATFORM"
    [ -n "$DAGSTER_PLUS_ORG" ] && ORG_CONFIG_ARG="--organization $DAGSTER_PLUS_ORG"
    echo "    Running: dg plus deploy configure hybrid --git-provider $GIT_PROVIDER --deployment $DAGSTER_PLUS_DEPLOYMENT $ORG_CONFIG_ARG $REG_ARG $PLAT_ARG --yes"
    CONFIGURE_MARKER=$(mktemp)
    # shellcheck disable=SC2086
    uv run dg plus deploy configure hybrid --git-provider "$GIT_PROVIDER" --deployment "$DAGSTER_PLUS_DEPLOYMENT" $ORG_CONFIG_ARG $REG_ARG $PLAT_ARG --yes || true
  else
    # Serverless: just go. Same org/deployment/--yes reasoning as the hybrid
    # branch above.
    ORG_CONFIG_ARG=""
    [ -n "$DAGSTER_PLUS_ORG" ] && ORG_CONFIG_ARG="--organization $DAGSTER_PLUS_ORG"
    echo "    Running: dg plus deploy configure serverless --git-provider $GIT_PROVIDER --deployment $DAGSTER_PLUS_DEPLOYMENT $ORG_CONFIG_ARG --yes"
    CONFIGURE_MARKER=$(mktemp)
    # shellcheck disable=SC2086
    uv run dg plus deploy configure serverless --git-provider "$GIT_PROVIDER" --deployment "$DAGSTER_PLUS_DEPLOYMENT" $ORG_CONFIG_ARG --yes || true
  fi
  # Exactly which workflow file(s) this run's configure call touched (by mtime,
  # relative to a marker created right before it ran) -- NOT "everything in
  # .github/workflows", which could include an unrelated workflow someone else
  # already had sitting there (mid-edit, uncommitted or otherwise).
  if [ -d .github/workflows ]; then
    while IFS= read -r -d '' wf; do
      CONFIGURED_WORKFLOW_FILES+=("$wf")
    done < <(find .github/workflows -maxdepth 1 -type f -newer "$CONFIGURE_MARKER" -print0 2>/dev/null)
  fi
  rm -f "$CONFIGURE_MARKER"
  echo ""
  echo "    ✓ generated artifacts:"
  ls -la build.yaml Dockerfile container_context.yaml .github/workflows/*.yml 2>/dev/null | sed 's/^/      /' || true

  # `dg plus deploy configure hybrid` ALWAYS writes container_context.yaml, for
  # every agent platform -- but its content is 100% commented-out placeholder
  # examples, not active config. So "does it need resource sizing" is really
  # "does it have any active (non-comment, non-blank) line yet", not "does the
  # file exist" -- a plain existence check would never fire.
  HAS_ACTIVE_CONTEXT=0
  if [ -f container_context.yaml ] && grep -vE '^[[:space:]]*(#|$)' container_context.yaml | grep -q .; then
    HAS_ACTIVE_CONTEXT=1
  fi
  if [ "$AGENT_TYPE" = "hybrid" ] && [ "$HAS_ACTIVE_CONTEXT" -eq 0 ]; then
    echo ""
    echo "    container_context.yaml was scaffolded but everything in it is commented"
    echo "    out -- nothing's actually configured yet. Per-code-location, this is also"
    echo "    where you'd set up sidecars, volume mounts, or IAM/service-account"
    echo "    overrides -- full schema (the commented examples already in the file are"
    echo "    a good reference for those):"
    echo "      ECS:        https://docs.dagster.io/deployment/dagster-plus/hybrid/amazon-ecs/configuration-reference"
    echo "      Kubernetes: https://docs.dagster.io/deployment/dagster-plus/hybrid/kubernetes/configuration"
    if [ "$AGENT_PLATFORM" = "ecs" ] || [ "$AGENT_PLATFORM" = "k8s" ]; then
      WANT_SIZING=$(ask "    A common cause of killed runs is under-sized compute -- set a default compute size now? [Y/n]:" "Y")
      if is_yes "$WANT_SIZING"; then
        {
          echo ""
          echo "# --- Added by deploy_to_dagster_plus.sh: default compute size ---"
          if [ "$AGENT_PLATFORM" = "ecs" ]; then
            cat <<'CCY'
ecs:
  server_resources: # code server that loads this code location
    cpu: '256'
    memory: '1024'
  run_resources: # individual job runs
    cpu: '4096'
    memory: '16384'
CCY
          else
            cat <<'CCY'
k8s:
  server_k8s_config: # code server that loads this code location
    container_config:
      resources:
        limits:
          cpu: 250m
          memory: 1024Mi
  run_k8s_config: # individual job runs
    container_config:
      resources:
        limits:
          cpu: 4000m
          memory: 16384Mi
CCY
          fi
        } >> container_context.yaml
        echo "    ✓ added active resource config to container_context.yaml (appended --"
        echo "      the commented examples already in the file, e.g. for sidecars/mounts,"
        echo "      are left in place for reference). run_resources is 4 vCPU / 16GB,"
        echo "      matching Dagster+ Serverless's own default run size. This is a"
        echo "      starting point, not a guarantee: adjust per code location once you"
        echo "      know its real memory footprint, especially if a run still gets"
        echo "      OOM-killed."
      else
        echo "    skipped -- using agent defaults. If a run gets OOM-killed later, this is"
        echo "    the file to fix: uncomment and set run_resources.memory (ECS) or"
        echo "    run_k8s_config's resources.limits.memory (Kubernetes) for that code"
        echo "    location."
      fi
    fi
  fi

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

# ── 4/8: CI API token ────────────────────────────────────────────────────────
if [ -d .github/workflows ]; then
  echo "==> 4/8: GitHub Actions CI API token (optional — only for unattended CI runs)"
  echo "    The local deploy in step 8 uses your 'dg plus login' token from step 2."
  echo "    A separate CI token is needed if you want GitHub Actions to deploy on push."
  WANT_TOKEN=$(ask "    Create a Dagster+ CI API token now? [y/N]:" "N")
  if [ "$PUBLISH_TO_GITHUB" -eq 1 ]; then WANT_TOKEN="Y"; fi
  if is_yes "$WANT_TOKEN"; then
    CI_TOKEN=$(uv run dg plus create ci-api-token)
    echo ""
    echo "    Token (copy now, it won't be shown again):"
    echo "$CI_TOKEN" | sed 's/^/      /'
    echo ""
    echo "    The next step can set this as a GitHub secret for you automatically."
    echo "    Otherwise: Settings → Secrets and variables → Actions → New repository secret"
    echo "      Name:  DAGSTER_CLOUD_API_TOKEN"
    echo "      Value: (the token above)"
  else
    echo "    skipped (you can run 'dg plus create ci-api-token' later)"
  fi
else
  echo "==> 4/8: GitHub Actions CI API token — skipped (no .github/workflows/ to wire up)"
fi

# ── 5/8: publish to GitHub (optional — lets the scaffolded Action actually run) ──
if [ "$GIT_PROVIDER" = "github" ] && [ -d .github/workflows ]; then
  echo "==> 5/8: Publish to GitHub"

  if ! command -v gh >/dev/null 2>&1; then
    echo "    'gh' (GitHub CLI) not found — skipping. Install: https://cli.github.com"
    echo "    Manual steps once you have it:"
    echo "      git init && git add -A && git commit -m 'Initial commit'"
    echo "      gh repo create <name> --private --source=. --remote=origin --push"
    echo "      git commit --allow-empty -m 'trigger deploy' && git push"
    echo "        (that last empty commit matters -- GitHub Actions doesn't reliably"
    echo "         fire on the push that creates a branch, so without it the first"
    echo "         deploy may just never happen)"
    [ -n "$CI_TOKEN" ] && echo "      gh secret set DAGSTER_CLOUD_API_TOKEN --body '<token from step 4>'"
  elif ! gh auth status >/dev/null 2>&1; then
    echo "    'gh' isn't authenticated. Run 'gh auth login', then either re-run this"
    echo "    script or do it manually:"
    echo "      git init && git add -A && git commit -m 'Initial commit'"
    echo "      gh repo create <name> --private --source=. --remote=origin --push"
    echo "      git commit --allow-empty -m 'trigger deploy' && git push"
    echo "        (that last empty commit matters -- GitHub Actions doesn't reliably"
    echo "         fire on the push that creates a branch, so without it the first"
    echo "         deploy may just never happen)"
  else
    NESTED_REPO=0
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      TOPLEVEL=$(git rev-parse --show-toplevel)
      if [ "$TOPLEVEL" != "$(pwd -P)" ]; then
        echo "    ⚠️  $(pwd) is nested inside an existing git repo at $TOPLEVEL."
        echo "       Skipping auto git-init/push to avoid a confusing nested repo — set this up manually if needed."
        NESTED_REPO=1
      fi
    fi

    if [ "$NESTED_REPO" -eq 0 ]; then
      DEFAULT_PUBLISH="N"
      [ "$WITH_CI" -eq 1 ] && DEFAULT_PUBLISH="Y"
      WANT_PUBLISH=$(ask "    Push this project to GitHub now, so the Action can run on future pushes? [y/N]:" "$DEFAULT_PUBLISH")
      if [ "$PUBLISH_TO_GITHUB" -eq 1 ]; then WANT_PUBLISH="Y"; fi

      if is_yes "$WANT_PUBLISH"; then
        FRESH_REPO=0
        if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
          git init -q -b main
          echo "    ✓ git init"
          FRESH_REPO=1
        fi

        if [ -z "$(git config user.email 2>/dev/null || true)" ]; then
          echo "    No git identity configured for this repo."
          GIT_NAME=$(ask "    git user.name:" "$(whoami)")
          GIT_EMAIL=$(ask "    git user.email:" "${GIT_NAME}@users.noreply.github.com")
          git config user.name "$GIT_NAME"
          git config user.email "$GIT_EMAIL"
        fi

        # Resolve which remote to push to.
        #   - If 'origin' already exists AND you can push to it (e.g. you
        #     cloned your own repo), use it as-is — this is the "fix up an
        #     existing repo" path.
        #   - If 'origin' exists but you have no push access (e.g. you cloned
        #     someone else's public example repo), don't try to push there —
        #     fall back to a second remote ('dagster-plus') pointed at a repo
        #     under your own account instead.
        #   - If there's no 'origin' at all, ask for a repo name and either
        #     attach to it (if it already exists on GitHub) or create it.
        REMOTE_NAME="origin"
        HAVE_REMOTE=0
        if git remote get-url origin >/dev/null 2>&1; then
          # Raw configured URL, not `git remote get-url` -- that silently applies
          # any url.*.insteadOf rewrite (e.g. a common SSH-over-HTTPS rule), which
          # would feed the wrong string into the owner/repo parsing below.
          ORIGIN_URL=$(git config --get remote.origin.url)
          ORIGIN_NWO=$(echo "$ORIGIN_URL" | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##')
          ORIGIN_PERM=""
          if echo "$ORIGIN_NWO" | grep -qE '^[^/]+/[^/]+$'; then
            ORIGIN_PERM=$(gh repo view "$ORIGIN_NWO" --json viewerPermission -q .viewerPermission 2>/dev/null || true)
          fi
          case "$ORIGIN_PERM" in
            WRITE|ADMIN|MAINTAIN)
              HAVE_REMOTE=1
              echo "    ✓ 'origin' ($ORIGIN_NWO) is pushable — using it"
              ;;
            *)
              echo "    'origin' is $ORIGIN_URL, but you don't have push access to it"
              echo "    (e.g. this looks like a clone of someone else's repo)."
              REMOTE_NAME="dagster-plus"
              git remote get-url "$REMOTE_NAME" >/dev/null 2>&1 && HAVE_REMOTE=1
              ;;
          esac
        elif git remote get-url "$REMOTE_NAME" >/dev/null 2>&1; then
          HAVE_REMOTE=1
        fi

        if [ "$HAVE_REMOTE" -eq 0 ]; then
          DEFAULT_REPO_NAME=$(basename "$(pwd)" | tr -c 'A-Za-z0-9_.-' '-')
          if [ -z "$GITHUB_REPO" ]; then
            GITHUB_REPO=$(ask "    GitHub repo (new or existing) to push to:" "$DEFAULT_REPO_NAME")
          fi
          GH_LOGIN=$(gh api user --jq .login 2>/dev/null || true)
          if echo "$GITHUB_REPO" | grep -q '/'; then
            REPO_NWO="$GITHUB_REPO"
          else
            REPO_NWO="${GH_LOGIN:+$GH_LOGIN/}$GITHUB_REPO"
          fi
          if [ -n "$REPO_NWO" ] && gh repo view "$REPO_NWO" >/dev/null 2>&1; then
            REPO_URL=$(gh repo view "$REPO_NWO" --json url -q .url)
            git remote add "$REMOTE_NAME" "${REPO_URL}.git" 2>/dev/null || git remote set-url "$REMOTE_NAME" "${REPO_URL}.git"
            echo "    ✓ $REPO_NWO already exists on GitHub — attached as '$REMOTE_NAME'"
          else
            if [ -z "$GITHUB_VISIBILITY" ]; then
              GITHUB_VISIBILITY=$(ask "    Visibility for the new repo [private/public]:" "private")
            fi
            [ "$GITHUB_VISIBILITY" != "public" ] && GITHUB_VISIBILITY="private"
            echo "    Running: gh repo create $GITHUB_REPO --$GITHUB_VISIBILITY --source=. --remote=$REMOTE_NAME"
            if gh repo create "$GITHUB_REPO" "--$GITHUB_VISIBILITY" --source=. --remote="$REMOTE_NAME"; then
              echo "    ✓ created $(gh repo view "$GITHUB_REPO" --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "$GITHUB_REPO")"
            else
              echo "    ✗ 'gh repo create' failed (name taken, no permission, etc.) — set up the remote manually:"
              echo "        gh repo create <name> --private --source=. --remote=$REMOTE_NAME"
            fi
          fi
        fi

        if git remote get-url "$REMOTE_NAME" >/dev/null 2>&1; then
          REMOTE_NWO=$(git config --get remote."$REMOTE_NAME".url | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##')
          if [ -n "$CI_TOKEN" ]; then
            if gh secret set DAGSTER_CLOUD_API_TOKEN --body "$CI_TOKEN" --repo "$REMOTE_NWO" >/dev/null 2>&1; then
              echo "    ✓ GitHub secret DAGSTER_CLOUD_API_TOKEN set"
            else
              echo "    ✗ couldn't set the GitHub secret automatically — set it manually:"
              echo "        gh secret set DAGSTER_CLOUD_API_TOKEN --body '<token from step 4>' --repo $REMOTE_NWO"
            fi
          fi

          # Fresh repo (we just ran git init): this IS the project, push all of it.
          # Pre-existing repo (you cloned this): only add files THIS RUN actually
          # generated/touched via `dg plus deploy configure` -- never sweep up
          # other uncommitted work, and never assume an existing Dockerfile/
          # build.yaml/workflow is ours to commit just because it exists on disk
          # (e.g. a generic "Dockerfile" could easily be unrelated and mid-edit).
          if [ "$FRESH_REPO" -eq 1 ]; then
            git add -A
          elif [ "$CONFIGURE_RAN" -eq 1 ]; then
            TO_ADD=()
            for f in build.yaml Dockerfile container_context.yaml dagster_cloud.yaml; do
              [ -f "$f" ] && TO_ADD+=("$f")
            done
            [ ${#CONFIGURED_WORKFLOW_FILES[@]} -gt 0 ] && TO_ADD+=("${CONFIGURED_WORKFLOW_FILES[@]}")
            [ ${#TO_ADD[@]} -gt 0 ] && git add "${TO_ADD[@]}"
          else
            echo "    step 3's configure didn't run this time, so there's nothing new to"
            echo "    add -- not touching build.yaml/Dockerfile/.github/workflows, in case"
            echo "    anything with those names already in this checkout is unrelated."
          fi

          if ! git diff --cached --quiet; then
            if [ "$FRESH_REPO" -eq 1 ]; then
              git commit -q -m "Initial Dagster+ deployment setup"
            else
              git commit -q -m "Add Dagster+ deployment config (build.yaml, CI workflow)"
            fi
            echo "    ✓ committed"
          else
            echo "    (nothing to commit)"
          fi

          CURRENT_BRANCH=$(git branch --show-current)
          PUSH_OUTPUT=$(git push -u "$REMOTE_NAME" "$CURRENT_BRANCH" 2>&1) && PUSH_OK=1 || PUSH_OK=0
          echo "$PUSH_OUTPUT" | sed 's/^/    /'
          if [ "$PUSH_OK" -eq 1 ]; then
            GH_PUBLISHED=1
            echo "    ✓ pushed to $(gh repo view "$REMOTE_NWO" --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "$REMOTE_NWO") ($CURRENT_BRANCH)"
            if echo "$PUSH_OUTPUT" | grep -q '\[new branch\]'; then
              # GitHub Actions doesn't reliably fire on the very push that creates a
              # branch -- the workflow run on this commit can silently never start.
              # A no-op follow-up push is the documented workaround.
              echo ""
              echo "    This was the first push to this branch, so Dagster+ is NOT deployed yet --"
              echo "    GitHub Actions doesn't reliably trigger on the push that creates a branch."
              echo "    Pushing a harmless empty commit to kick off the first real run..."
              git commit --allow-empty -q -m "Trigger initial Dagster+ deploy workflow"
              if git push "$REMOTE_NAME" "$CURRENT_BRANCH" >/dev/null 2>&1; then
                echo "    ✓ follow-up commit pushed — check the repo's Actions tab now."
              else
                echo "    ✗ follow-up push failed. The workflow is scaffolded and committed, but"
                echo "      you'll need to push any small change yourself to trigger the first run:"
                echo "        git commit --allow-empty -m 'trigger deploy' && git push $REMOTE_NAME $CURRENT_BRANCH"
              fi
            else
              echo "    GitHub Actions should be running now — check the repo's Actions tab."
            fi
          else
            echo "    ✗ push failed — check the remote and push manually:"
            echo "        git push -u $REMOTE_NAME $CURRENT_BRANCH"
          fi
        fi
      else
        echo "    skipped — you can do this manually later:"
        echo "      git init && git add -A && git commit -m 'Initial commit'"
        echo "      gh repo create <name> --private --source=. --remote=origin --push"
        echo "      git commit --allow-empty -m 'trigger deploy' && git push"
        echo "        (that last empty commit matters -- GitHub Actions doesn't reliably"
        echo "         fire on the push that creates a branch, so without it the first"
        echo "         deploy may just never happen)"
      fi
    fi
  fi
else
  echo "==> 5/8: Publish to GitHub — skipped (git-provider isn't github, or no .github/workflows scaffolded)"
fi

# ── 6/8: deployment target — CONFIRM FIRST (env-var step depends on it) ─────
echo "==> 6/8: Deployment target"
ORG_ARG=""; DEPLOY_ARG=""; BUILD_ARG=""
[ -n "$DAGSTER_PLUS_ORG" ]        && ORG_ARG="--organization $DAGSTER_PLUS_ORG"
[ -n "$DAGSTER_PLUS_DEPLOYMENT" ] && DEPLOY_ARG="--deployment $DAGSTER_PLUS_DEPLOYMENT"
[ -n "$BUILD_STRATEGY" ]          && BUILD_ARG="--build-strategy $BUILD_STRATEGY"
echo "    organization: ${DAGSTER_PLUS_ORG:-(from dg plus login)}"
echo "    deployment:   $DAGSTER_PLUS_DEPLOYMENT"
echo "    build:        ${BUILD_STRATEGY:-(auto: PEX for Serverless, Docker for Hybrid)}"
if [ "$NON_INTERACTIVE" -ne 1 ]; then
  CONFIRM_TARGET=$(ask "    Continue with this target? [Y/n]:" "Y")
  if ! is_yes "$CONFIRM_TARGET"; then
    echo "    Aborted. Re-run with --deployment <name> to target a different deployment."
    exit 0
  fi
fi

# ── 7/8: env vars (uses confirmed deployment + checks what's already set) ────
echo "==> 7/8: Scanning your project for env var references"
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
  # Pull what's ALREADY set on this deployment so we don't blindly overwrite.
  echo "    Checking what's already set in Dagster+ (dg plus pull env)..."
  EXISTING_KEYS=""
  if uv run dg plus pull env >/dev/null 2>&1 && [ -f .env ]; then
    EXISTING_KEYS=$(grep -oE '^[A-Z_][A-Z0-9_]*=' .env 2>/dev/null | sed 's/=$//')
  fi
  if [ -n "$EXISTING_KEYS" ]; then
    echo "    Already in Dagster+ deployment '$DAGSTER_PLUS_DEPLOYMENT':"
    echo "$EXISTING_KEYS" | sed 's/^/      ✓ /'
    echo ""
  else
    echo "    (none currently set on this deployment)"
    echo ""
  fi

  CREATED=0
  ALREADY=()
  SHELL_USED=()
  PROMPTED=()
  SKIPPED=()
  while IFS= read -r var; do
    [ -z "$var" ] && continue

    # Already set on Dagster+? Don't overwrite without explicit consent.
    if echo "$EXISTING_KEYS" | grep -qx "$var"; then
      if [ "$NON_INTERACTIVE" -eq 1 ]; then
        ALREADY+=("$var"); continue
      fi
      reset=$(ask "      $var is already set on '$DAGSTER_PLUS_DEPLOYMENT'. Overwrite? [y/N]:" "N")
      if ! is_yes "$reset"; then
        ALREADY+=("$var"); continue
      fi
      # else fall through to set-new flow
    fi

    current_val="${!var:-}"
    val=""
    origin=""

    if [ -n "$current_val" ]; then
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
      echo "      ✗ $var: failed to set (insufficient permissions or other — set via the UI)"
    fi
  done <<<"$ENV_VARS"
  echo ""
  echo "    Summary: $CREATED set"
  [ ${#ALREADY[@]} -gt 0 ]    && echo "      already on Dagster+ (kept):   ${ALREADY[*]}"
  [ ${#SHELL_USED[@]} -gt 0 ] && echo "      used your shell values for:   ${SHELL_USED[*]}"
  [ ${#PROMPTED[@]} -gt 0 ]   && echo "      took values you entered for:  ${PROMPTED[*]}"
  [ ${#SKIPPED[@]} -gt 0 ]    && echo "      skipped:                      ${SKIPPED[*]}"
  echo ""
  echo "    Override or set additional vars anytime:"
  echo "      uv run dg plus create env <NAME> --value '...' --deployment $DAGSTER_PLUS_DEPLOYMENT"
  echo ""
  echo "    Note: the scan catches the most common patterns. If your project uses"
  echo "    dynamically-constructed env-var names, set those manually."
fi

# ── 8/8: deploy ──────────────────────────────────────────────────────────────
echo ""
if [ "$GH_PUBLISHED" -eq 1 ]; then
  echo "    You published to GitHub in step 5 -- the scaffolded Action will build and"
  echo "    deploy this automatically once it runs, so a local deploy here would just"
  echo "    be a redundant duplicate (defaulting to skip)."
  WANT_DEPLOY=$(ask "==> 8/8: Run 'dg plus deploy' now anyway? [y/N]:" "N")
else
  WANT_DEPLOY=$(ask "==> 8/8: Run 'dg plus deploy' now? [Y/n]:" "Y")
fi
if is_yes "$WANT_DEPLOY"; then
  # shellcheck disable=SC2086
  uv run dg plus deploy $BUILD_ARG $ORG_ARG $DEPLOY_ARG
else
  if [ "$GH_PUBLISHED" -eq 1 ]; then
    echo "    Skipped -- the GitHub Action is deploying this instead. Check the repo's"
    echo "    Actions tab for progress. If you ever need to deploy locally too:"
  else
    echo "    Skipped. Run manually:"
  fi
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

If you published to GitHub in step 5, pushing to 'main' will redeploy
automatically from here on — no local command needed.
──────────────────────────────────────────────────────────────────────────────

Docs: https://docs.dagster.io/api/clis/dg-cli/dg-plus
MSG
