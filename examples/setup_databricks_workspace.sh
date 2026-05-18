#!/usr/bin/env bash
# Interactive setup: bring an existing Databricks workspace's Jobs into a
# Dagster project — using the official `dagster-databricks` integration's
# DatabricksWorkspaceComponent. Asks the user for everything they might
# want to customize and generates a working project with the wiring done.
#
# Auto-installs uv + jq if missing (with user consent). Works on macOS
# (default bash 3.2 — no bash 4+ syntax used) and modern Linux distros
# (Debian/Ubuntu, RHEL/CentOS/Fedora, Arch).
#
# What it does:
#   1. Asks for project name (collision-checked)
#   2. Asks for Databricks host (uses $DATABRICKS_HOST if set; validates URL)
#   3. Asks for Databricks personal access token (uses $DATABRICKS_TOKEN
#      if set; hidden prompt; verified against Jobs API before continuing)
#   4. Lists every job in the workspace (Jobs API 2.1, paginated)
#   5. User picks which job IDs to bring in (numbers, 'all', or 'q')
#   6. For each selected job, asks which OTHER selected jobs it depends on
#      so Dagster models the cross-job orchestration dependencies
#   7. Confirms the plan; user gets a last-chance bail-out
#   8. Scaffolds Dagster project, installs dagster-databricks, writes
#      defs.yaml with databricks_filter.include_jobs.job_ids and
#      asset_overrides.<job>.depends_on populated from the answers
#
# COST: $0 (script-side). Triggered job runs cost whatever they cost
# in your Databricks workspace.

set -eo pipefail

# This script is interactive — prompts the user for every input.
# Refuse to run in non-interactive mode (e.g. piped from curl) so prompts
# don't silently fail when stdin is exhausted.
if [ ! -t 0 ]; then
  echo "ERROR: this script is interactive — it asks for project name, host, token, etc."
  echo "       Download it first, then run from a terminal:"
  echo
  echo "  curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_databricks_workspace.sh -o setup_databricks_workspace.sh"
  echo "  chmod +x setup_databricks_workspace.sh"
  echo "  ./setup_databricks_workspace.sh"
  exit 1
fi

PROJECT=""
PARTIAL_PROJECT_PATH=""

# Cleanup on interrupt: remove any half-scaffolded project dir so retries are clean.
cleanup_on_interrupt() {
  if [ -n "$PARTIAL_PROJECT_PATH" ] && [ -d "$PARTIAL_PROJECT_PATH" ]; then
    echo
    echo ">>> Cleaning up half-built project at $PARTIAL_PROJECT_PATH ..."
    rm -rf "$PARTIAL_PROJECT_PATH" 2>/dev/null || true
  fi
  exit 130
}
trap cleanup_on_interrupt INT TERM

# ── Helpers ─────────────────────────────────────────────────────────────
print_header() {
  echo "════════════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "════════════════════════════════════════════════════════════════════"
}

confirm() {
  # confirm "prompt" — returns 0 (yes) on Y/y/<empty default Y>, else 1
  local prompt="$1"
  local answer
  read -r -p "$prompt [Y/n]: " answer
  case "$answer" in
    ""|y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

# ── Pre-flight: auto-install uv + jq if missing ────────────────────────
ensure_uv() {
  if command -v uvx >/dev/null 2>&1; then return 0; fi
  echo
  echo "uv (uvx) is required but not installed."
  echo "  See: https://docs.astral.sh/uv/"
  if confirm "Install uv now via the official installer?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installer adds to ~/.local/bin or ~/.cargo/bin and writes shell rc
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uvx >/dev/null 2>&1; then
      echo "ERROR: uv installed but 'uvx' still not on PATH."
      echo "       Open a new shell (so PATH is reloaded) and re-run this script."
      exit 1
    fi
    echo "✓ uv installed."
  else
    echo "Aborted. Install uv manually: https://docs.astral.sh/uv/"
    exit 1
  fi
}

ensure_jq() {
  if command -v jq >/dev/null 2>&1; then return 0; fi
  echo
  echo "jq is required but not installed."
  local OS_NAME
  OS_NAME="$(uname -s)"

  if [ "$OS_NAME" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      if confirm "Install jq via Homebrew?"; then
        brew install jq
      else
        echo "Aborted. Install manually: brew install jq"; exit 1
      fi
    else
      echo "ERROR: Homebrew not found. Install brew first: https://brew.sh"
      echo "Then run: brew install jq"
      exit 1
    fi
  elif [ "$OS_NAME" = "Linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      if confirm "Install jq via apt-get (will use sudo)?"; then
        sudo apt-get update -qq && sudo apt-get install -y jq
      else
        echo "Aborted."; exit 1
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if confirm "Install jq via dnf (will use sudo)?"; then
        sudo dnf install -y jq
      else
        echo "Aborted."; exit 1
      fi
    elif command -v yum >/dev/null 2>&1; then
      if confirm "Install jq via yum (will use sudo)?"; then
        sudo yum install -y jq
      else
        echo "Aborted."; exit 1
      fi
    elif command -v pacman >/dev/null 2>&1; then
      if confirm "Install jq via pacman (will use sudo)?"; then
        sudo pacman -S --noconfirm jq
      else
        echo "Aborted."; exit 1
      fi
    else
      echo "ERROR: jq not found and no known package manager (apt/dnf/yum/pacman) detected."
      echo "Install jq manually: https://stedolan.github.io/jq/download/"
      exit 1
    fi
  else
    echo "ERROR: jq not found on $OS_NAME. Install manually: https://stedolan.github.io/jq/download/"
    exit 1
  fi

  if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq install failed."
    exit 1
  fi
  echo "✓ jq installed."
}

ensure_curl() {
  if command -v curl >/dev/null 2>&1; then return 0; fi
  echo "ERROR: curl is required but not installed. (Should come pre-installed on macOS/Linux.)"
  echo "  macOS:  xcode-select --install"
  echo "  Debian: sudo apt-get install -y curl"
  echo "  RHEL:   sudo dnf install -y curl"
  exit 1
}

print_header "Databricks workspace → Dagster project setup"
echo "  Uses the official dagster-databricks DatabricksWorkspaceComponent."
echo

ensure_curl
ensure_uv
ensure_jq

# ── 1. Project name (collision-checked) ────────────────────────────────
while true; do
  read -r -p "Project name [databricks-dagster]: " PROJECT
  PROJECT="${PROJECT:-databricks-dagster}"
  PROJECT="${PROJECT// /-}"
  # validate: no slashes, no leading dots, non-empty
  case "$PROJECT" in
    *"/"*|.*|"")
      echo "  Invalid name. Use letters / digits / hyphens / underscores."
      continue
      ;;
  esac
  if [ -e "$PROJECT" ]; then
    echo "  '$PROJECT' already exists in this directory."
    if confirm "  Remove it and continue?"; then
      rm -rf "$PROJECT"
      break
    fi
    # else loop and re-prompt
  else
    break
  fi
done

# ── 2. Databricks host (validated) ─────────────────────────────────────
while true; do
  if [ -n "${DATABRICKS_HOST:-}" ]; then
    read -r -p "Databricks host [$DATABRICKS_HOST]: " HOST_INPUT
    DATABRICKS_HOST="${HOST_INPUT:-$DATABRICKS_HOST}"
  else
    read -r -p "Databricks host (e.g. https://dbc-abc123.cloud.databricks.com): " DATABRICKS_HOST
  fi
  DATABRICKS_HOST="${DATABRICKS_HOST%/}"
  case "$DATABRICKS_HOST" in
    https://*) break ;;
    http://*)
      echo "  WARNING: http:// (insecure). Use https:// unless you know what you're doing."
      if confirm "  Continue anyway?"; then break; fi
      ;;
    "")
      echo "  Host cannot be empty."
      ;;
    *)
      echo "  Host must start with https:// (e.g. https://dbc-12345.cloud.databricks.com)"
      ;;
  esac
done

# ── 3. Databricks token (verified) ─────────────────────────────────────
while true; do
  if [ -n "${DATABRICKS_TOKEN:-}" ]; then
    if confirm "Use \$DATABRICKS_TOKEN from environment?"; then
      :
    else
      DATABRICKS_TOKEN=""
    fi
  fi
  if [ -z "${DATABRICKS_TOKEN:-}" ]; then
    read -r -s -p "Databricks personal access token (input hidden): " DATABRICKS_TOKEN
    echo
  fi
  if [ -z "$DATABRICKS_TOKEN" ]; then
    echo "  Token cannot be empty."
    continue
  fi

  # Verify by hitting an endpoint that needs only basic auth scope
  echo "  Verifying token against $DATABRICKS_HOST ..."
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $DATABRICKS_TOKEN" \
    "$DATABRICKS_HOST/api/2.0/preview/scim/v2/Me" || echo "000")
  case "$HTTP_CODE" in
    200) echo "  ✓ Token authenticates."; break ;;
    401|403) echo "  ✗ HTTP $HTTP_CODE — token rejected. Try again." ; DATABRICKS_TOKEN="" ;;
    404) echo "  ✓ Endpoint missing (HTTP 404) — your host may be older, continuing anyway."; break ;;
    000) echo "  ✗ Could not reach $DATABRICKS_HOST. Check the URL + network." ;;
    *)   echo "  ✗ Unexpected HTTP $HTTP_CODE — check host/token." ;;
  esac
done

# ── 4. Fetch jobs (paginated) ──────────────────────────────────────────
echo
echo ">>> Fetching jobs from $DATABRICKS_HOST ..."

ALL_JOBS_JSON='{"jobs":[]}'
NEXT_PAGE_TOKEN=""
PAGE=1
while true; do
  if [ -z "$NEXT_PAGE_TOKEN" ]; then
    URL="$DATABRICKS_HOST/api/2.1/jobs/list?limit=100&expand_tasks=false"
  else
    URL="$DATABRICKS_HOST/api/2.1/jobs/list?limit=100&expand_tasks=false&page_token=$NEXT_PAGE_TOKEN"
  fi
  PAGE_JSON=$(curl -sf -H "Authorization: Bearer $DATABRICKS_TOKEN" "$URL" || echo "")
  if [ -z "$PAGE_JSON" ]; then
    echo "ERROR: Jobs API call failed on page $PAGE."
    exit 1
  fi
  ALL_JOBS_JSON=$(jq -s '{jobs: (.[0].jobs + .[1].jobs)}' \
    <(echo "$ALL_JOBS_JSON") <(echo "$PAGE_JSON" | jq '{jobs: (.jobs // [])}'))
  NEXT_PAGE_TOKEN=$(echo "$PAGE_JSON" | jq -r '.next_page_token // empty')
  HAS_MORE=$(echo "$PAGE_JSON" | jq -r '.has_more // false')
  if [ -z "$NEXT_PAGE_TOKEN" ] || [ "$HAS_MORE" != "true" ]; then break; fi
  PAGE=$((PAGE + 1))
done

JOB_LIST=$(echo "$ALL_JOBS_JSON" | jq -r '.jobs[]? | "\(.job_id)\t\(.settings.name // "(no name)")"' | sort -t$'\t' -k2)
JOB_COUNT=$(echo "$JOB_LIST" | grep -c "" || true)
if [ -z "$JOB_LIST" ] || [ "$JOB_COUNT" -eq 0 ]; then
  echo "ERROR: No jobs found in workspace (or token lacks Jobs:Read permission)."
  exit 1
fi

echo
echo "Available jobs in workspace ($JOB_COUNT total):"
echo "$JOB_LIST" | awk -F'\t' '{printf "  [%3d] job_id=%-10s  %s\n", NR, $1, $2}'
echo

# ── 5. Select jobs ─────────────────────────────────────────────────────
SELECTED_IDS=()
SELECTED_NAMES=()

while [ "${#SELECTED_IDS[@]}" -eq 0 ]; do
  read -r -p "Select job numbers (comma-separated, 'all', or 'q' to quit): " SELECTION
  case "$SELECTION" in
    q|Q) echo "Aborted."; exit 0 ;;
    all|ALL|All)
      while IFS=$'\t' read -r jid jname; do
        SELECTED_IDS+=("$jid")
        SELECTED_NAMES+=("$jname")
      done <<< "$JOB_LIST"
      ;;
    "")
      echo "  Nothing selected."
      ;;
    *)
      OLD_IFS="$IFS"; IFS=','
      for idx in $SELECTION; do
        IFS="$OLD_IFS"
        idx="${idx// /}"
        [ -z "$idx" ] && continue
        if ! echo "$idx" | grep -qE '^[0-9]+$'; then
          echo "  Ignoring non-numeric token: '$idx'"; continue
        fi
        if [ "$idx" -lt 1 ] || [ "$idx" -gt "$JOB_COUNT" ]; then
          echo "  Out of range: $idx (have 1..$JOB_COUNT)"; continue
        fi
        LINE=$(echo "$JOB_LIST" | sed -n "${idx}p")
        SELECTED_IDS+=("$(echo "$LINE" | cut -f1)")
        SELECTED_NAMES+=("$(echo "$LINE" | cut -f2)")
      done
      IFS="$OLD_IFS"
      ;;
  esac
done

N_SELECTED=${#SELECTED_IDS[@]}
echo
echo ">>> Selected $N_SELECTED job(s):"
for i in "${!SELECTED_IDS[@]}"; do
  printf "  [%d] job_id=%-10s  %s\n" "$((i+1))" "${SELECTED_IDS[$i]}" "${SELECTED_NAMES[$i]}"
done

# ── 6. Cross-job dependencies ──────────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Cross-job dependencies"
echo "─────────────────────────────────────────────────────────────────────"
echo "For each selected job, list which OTHER selected jobs it depends on"
echo "(by their number above, comma-separated). Press Enter for none."
echo

DEPS_FOR_JOB=()
for i in "${!SELECTED_NAMES[@]}"; do
  NAME="${SELECTED_NAMES[$i]}"
  while true; do
    read -r -p "  [$((i+1))] '$NAME' depends on: " RAW_DEPS
    DEP_NAMES=""
    BAD=false
    if [ -n "$RAW_DEPS" ]; then
      OLD_IFS="$IFS"; IFS=','
      for didx in $RAW_DEPS; do
        IFS="$OLD_IFS"
        didx="${didx// /}"
        [ -z "$didx" ] && continue
        if [ "$didx" = "$((i+1))" ]; then
          echo "    (skipped self-reference: $didx)"; continue
        fi
        if ! echo "$didx" | grep -qE '^[0-9]+$'; then
          echo "    Non-numeric: '$didx' — please re-enter."; BAD=true; break
        fi
        if [ "$didx" -lt 1 ] || [ "$didx" -gt "$N_SELECTED" ]; then
          echo "    Out of range: $didx (have 1..$N_SELECTED) — please re-enter."; BAD=true; break
        fi
        D_NAME="${SELECTED_NAMES[$((didx-1))]}"
        DEP_NAMES="${DEP_NAMES}${D_NAME}|"
      done
      IFS="$OLD_IFS"
      [ "$BAD" = "true" ] && continue
      DEP_NAMES="${DEP_NAMES%|}"
    fi
    DEPS_FOR_JOB+=("$DEP_NAMES")
    break
  done
done

# ── 7. Confirm before scaffolding ──────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Ready to scaffold."
echo "─────────────────────────────────────────────────────────────────────"
echo "  Project:  $PROJECT"
echo "  Host:     $DATABRICKS_HOST"
echo "  Jobs:     $N_SELECTED selected"
for i in "${!SELECTED_NAMES[@]}"; do
  NAME="${SELECTED_NAMES[$i]}"
  DEPS="${DEPS_FOR_JOB[$i]}"
  if [ -n "$DEPS" ]; then
    DEPS_PRETTY="${DEPS//|/, }"
    printf "    • %s   (deps: %s)\n" "$NAME" "$DEPS_PRETTY"
  else
    printf "    • %s\n" "$NAME"
  fi
done
echo
if ! confirm "Proceed?"; then
  echo "Aborted."
  exit 0
fi

# ── 8. Scaffold ────────────────────────────────────────────────────────
PARTIAL_PROJECT_PATH="$PWD/$PROJECT"
echo
echo ">>> Scaffolding Dagster project at $PROJECT ..."
uvx create-dagster@latest project "$PROJECT" --no-uv-sync >/dev/null
cd "$PROJECT"
PKG=$(ls src/ | head -1)
if [ -z "$PKG" ]; then
  echo "ERROR: create-dagster didn't produce a src/<pkg>/ folder."
  exit 1
fi

echo ">>> Installing dagster-databricks ..."
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q dagster-databricks

# ── 9. defs.yaml ───────────────────────────────────────────────────────
mkdir -p "src/$PKG/defs/databricks_workspace"
DEFS_YAML="src/$PKG/defs/databricks_workspace/defs.yaml"

{
  echo "type: dagster_databricks.DatabricksWorkspaceComponent"
  echo "attributes:"
  echo "  databricks_filter:"
  echo "    include_jobs:"
  echo "      job_ids:"
  for jid in "${SELECTED_IDS[@]}"; do
    echo "        - $jid"
  done

  HAS_DEPS=false
  for d in "${DEPS_FOR_JOB[@]}"; do
    if [ -n "$d" ]; then HAS_DEPS=true; break; fi
  done

  if [ "$HAS_DEPS" = "true" ]; then
    echo "  asset_overrides:"
    for i in "${!SELECTED_NAMES[@]}"; do
      NAME="${SELECTED_NAMES[$i]}"
      DEPS="${DEPS_FOR_JOB[$i]}"
      if [ -n "$DEPS" ]; then
        echo "    \"$NAME\":"
        echo "      depends_on:"
        OLD_IFS="$IFS"; IFS='|'
        for dn in $DEPS; do
          IFS="$OLD_IFS"
          echo "        - \"$dn\""
        done
        IFS="$OLD_IFS"
      fi
    done
  fi
} > "$DEFS_YAML"

# ── 10. .env.demo (mode 600, gitignored) ───────────────────────────────
cat > .env.demo <<EOF
export DATABRICKS_HOST='$DATABRICKS_HOST'
export DATABRICKS_TOKEN='$DATABRICKS_TOKEN'
EOF
chmod 600 .env.demo
if ! grep -q "^\.env\.demo$" .gitignore 2>/dev/null; then
  echo ".env.demo" >> .gitignore
fi

# Validate defs.yaml parses
echo ">>> Validating generated defs.yaml ..."
if ! uv run --quiet python -c "import yaml,sys; yaml.safe_load(open('$DEFS_YAML'))" 2>/dev/null; then
  echo "WARNING: $DEFS_YAML may not be valid YAML. Inspect manually."
fi

PARTIAL_PROJECT_PATH=""  # success — disable cleanup-on-interrupt

# ── 11. Summary + next steps ───────────────────────────────────────────
cat <<MSG

════════════════════════════════════════════════════════════════════════
  Setup complete.
════════════════════════════════════════════════════════════════════════

Next:
    cd $PROJECT
    source .env.demo
    uv run dg check defs    # validate the YAML loads
    uv run dg dev           # opens Dagster UI at http://localhost:3000

Generated:
  $DEFS_YAML
  .env.demo  (mode 600 — contains your token; gitignored)

Bringing in $N_SELECTED Databricks job(s) as Dagster assets:
MSG

for i in "${!SELECTED_NAMES[@]}"; do
  NAME="${SELECTED_NAMES[$i]}"
  DEPS="${DEPS_FOR_JOB[$i]}"
  if [ -n "$DEPS" ]; then
    DEPS_PRETTY="${DEPS//|/, }"
    printf "  • job_id=%s  %s   (deps: %s)\n" "${SELECTED_IDS[$i]}" "$NAME" "$DEPS_PRETTY"
  else
    printf "  • job_id=%s  %s\n" "${SELECTED_IDS[$i]}" "$NAME"
  fi
done

cat <<MSG

Each Databricks Job materializes as a Dagster asset. The lineage graph in
'dg dev' shows the cross-job dependencies you configured. Click-to-
materialize triggers the Databricks Job and streams run status into the
Dagster timeline.

To add more jobs later:    edit databricks_filter.include_jobs.job_ids in $DEFS_YAML
To change dependencies:    edit the asset_overrides block in the same file
MSG
