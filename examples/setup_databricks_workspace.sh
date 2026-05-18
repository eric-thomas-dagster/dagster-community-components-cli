#!/usr/bin/env bash
# Interactive setup: bring an existing Databricks workspace's Jobs into a
# Dagster project — using the official `dagster-databricks` integration's
# DatabricksWorkspaceComponent. Asks the user for everything they might
# want to customize and generates a working project with the wiring done.
#
# What it does:
#   1. Asks for project name
#   2. Asks for Databricks host (uses $DATABRICKS_HOST if set)
#   3. Asks for Databricks personal access token (uses $DATABRICKS_TOKEN if set)
#   4. Lists every job in the workspace (Jobs API 2.1)
#   5. User picks which job IDs to bring in
#   6. For each selected job, asks which other selected jobs it depends on
#      (so Dagster models the cross-job dependencies)
#   7. Scaffolds a Dagster project, installs dagster-databricks, writes
#      defs.yaml with `databricks_filter.include_jobs.job_ids` + any
#      `asset_overrides.<job>.depends_on` from the dependency answers
#
# REQUIRES: uvx, curl, jq
# COST: $0 (script-side) — your Databricks workspace handles job runs as usual.

set -euo pipefail

# ── Pre-flight ─────────────────────────────────────────────────────────
command -v uvx >/dev/null || { echo "ERROR: uvx not found. Install uv: https://docs.astral.sh/uv/"; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl required"; exit 1; }
command -v jq >/dev/null || { echo "ERROR: jq required (brew install jq / apt install jq)"; exit 1; }

echo "════════════════════════════════════════════════════════════════════"
echo "  Databricks workspace → Dagster project setup"
echo "  (official dagster-databricks.DatabricksWorkspaceComponent)"
echo "════════════════════════════════════════════════════════════════════"
echo

# ── 1. Project name ────────────────────────────────────────────────────
DEFAULT_PROJECT="databricks-dagster"
read -p "Project name [$DEFAULT_PROJECT]: " PROJECT
PROJECT="${PROJECT:-$DEFAULT_PROJECT}"
PROJECT="${PROJECT// /-}"  # spaces → hyphens for create-dagster

# ── 2. Databricks host ─────────────────────────────────────────────────
if [ -n "${DATABRICKS_HOST:-}" ]; then
  read -p "Databricks host [$DATABRICKS_HOST]: " HOST_INPUT
  DATABRICKS_HOST="${HOST_INPUT:-$DATABRICKS_HOST}"
else
  read -p "Databricks host (e.g. https://dbc-abc123.cloud.databricks.com): " DATABRICKS_HOST
fi
DATABRICKS_HOST="${DATABRICKS_HOST%/}"  # strip trailing /

# ── 3. Databricks token ────────────────────────────────────────────────
if [ -n "${DATABRICKS_TOKEN:-}" ]; then
  read -p "Use \$DATABRICKS_TOKEN from environment? [Y/n]: " USE_ENV_TOKEN
  USE_ENV_TOKEN="${USE_ENV_TOKEN:-Y}"
  if [ "${USE_ENV_TOKEN^^}" = "N" ] 2>/dev/null || [ "$(echo "$USE_ENV_TOKEN" | tr '[:lower:]' '[:upper:]')" = "N" ]; then
    read -sp "Databricks personal access token: " DATABRICKS_TOKEN
    echo
  fi
else
  read -sp "Databricks personal access token (input hidden): " DATABRICKS_TOKEN
  echo
fi

# ── 4. Fetch jobs from workspace ───────────────────────────────────────
echo
echo ">>> Fetching jobs from $DATABRICKS_HOST ..."
JOBS_JSON=$(curl -sf -H "Authorization: Bearer $DATABRICKS_TOKEN" \
  "$DATABRICKS_HOST/api/2.1/jobs/list?limit=100&expand_tasks=false" || true)

if [ -z "$JOBS_JSON" ]; then
  echo "ERROR: Could not reach Databricks. Check host URL + token (must have Jobs:Read)."
  exit 1
fi

# Parse: one line per job → "<job_id>\t<name>"
JOB_LIST=$(echo "$JOBS_JSON" | jq -r '.jobs[]? | "\(.job_id)\t\(.settings.name // "(no name)")"' | sort -k2 -t$'\t')
JOB_COUNT=$(echo "$JOB_LIST" | grep -c "" || true)
if [ -z "$JOB_LIST" ] || [ "$JOB_COUNT" -eq 0 ]; then
  echo "ERROR: No jobs found in workspace (or token lacks Jobs:Read permission)."
  exit 1
fi

echo
echo "Available jobs in workspace ($JOB_COUNT total, max 100 listed):"
echo "$JOB_LIST" | awk -F'\t' '{printf "  [%2d] job_id=%s  %s\n", NR, $1, $2}'
echo

# ── 5. Select jobs to bring into Dagster ───────────────────────────────
read -p "Select job numbers (comma-separated, 'all', or 'q' to quit): " SELECTION
if [ "$SELECTION" = "q" ]; then
  echo "Aborted."; exit 0
fi

SELECTED_IDS=()
SELECTED_NAMES=()
if [ "$SELECTION" = "all" ]; then
  while IFS=$'\t' read -r jid jname; do
    SELECTED_IDS+=("$jid")
    SELECTED_NAMES+=("$jname")
  done <<< "$JOB_LIST"
else
  IFS=',' read -ra IDX_ARRAY <<< "$SELECTION"
  for idx in "${IDX_ARRAY[@]}"; do
    idx="${idx// /}"
    [ -z "$idx" ] && continue
    LINE=$(echo "$JOB_LIST" | sed -n "${idx}p")
    if [ -z "$LINE" ]; then
      echo "  (skipped invalid index: $idx)"
      continue
    fi
    SELECTED_IDS+=("$(echo "$LINE" | cut -f1)")
    SELECTED_NAMES+=("$(echo "$LINE" | cut -f2)")
  done
fi

N_SELECTED=${#SELECTED_IDS[@]}
if [ "$N_SELECTED" -eq 0 ]; then
  echo "ERROR: No valid jobs selected."; exit 1
fi

echo
echo ">>> Selected $N_SELECTED job(s):"
for i in "${!SELECTED_IDS[@]}"; do
  printf "  [%d] job_id=%s  %s\n" "$((i+1))" "${SELECTED_IDS[$i]}" "${SELECTED_NAMES[$i]}"
done

# ── 6. Ask for cross-job dependencies ──────────────────────────────────
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
  read -p "  [$((i+1))] '$NAME' depends on: " RAW_DEPS
  DEP_NAMES=""
  if [ -n "$RAW_DEPS" ]; then
    IFS=',' read -ra DEP_IDX_ARRAY <<< "$RAW_DEPS"
    for didx in "${DEP_IDX_ARRAY[@]}"; do
      didx="${didx// /}"
      [ -z "$didx" ] && continue
      [ "$didx" = "$((i+1))" ] && continue  # ignore self-deps
      if ! [[ "$didx" =~ ^[0-9]+$ ]]; then continue; fi
      if [ "$didx" -lt 1 ] || [ "$didx" -gt "$N_SELECTED" ]; then continue; fi
      D_NAME="${SELECTED_NAMES[$((didx-1))]}"
      DEP_NAMES="${DEP_NAMES}${D_NAME}|"
    done
    DEP_NAMES="${DEP_NAMES%|}"
  fi
  DEPS_FOR_JOB+=("$DEP_NAMES")
done

# ── 7. Scaffold project ────────────────────────────────────────────────
echo
echo ">>> Scaffolding Dagster project at $PROJECT ..."
uvx create-dagster@latest project "$PROJECT" --no-uv-sync >/dev/null
cd "$PROJECT"
PKG=$(ls src/ | head -1)

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q dagster-databricks

# ── 8. Write defs.yaml ─────────────────────────────────────────────────
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

  # asset_overrides only if any job has a dep
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
        # Quote job-name keys (Databricks job names can contain spaces / dots / etc.)
        echo "    \"$NAME\":"
        echo "      depends_on:"
        IFS='|' read -ra DA <<< "$DEPS"
        for dn in "${DA[@]}"; do
          echo "        - \"$dn\""
        done
      fi
    done
  fi
} > "$DEFS_YAML"

# ── 9. Write .env.demo (mode 600 — contains the token) ─────────────────
cat > .env.demo <<EOF
export DATABRICKS_HOST='$DATABRICKS_HOST'
export DATABRICKS_TOKEN='$DATABRICKS_TOKEN'
EOF
chmod 600 .env.demo

# Ensure .env.demo is gitignored
if ! grep -q "^\.env\.demo$" .gitignore 2>/dev/null; then
  echo ".env.demo" >> .gitignore
fi

# ── 10. Summary ────────────────────────────────────────────────────────
cat <<MSG

════════════════════════════════════════════════════════════════════════
  Setup complete.
════════════════════════════════════════════════════════════════════════

    cd $PROJECT
    source .env.demo
    uv run dg check defs
    uv run dg dev         # opens Dagster UI at http://localhost:3000

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

Each Databricks Job materializes as a Dagster asset in the UI. The
lineage graph shows the dependencies you configured. Click to
materialize → Dagster triggers the corresponding Databricks Job and
streams its run status into the Dagster timeline.

To add more jobs later: edit $DEFS_YAML and add to databricks_filter.include_jobs.job_ids.
To change dependencies later: edit the asset_overrides block in that same file.
MSG
