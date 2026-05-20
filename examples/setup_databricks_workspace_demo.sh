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

# This script is interactive — prompts the user for project name, host,
# token, job selection, dep wiring, schedule, etc. (Databricks setup
# can't be canned into a single static demo; every workspace is shaped
# differently.) Refuse to run when stdin isn't a TTY (e.g. piped from
# curl) — prompts would silently fail with stdin exhausted.
if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════
  This is an INTERACTIVE script — it can't be run via `curl | bash`.
  It prompts you for project name, Databricks host, token, which
  jobs to bring in, dep wiring between jobs, and the cron schedule.

  Download it first, then run from a terminal:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_databricks_workspace_demo.sh -o setup_databricks_workspace_demo.sh
    chmod +x setup_databricks_workspace_demo.sh
    ./setup_databricks_workspace_demo.sh

  Tip: set $DATABRICKS_HOST and $DATABRICKS_TOKEN in your shell first
  and the script will pick them up as defaults.
════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  # Drain any remaining stdin so the upstream `curl` finishes writing
  # without SIGPIPE (which would emit a misleading "curl: (56)" error
  # after our clean message above).
  cat >/dev/null 2>&1 || true
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

# snake_case helper — matches dagster_databricks.utils.snake_case exactly.
snake_case() {
  local s="$1"
  s=$(echo "$s" | sed -E 's/([a-z0-9])([A-Z])/\1_\2/g')
  s=$(echo "$s" | sed -E 's/[^a-zA-Z0-9]+/_/g')
  s=$(echo "$s" | tr '[:upper:]' '[:lower:]' | sed -E 's/^_+|_+$//g')
  echo "$s"
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
# `confirm_only_once` controls the "Use $DATABRICKS_TOKEN from env?" prompt —
# we only ask once per token entry, not on every retry, so verification
# failures don't bounce the user back to that question.
TOKEN_ASKED_ENV=0
while true; do
  if [ -n "${DATABRICKS_TOKEN:-}" ] && [ "$TOKEN_ASKED_ENV" = "0" ]; then
    if confirm "Use \$DATABRICKS_TOKEN from environment?"; then
      :
    else
      DATABRICKS_TOKEN=""
    fi
    TOKEN_ASKED_ENV=1
  fi
  if [ -z "${DATABRICKS_TOKEN:-}" ]; then
    read -r -s -p "Databricks personal access token (input hidden): " DATABRICKS_TOKEN
    echo
  fi
  if [ -z "$DATABRICKS_TOKEN" ]; then
    echo "  Token cannot be empty."
    continue
  fi

  # Verify by hitting an endpoint that needs only basic auth scope.
  # NB: curl's %{http_code} format already prints "000" on connection
  # failure. Need `|| true` so the script's `set -e` doesn't kill us
  # on the non-zero curl exit; need to NOT use `|| echo "000"` (that
  # would append, producing "000000" alongside the %{http_code} output).
  echo "  Verifying token against $DATABRICKS_HOST ..."
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $DATABRICKS_TOKEN" \
    "$DATABRICKS_HOST/api/2.0/preview/scim/v2/Me" 2>/dev/null || true)
  [ -z "$HTTP_CODE" ] && HTTP_CODE="000"

  # Successful verification — break out. For any failure, fall through to
  # the "didn't verify — continue anyway?" prompt so a customer who's
  # testing the script flow (bogus values, offline, behind a VPN, …) can
  # press through with one keystroke.
  case "$HTTP_CODE" in
    200) echo "  ✓ Token authenticates."; break ;;
    404) echo "  ✓ Endpoint missing (HTTP 404) — your host may be older, continuing anyway."; break ;;
    401|403) echo "  ✗ HTTP $HTTP_CODE — token rejected by $DATABRICKS_HOST." ;;
    000)     echo "  ✗ Could not reach $DATABRICKS_HOST (DNS / network / hostname wrong)." ;;
    *)       echo "  ✗ Unexpected HTTP $HTTP_CODE from $DATABRICKS_HOST." ;;
  esac

  # Single "continue anyway?" prompt. Default is N — so a customer just
  # hitting Enter goes back to re-entering, but a `y` accepts bogus values
  # for testing / offline / VPN scenarios.
  echo "    These values weren't verified. Common reasons: typo, behind a"
  echo "    VPN/proxy, offline, or you're testing the script flow."
  read -r -p "    Continue with this host + token anyway? [y/N]: " CONT_ANYWAY
  case "${CONT_ANYWAY:-n}" in
    y|Y|yes|YES|Yes)
      echo "  ⚠ Skipping verification — host + token accepted as-is."
      break
      ;;
    *)
      echo "    What would you like to do?"
      echo "      [t] Re-enter token"
      echo "      [h] Re-enter host  (current: $DATABRICKS_HOST)"
      echo "      [q] Quit"
      read -r -p "    Choice [t/h/q]: " RETRY_CHOICE
      case "${RETRY_CHOICE:-t}" in
        h|H)
          read -r -p "    New Databricks host (e.g. https://dbc-abc123.cloud.databricks.com): " NEW_HOST
          [ -n "$NEW_HOST" ] && DATABRICKS_HOST="${NEW_HOST%/}"
          ;;
        q|Q) echo "  Aborting."; exit 1 ;;
        *)   DATABRICKS_TOKEN=""; TOKEN_ASKED_ENV=1 ;;  # re-enter token
      esac
      ;;
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
    echo "  ✗ Jobs API call failed on page $PAGE (host unreachable or token lacks Jobs:Read)."
    echo
    echo "  How would you like to proceed?"
    echo "    [1] Enter job IDs manually  (recommended if you know which jobs to bring in,"
    echo "                                 or your token has jobs/get but not jobs/list scope)"
    echo "    [2] Use fixture jobs        (testing the script flow — fake job IDs, project"
    echo "                                 won't run against a real workspace)"
    echo "    [3] Quit"
    read -r -p "  Choice [1/2/3]: " API_FAIL_CHOICE
    case "${API_FAIL_CHOICE:-1}" in
      1|"")
        # Manual job-ID entry. The script's later per-job task-lookup will try
        # the jobs/get API for each ID — that may or may not succeed depending
        # on token scope / host. The graceful fallback there defaults to a
        # single "main" task per job.
        echo
        read -r -p "  Job IDs (comma-separated, e.g. '12345,67890'): " MANUAL_IDS
        if [ -z "$MANUAL_IDS" ]; then
          echo "  No IDs entered. Aborting."
          exit 1
        fi
        # Build a minimal ALL_JOBS_JSON with the manual IDs (name defaults to "job_<id>")
        MANUAL_JSON='{"jobs":['
        FIRST_ENTRY=1
        OLD_IFS="$IFS"; IFS=','
        for jid in $MANUAL_IDS; do
          IFS="$OLD_IFS"
          jid="${jid// /}"
          [ -z "$jid" ] && continue
          if [ "$FIRST_ENTRY" -eq 1 ]; then FIRST_ENTRY=0; else MANUAL_JSON="$MANUAL_JSON,"; fi
          # Try to fetch the real job name via jobs/get; if that fails, fall back to "job_<id>"
          NAME=$(curl -sf -H "Authorization: Bearer $DATABRICKS_TOKEN" \
            "$DATABRICKS_HOST/api/2.1/jobs/get?job_id=$jid" 2>/dev/null \
            | jq -r '.settings.name // empty' 2>/dev/null || echo "")
          [ -z "$NAME" ] && NAME="job_$jid"
          MANUAL_JSON="$MANUAL_JSON{\"job_id\":$jid,\"settings\":{\"name\":\"$NAME\"}}"
          OLD_IFS="$IFS"; IFS=','
        done
        IFS="$OLD_IFS"
        MANUAL_JSON="$MANUAL_JSON]}"
        ALL_JOBS_JSON="$MANUAL_JSON"
        echo "  ✓ Loaded $(echo "$ALL_JOBS_JSON" | jq '.jobs | length') job(s) from manual entry."
        break
        ;;
      2)
        echo "  ⚠ Using fixture jobs. The scaffolded project will point at fake job IDs."
        # Flag the per-job task lookup to read from ALL_JOBS_JSON instead of hitting the API
        USE_FIXTURE_TASKS=1
        # 5 realistic-shaped fake jobs covering single-task, linear, diamond, and fan-out DAGs.
        ALL_JOBS_JSON='{"jobs":[
          {"job_id":100001,"settings":{"name":"bronze_ingest_orders","tasks":[
            {"task_key":"main"}
          ]}},
          {"job_id":100002,"settings":{"name":"bronze_ingest_customers","tasks":[
            {"task_key":"extract"},
            {"task_key":"land","depends_on":[{"task_key":"extract"}]}
          ]}},
          {"job_id":100003,"settings":{"name":"silver_orders_clean","tasks":[
            {"task_key":"validate"},
            {"task_key":"dedup","depends_on":[{"task_key":"validate"}]},
            {"task_key":"enrich","depends_on":[{"task_key":"dedup"}]}
          ]}},
          {"job_id":100004,"settings":{"name":"silver_customers_clean","tasks":[
            {"task_key":"normalize"},
            {"task_key":"geocode","depends_on":[{"task_key":"normalize"}]},
            {"task_key":"merge","depends_on":[{"task_key":"geocode"}]}
          ]}},
          {"job_id":100005,"settings":{"name":"gold_revenue_summary","tasks":[
            {"task_key":"join_orders_customers"},
            {"task_key":"by_region","depends_on":[{"task_key":"join_orders_customers"}]},
            {"task_key":"by_category","depends_on":[{"task_key":"join_orders_customers"}]},
            {"task_key":"final_report","depends_on":[
              {"task_key":"by_region"},{"task_key":"by_category"}
            ]}
          ]}}
        ]}'
        break
        ;;
      3|q|Q)
        echo "  Aborting."
        exit 1
        ;;
      *)
        echo "  Invalid choice. Aborting."
        exit 1
        ;;
    esac
  fi
  # Skip the next page logic when we just loaded fixtures
  [ "$PAGE_JSON" = "" ] && break
  ALL_JOBS_JSON=$(jq -s '{jobs: (.[0].jobs + .[1].jobs)}' \
    <(echo "$ALL_JOBS_JSON") <(echo "$PAGE_JSON" | jq '{jobs: (.jobs // [])}'))
  NEXT_PAGE_TOKEN=$(echo "$PAGE_JSON" | jq -r '.next_page_token // empty')
  HAS_MORE=$(echo "$PAGE_JSON" | jq -r '.has_more // false')
  if [ -z "$NEXT_PAGE_TOKEN" ] || [ "$HAS_MORE" != "true" ]; then break; fi
  PAGE=$((PAGE + 1))
done

JOB_LIST=$(echo "$ALL_JOBS_JSON" | jq -r '.jobs[]? | "\(.job_id)\t\(.settings.name // "(no name)")"' | sort -t$'\t' -k2)
# Count non-empty lines (echo "" | grep -c "" returns 1 — we want 0)
if [ -z "$JOB_LIST" ]; then
  JOB_COUNT=0
else
  JOB_COUNT=$(printf "%s\n" "$JOB_LIST" | grep -c .)
fi
if [ -z "$JOB_LIST" ] || [ "$JOB_COUNT" -eq 0 ]; then
  echo "ERROR: No jobs found in workspace (or token lacks Jobs:Read permission)."
  exit 1
fi

# ── 5. Select jobs (scale-aware) ───────────────────────────────────────
# Below threshold: show all + pick by number.
# Above threshold: prompt for IDs / glob / paginated 'list'.

SELECTION_THRESHOLD=30
SELECTED_IDS=()
SELECTED_NAMES=()

# Build a display table from a tab-separated subset of JOB_LIST.
# Usage: render_jobs "<subset>" → numbered list to stdout
render_jobs() {
  echo "$1" | awk -F'\t' '{printf "  [%3d] job_id=%-10s  %s\n", NR, $1, $2}'
}

# Take "selection" (comma-list of numbers) against a passed-in tab-separated
# list and append matches to SELECTED_IDS / SELECTED_NAMES.
select_from_list() {
  local pool="$1"
  local selection="$2"
  local pool_count
  if [ -z "$pool" ]; then pool_count=0; else pool_count=$(printf "%s\n" "$pool" | grep -c .); fi
  if [ "$selection" = "all" ] || [ "$selection" = "ALL" ] || [ "$selection" = "All" ]; then
    while IFS=$'\t' read -r jid jname; do
      SELECTED_IDS+=("$jid")
      SELECTED_NAMES+=("$jname")
    done <<< "$pool"
    return 0
  fi
  local OLD_IFS="$IFS"; IFS=','
  for idx in $selection; do
    IFS="$OLD_IFS"
    idx="${idx// /}"
    [ -z "$idx" ] && continue
    if ! echo "$idx" | grep -qE '^[0-9]+$'; then
      echo "  Ignoring non-numeric token: '$idx'"; continue
    fi
    if [ "$idx" -lt 1 ] || [ "$idx" -gt "$pool_count" ]; then
      echo "  Out of range: $idx (have 1..$pool_count)"; continue
    fi
    local line
    line=$(echo "$pool" | sed -n "${idx}p")
    SELECTED_IDS+=("$(echo "$line" | cut -f1)")
    SELECTED_NAMES+=("$(echo "$line" | cut -f2)")
  done
  IFS="$OLD_IFS"
}

# Look up job names by ID. Skips IDs not in the workspace.
select_by_ids() {
  local id_csv="$1"
  local OLD_IFS="$IFS"; IFS=','
  for jid in $id_csv; do
    IFS="$OLD_IFS"
    jid="${jid// /}"
    [ -z "$jid" ] && continue
    if ! echo "$jid" | grep -qE '^[0-9]+$'; then
      echo "  Ignoring non-numeric job_id: '$jid'"; continue
    fi
    local name
    name=$(echo "$JOB_LIST" | awk -F'\t' -v id="$jid" '$1==id {print $2; exit}')
    if [ -z "$name" ]; then
      echo "  job_id $jid not in workspace; skipping"; continue
    fi
    SELECTED_IDS+=("$jid")
    SELECTED_NAMES+=("$name")
  done
  IFS="$OLD_IFS"
}

# Filter JOB_LIST by glob pattern (case-insensitive) against job name.
filter_by_glob() {
  local pattern="$1"
  local pattern_lower
  pattern_lower=$(echo "$pattern" | tr '[:upper:]' '[:lower:]')
  local out=""
  while IFS=$'\t' read -r jid jname; do
    local name_lower
    name_lower=$(echo "$jname" | tr '[:upper:]' '[:lower:]')
    # shellcheck disable=SC2053
    if [[ "$name_lower" == $pattern_lower ]]; then
      out="${out}${jid}	${jname}
"
    fi
  done <<< "$JOB_LIST"
  printf "%s" "${out%$'\n'}"
}

# Paginated display of JOB_LIST: 30 at a time, 'q' to stop browsing.
paginated_browse() {
  local page=1
  local per_page=30
  while true; do
    local start=$((1 + (page - 1) * per_page))
    local end=$((page * per_page))
    [ "$end" -gt "$JOB_COUNT" ] && end=$JOB_COUNT
    [ "$start" -gt "$JOB_COUNT" ] && break
    echo
    echo "Jobs $start..$end of $JOB_COUNT:"
    echo "$JOB_LIST" | sed -n "${start},${end}p" | awk -F'\t' -v s="$start" '{printf "  [%3d] job_id=%-10s  %s\n", s + NR - 1, $1, $2}'
    if [ "$end" -ge "$JOB_COUNT" ]; then break; fi
    read -r -p "  Next $per_page? [Y/n/q to stop browsing]: " NEXT
    case "$NEXT" in
      n|N|q|Q) break ;;
      *) page=$((page + 1)) ;;
    esac
  done
}

echo
echo "Workspace has $JOB_COUNT job(s)."

if [ "$JOB_COUNT" -le "$SELECTION_THRESHOLD" ]; then
  echo
  render_jobs "$JOB_LIST"
  echo
  while [ "${#SELECTED_IDS[@]}" -eq 0 ]; do
    read -r -p "Select job numbers (comma-separated, 'all', or 'q' to quit): " SELECTION
    case "$SELECTION" in
      q|Q) echo "Aborted."; exit 0 ;;
      "")  echo "  Nothing selected." ;;
      *)   select_from_list "$JOB_LIST" "$SELECTION" ;;
    esac
  done
else
  # Large-workspace path: filter / paste IDs / paginated browse
  cat <<MSG

That's too many to list at once. Pick one:

  1. Filter by name pattern  (e.g. 'bronze_*', 'silver_customer*', 'gold_*_v2')
  2. Paste job IDs directly  (comma-separated, e.g. '482631,482632,482638')
  3. Browse paginated        (30 at a time)
  4. Show all                (forces full list — use only if you really want it)
  5. Quit

MSG
  while [ "${#SELECTED_IDS[@]}" -eq 0 ]; do
    read -r -p "Choice [1/2/3/4/5]: " MODE
    case "$MODE" in
      5|q|Q) echo "Aborted."; exit 0 ;;

      1)
        while true; do
          read -r -p "  Filter pattern (glob, e.g. 'bronze_*'; 'r' to pick a different mode): " PATTERN
          if [ "$PATTERN" = "r" ] || [ "$PATTERN" = "R" ]; then break; fi
          if [ -z "$PATTERN" ]; then continue; fi
          FILTERED=$(filter_by_glob "$PATTERN")
          if [ -z "$FILTERED" ]; then FCOUNT=0; else FCOUNT=$(printf "%s\n" "$FILTERED" | grep -c .); fi
          if [ -z "$FILTERED" ] || [ "$FCOUNT" -eq 0 ]; then
            echo "  No jobs match '$PATTERN'. Try a broader pattern."
            continue
          fi
          if [ "$FCOUNT" -gt 200 ]; then
            echo "  Pattern matched $FCOUNT jobs — please narrow ('${PATTERN}' is too broad)."
            continue
          fi
          echo
          echo "  Matched $FCOUNT job(s):"
          render_jobs "$FILTERED"
          echo
          read -r -p "  Select numbers (comma-separated), 'all', or 'r' to re-search: " SUBSEL
          case "$SUBSEL" in
            r|R) continue ;;
            "")  echo "  Nothing selected."; continue ;;
            *)   select_from_list "$FILTERED" "$SUBSEL"; break ;;
          esac
        done
        ;;

      2)
        read -r -p "  Job IDs (comma-separated): " ID_CSV
        if [ -n "$ID_CSV" ]; then
          select_by_ids "$ID_CSV"
        fi
        ;;

      3)
        paginated_browse
        echo
        read -r -p "  Now select by number (comma-separated, 'all', or 'q'): " SELECTION
        case "$SELECTION" in
          q|Q) echo "Aborted."; exit 0 ;;
          "")  echo "  Nothing selected." ;;
          *)   select_from_list "$JOB_LIST" "$SELECTION" ;;
        esac
        ;;

      4)
        echo
        render_jobs "$JOB_LIST"
        echo
        read -r -p "  Select job numbers (comma-separated, 'all', or 'q'): " SELECTION
        case "$SELECTION" in
          q|Q) echo "Aborted."; exit 0 ;;
          "")  echo "  Nothing selected." ;;
          *)   select_from_list "$JOB_LIST" "$SELECTION" ;;
        esac
        ;;

      *)
        echo "  Pick 1, 2, 3, 4, or 5."
        ;;
    esac
  done
fi

N_SELECTED=${#SELECTED_IDS[@]}
echo
echo ">>> Selected $N_SELECTED job(s):"
for i in "${!SELECTED_IDS[@]}"; do
  printf "  [%d] job_id=%-10s  %s\n" "$((i+1))" "${SELECTED_IDS[$i]}" "${SELECTED_NAMES[$i]}"
done

# ── 5b. Fetch tasks AND leaf tasks for each selected job ──────────────
# DatabricksWorkspaceComponent makes one Dagster asset per (job, task).
# We need two pieces of information per job:
#   1. ALL task_keys — for assets_by_job_task_key (one entry per task) and
#      for the cron schedule's asset_keys list on root jobs (so the
#      schedule fires the WHOLE upstream job, not just a partial slice).
#   2. LEAF task_keys (the ones nothing else depends on within the job)
#      — for cross-job dep wiring. Downstream depends on the leaf of
#      upstream because the leaf finishing means the whole job finished.

TASK_KEYS_FOR_JOB=()
LEAVES_FOR_JOB=()
echo
echo ">>> Looking up tasks for each selected job ..."
for i in "${!SELECTED_IDS[@]}"; do
  JID="${SELECTED_IDS[$i]}"
  JNAME="${SELECTED_NAMES[$i]}"
  JOB_DETAIL=$(curl -sf -H "Authorization: Bearer $DATABRICKS_TOKEN" \
    "$DATABRICKS_HOST/api/2.1/jobs/get?job_id=$JID" 2>/dev/null || echo "")
  if [ -z "$JOB_DETAIL" ]; then
    echo "  ⚠ Could not fetch job $JID detail — defaulting to a single task named 'main'."
    TASK_KEYS_FOR_JOB+=("main")
    LEAVES_FOR_JOB+=("main")
    continue
  fi
  TKS=$(echo "$JOB_DETAIL" | jq -r '.settings.tasks[]?.task_key' 2>/dev/null | paste -sd, -)
  if [ -z "$TKS" ]; then
    TKS="main"
    LEAVES="main"
  else
    # A leaf = a task_key not present in any other task's depends_on[].task_key.
    LEAVES=$(echo "$JOB_DETAIL" | jq -r '
      .settings.tasks as $tasks |
      ($tasks | map(.depends_on // [] | .[].task_key) | unique) as $up |
      $tasks | map(.task_key) | map(select(. as $tk | $up | index($tk) | not)) | .[]
    ' 2>/dev/null | paste -sd, -)
    if [ -z "$LEAVES" ]; then
      # Defensive — circular DAG or some weird shape. Fall back to all tasks.
      LEAVES="$TKS"
    fi
  fi
  TASK_KEYS_FOR_JOB+=("$TKS")
  LEAVES_FOR_JOB+=("$LEAVES")
  TASK_COUNT=$(echo "$TKS" | tr ',' '\n' | grep -c .)
  LEAF_COUNT=$(echo "$LEAVES" | tr ',' '\n' | grep -c .)
  if [ "$TASK_COUNT" -eq 1 ]; then
    printf "  job_id=%-10s  %s\n" "$JID" "$JNAME"
  else
    printf "  job_id=%-10s  %s  (%d tasks, leaf: %s)\n" "$JID" "$JNAME" "$TASK_COUNT" "$LEAVES"
  fi
done

# ── 6. Cross-job dependencies ──────────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Cross-job dependencies"
echo "─────────────────────────────────────────────────────────────────────"
echo "These are CROSS-JOB deps (job → job). Within-job task deps are"
echo "auto-discovered by DatabricksWorkspaceComponent from each job's own"
echo "depends_on; don't re-declare them here."
echo
echo "Selected jobs (reference numbers for the prompt below):"
for i in "${!SELECTED_IDS[@]}"; do
  printf "  [%d] job_id=%-10s  %s\n" "$((i+1))" "${SELECTED_IDS[$i]}" "${SELECTED_NAMES[$i]}"
done
echo
echo "For each selected job, list which OTHER selected jobs it depends on"
echo "(by their [N] reference number, comma-separated). Press Enter for none."
echo

DEPS_FOR_JOB=()
# Parallel array DEPS_IDX_FOR_JOB holds the dep INDICES (1-based) for cycle detection.
DEPS_IDX_FOR_JOB=()
for i in "${!SELECTED_NAMES[@]}"; do
  NAME="${SELECTED_NAMES[$i]}"
  while true; do
    read -r -p "  [$((i+1))] '$NAME' depends on: " RAW_DEPS
    DEP_NAMES=""
    DEP_IDXS=""
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
        DEP_IDXS="${DEP_IDXS}${didx},"
      done
      IFS="$OLD_IFS"
      [ "$BAD" = "true" ] && continue
      DEP_NAMES="${DEP_NAMES%|}"
      DEP_IDXS="${DEP_IDXS%,}"
    fi
    DEPS_FOR_JOB+=("$DEP_NAMES")
    DEPS_IDX_FOR_JOB+=("$DEP_IDXS")
    break
  done
done

# Cycle detection — DFS over the (1-based) index graph in DEPS_IDX_FOR_JOB.
# If we find a back-edge, abort with a clear message rather than emitting a
# defs.yaml whose dependency graph would loop at materialization time.
has_cycle() {
  local n=$N_SELECTED
  # 0=unvisited, 1=on-stack, 2=done
  local state=()
  local k
  for ((k=0; k<n; k++)); do state[k]=0; done
  local stack_path=()
  _dfs() {
    local v=$1
    state[v]=1
    stack_path+=("$((v+1))")
    local deps_csv="${DEPS_IDX_FOR_JOB[$v]}"
    [ -z "$deps_csv" ] && { state[v]=2; unset 'stack_path[${#stack_path[@]}-1]'; return 0; }
    local OLD_IFS="$IFS"; IFS=','
    for didx in $deps_csv; do
      IFS="$OLD_IFS"
      local u=$((didx-1))
      if [ "${state[u]}" = "1" ]; then
        stack_path+=("$((u+1))")
        echo "  ⚠ Cycle detected: ${stack_path[*]}"
        return 1
      elif [ "${state[u]}" = "0" ]; then
        _dfs "$u" || return 1
      fi
    done
    IFS="$OLD_IFS"
    state[v]=2
    unset 'stack_path[${#stack_path[@]}-1]'
    return 0
  }
  for ((k=0; k<n; k++)); do
    if [ "${state[k]}" = "0" ]; then
      _dfs "$k" || return 1
    fi
  done
  return 0
}

if ! has_cycle; then
  echo
  echo "    The dependency graph above contains a cycle. Dagster can't materialize"
  echo "    a cyclic asset graph — please re-run and pick non-circular deps."
  echo "    (Tip: within-job task deps are auto-discovered by the component;"
  echo "     only list CROSS-JOB deps here.)"
  exit 1
fi

# ── 7. Orchestration mode ──────────────────────────────────────────────
# How should the assets be triggered? Three options:
#   1. Cron schedule          — runs all selected jobs on a cron (using the
#                               community cron_schedule component)
#   2. Auto-trigger on upstream completion — each downstream job materializes
#                               as soon as its upstream completes (Dagster
#                               AutomationCondition.eager). For job chains
#                               where you want lakeflow-style cascading.
#   3. Manual only            — just lineage; click to materialize / dg launch

echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Orchestration"
echo "─────────────────────────────────────────────────────────────────────"
echo "Downstream jobs (those with upstream deps) will automatically cascade"
echo "via Dagster AutomationCondition.eager — when their upstream completes,"
echo "they fire. No prompts needed for those."
echo
echo "For ROOT jobs (jobs you selected that have no upstream in this project),"
echo "what triggers them?"
echo "  1. Cron schedule  — runs the roots at fixed times → cascades downstream"
echo "  2. Manual only    — you trigger the roots; downstream cascades"
echo

# Identify root jobs (those without any user-specified deps)
ROOT_INDICES=()
for i in "${!SELECTED_NAMES[@]}"; do
  if [ -z "${DEPS_FOR_JOB[$i]}" ]; then
    ROOT_INDICES+=("$i")
  fi
done
N_ROOTS=${#ROOT_INDICES[@]}

if [ "$N_ROOTS" -eq 0 ]; then
  echo "  ⚠ No root jobs detected — every selected job has a dep. Falling back to"
  echo "    manual mode (nothing to schedule)."
  ORCH_MODE="manual"
fi

CRON_EXPR=""
CRON_TZ=""
SCHEDULE_NAME=""
if [ "$N_ROOTS" -gt 0 ]; then
  while [ -z "${ORCH_MODE:-}" ]; do
    read -r -p "Choice [1/2]: " M
    case "$M" in
      1)
        ORCH_MODE="cron"
        read -r -p "  Cron expression [0 2 * * *]: " CRON_EXPR
        CRON_EXPR="${CRON_EXPR:-0 2 * * *}"
        read -r -p "  Timezone [UTC]: " CRON_TZ
        CRON_TZ="${CRON_TZ:-UTC}"
        DEFAULT_SCHED_NAME=$(echo "${PROJECT}_schedule" | tr -c 'a-zA-Z0-9_' '_')
        read -r -p "  Schedule name [$DEFAULT_SCHED_NAME]: " SCHEDULE_NAME
        SCHEDULE_NAME="${SCHEDULE_NAME:-$DEFAULT_SCHED_NAME}"
        ;;
      2) ORCH_MODE="manual" ;;
      *) echo "  Pick 1 or 2." ;;
    esac
  done
fi

# ── 8. Confirm before scaffolding ──────────────────────────────────────
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
echo "  Roots:    $N_ROOTS job(s) with no upstream deps"
case "$ORCH_MODE" in
  cron)   echo "  Trigger:  cron '$CRON_EXPR' (TZ $CRON_TZ) on roots, downstream auto-cascades" ;;
  manual) echo "  Trigger:  manual on roots, downstream auto-cascades when roots complete" ;;
esac
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
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q dagster-databricks

# ── 9. defs.yaml ───────────────────────────────────────────────────────
mkdir -p "src/$PKG/defs/databricks_workspace"
DEFS_YAML="src/$PKG/defs/databricks_workspace/defs.yaml"

# Helper: render ALL of a job's task asset keys (used for cron schedule's
# asset_keys list so the whole upstream job fires when scheduled).
# Returns each as "<clean_job>/<clean_task>", one per line.
asset_keys_for_job_index() {
  local idx="$1"
  local jname="${SELECTED_NAMES[$idx]}"
  local clean_job
  clean_job=$(snake_case "$jname")
  local tks="${TASK_KEYS_FOR_JOB[$idx]}"
  local OLD_IFS="$IFS"; IFS=','
  for tk in $tks; do
    IFS="$OLD_IFS"
    local clean_task
    clean_task=$(snake_case "$tk")
    echo "${clean_job}/${clean_task}"
  done
  IFS="$OLD_IFS"
}

# Helper: render a job's LEAF task asset keys (used for cross-job `deps:`).
# Leaf-only because the leaf finishing means the whole upstream Databricks
# Job finished — the right granularity for "Job 2 depends on Job 1."
leaf_asset_keys_for_job_index() {
  local idx="$1"
  local jname="${SELECTED_NAMES[$idx]}"
  local clean_job
  clean_job=$(snake_case "$jname")
  local leaves="${LEAVES_FOR_JOB[$idx]}"
  local OLD_IFS="$IFS"; IFS=','
  for tk in $leaves; do
    IFS="$OLD_IFS"
    local clean_task
    clean_task=$(snake_case "$tk")
    echo "${clean_job}/${clean_task}"
  done
  IFS="$OLD_IFS"
}

# Helper: lookup a job's index in SELECTED_NAMES by name
index_for_name() {
  local target="$1"
  for j in "${!SELECTED_NAMES[@]}"; do
    if [ "${SELECTED_NAMES[$j]}" = "$target" ]; then
      echo "$j"
      return 0
    fi
  done
  return 1
}

{
  echo "type: dagster_databricks.DatabricksWorkspaceComponent"
  echo "attributes:"
  echo "  workspace:"
  echo "    host: \"{{ env('DATABRICKS_HOST') }}\""
  echo "    token: \"{{ env('DATABRICKS_TOKEN') }}\""
  echo "  databricks_filter:"
  echo "    include_jobs:"
  echo "      job_ids:"
  for jid in "${SELECTED_IDS[@]}"; do
    echo "        - $jid"
  done

  # Emit assets_by_job_task_key for every DOWNSTREAM job (has deps).
  # Each task in the downstream job gets:
  #   - deps: every task asset key of every upstream job
  #   - automation_condition: "{{ dg.AutomationCondition.eager() }}"
  HAS_DEPS=false
  for d in "${DEPS_FOR_JOB[@]}"; do
    if [ -n "$d" ]; then HAS_DEPS=true; break; fi
  done

  if [ "$HAS_DEPS" = "true" ]; then
    echo "  assets_by_job_task_key:"
    for i in "${!SELECTED_NAMES[@]}"; do
      NAME="${SELECTED_NAMES[$i]}"
      DEPS="${DEPS_FOR_JOB[$i]}"
      [ -z "$DEPS" ] && continue
      CLEAN_JOB=$(snake_case "$NAME")

      # Collect upstream LEAF asset keys for each upstream job. Depending on
      # the leaf (vs every task) is the right granularity: the leaf
      # materializing means the whole upstream Databricks Job succeeded.
      UPSTREAM_KEYS=()
      OLD_IFS="$IFS"; IFS='|'
      for dn in $DEPS; do
        IFS="$OLD_IFS"
        UPSTREAM_IDX=$(index_for_name "$dn" || true)
        [ -z "$UPSTREAM_IDX" ] && continue
        while IFS= read -r ak; do
          UPSTREAM_KEYS+=("$ak")
        done < <(leaf_asset_keys_for_job_index "$UPSTREAM_IDX")
      done
      IFS="$OLD_IFS"

      echo "    \"$NAME\":"
      TKS="${TASK_KEYS_FOR_JOB[$i]}"
      OLD_IFS="$IFS"; IFS=','
      for tk in $TKS; do
        IFS="$OLD_IFS"
        CLEAN_TASK=$(snake_case "$tk")
        echo "      \"$tk\":"
        echo "        - key: \"${CLEAN_JOB}/${CLEAN_TASK}\""
        echo "          deps:"
        for uk in "${UPSTREAM_KEYS[@]}"; do
          echo "            - \"$uk\""
        done
        echo "          automation_condition: \"{{ dg.AutomationCondition.eager() }}\""
      done
      IFS="$OLD_IFS"
    done
  fi
} > "$DEFS_YAML"

# Cron schedule for ROOT jobs only — downstream cascades via automation_condition.
if [ "$ORCH_MODE" = "cron" ]; then
  echo ">>> Installing the community cron_schedule component ..."
  # `--auto-install` copies the component source into src/<pkg>/components/cron_schedule/
  # AND writes a default sample defs.yaml at src/<pkg>/defs/cron_schedule/defs.yaml.
  # We then overwrite that defs.yaml with the one targeting THIS project's root jobs.
  uvx --from dagster-community-components-cli dagster-component add cron_schedule --auto-install

  SCHED_YAML="src/$PKG/defs/cron_schedule/defs.yaml"
  {
    echo "type: $PKG.components.cron_schedule.component.CronScheduleComponent"
    echo "attributes:"
    echo "  schedule_name: \"$SCHEDULE_NAME\""
    echo "  cron_expression: \"$CRON_EXPR\""
    echo "  execution_timezone: \"$CRON_TZ\""
    echo "  default_status: RUNNING"
    echo "  asset_keys:"
    for idx in "${ROOT_INDICES[@]}"; do
      while IFS= read -r ak; do
        echo "      - \"$ak\""
      done < <(asset_keys_for_job_index "$idx")
    done
  } > "$SCHED_YAML"
fi

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
MSG
if [ "$ORCH_MODE" = "cron" ]; then
  echo "  src/$PKG/defs/cron_schedule/defs.yaml  (cron: '$CRON_EXPR' $CRON_TZ)"
fi
echo "  .env.demo  (mode 600 — contains your token; gitignored)"
echo
echo "Bringing in $N_SELECTED Databricks job(s) as Dagster assets:"

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

echo
case "$ORCH_MODE" in
  cron)
    cat <<MSG
Orchestration:
  • Cron schedule '$SCHEDULE_NAME' runs '$CRON_EXPR' ($CRON_TZ) — kicks the
    $N_ROOTS root job(s).
  • Downstream jobs auto-cascade via AutomationCondition.eager when their
    upstream completes. No further config needed.

To pause the cron: set default_status: STOPPED in src/$PKG/defs/cron_schedule/defs.yaml.
To pause the cascade: remove 'automation_condition' lines from
$DEFS_YAML (downstream becomes manual-only).
MSG
    ;;
  manual)
    cat <<MSG
Orchestration:
  • Roots run manually — open dg dev → click a root asset → 'Materialize'
    (or 'uv run dg launch --assets "<root_job_name>"').
  • Downstream auto-cascades via AutomationCondition.eager once a root
    finishes.

To add a cron later: copy a CronScheduleComponent defs.yaml into
src/$PKG/defs/cron_schedule/ targeting the root asset keys.
MSG
    ;;
esac

cat <<MSG

Each Databricks Job materializes as a Dagster asset. The lineage graph in
'dg dev' shows the cross-job dependencies you configured. Click-to-
materialize triggers the Databricks Job and streams run status into the
Dagster timeline.

To add more jobs later:    edit databricks_filter.include_jobs.job_ids in $DEFS_YAML
To change dependencies:    edit assets_by_job_task_key in the same file
To change orchestration:   edit assets_by_job_task_key (automation_condition)
                           or the cron schedule's defs.yaml

═══════════════════════════════════════════════════════════════════════
  Deploy to production (Dagster+)
═══════════════════════════════════════════════════════════════════════

Running locally with 'dg dev' is the dev loop. To deploy:

  ▸ Dagster+ Serverless (push from your laptop, fastest):
      uv add --dev dagster-cloud-cli
      uv run dg plus deploy
    Docs:  https://docs.dagster.io/dagster-plus/deployment/serverless

  ▸ Dagster+ Hybrid (CI/CD via GitHub Actions):
      uv add --dev dagster-cloud-cli
      uv run dagster-cloud ci init
      # → scaffolds .github/workflows/dagster-plus-deploy.yml in your repo
      git add .github/ && git commit -m "ci: dagster+ deploy" && git push
      # Then in GitHub repo Settings → Secrets, add:
      #   DAGSTER_CLOUD_API_TOKEN     (from Dagster+ Settings → Tokens)
    Docs:  https://docs.dagster.io/dagster-plus/deployment/code-locations

  ▸ Self-hosted Dagster OSS (k8s / ECS / Docker):
      Build your own image; deploy as a code location to your gRPC server.
    Docs:  https://docs.dagster.io/deployment

IMPORTANT — Databricks credentials in production:
  Do NOT commit .env.demo (already in .gitignore — verify before pushing).
  Dagster+ UI → Deployment → Environment variables → add:
    DATABRICKS_HOST  =  $DATABRICKS_HOST
    DATABRICKS_TOKEN =  <new long-lived service-principal token>
  Don't reuse your personal-access-token in prod — generate a workspace
  service principal token instead.

MSG
