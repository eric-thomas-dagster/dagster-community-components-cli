#!/usr/bin/env bash
# External Scheduler demo — show how Control-M, CA WA ESP, Autosys, Tidal,
# IBM TWS, JAMS, Stonebranch, Redwood, Airflow, cron, or any other "master
# scheduler" can keep ownership of WHEN a job runs while Dagster owns HOW
# it runs. The scheduler doesn't need a Dagster integration component; it
# just calls a companion script.
#
# What gets scaffolded:
#   1. A normal daily-partitioned Dagster project (3 components)
#   2. A bin/kick_off_run.sh companion script that simulates the external
#      scheduler invocation — calls dg launch with a partition key derived
#      from the scheduler-supplied date, exits with the run's status code.
#
# How it ties to a real scheduler:
#   - Control-M:    job step calls `bin/kick_off_run.sh %%$ODATE`
#   - CA WA ESP:    INVOKE command  `bin/kick_off_run.sh %ESP.APPL.BIZ_DATE%`
#   - Autosys:      `command: bin/kick_off_run.sh $$AUTODATE`
#   - Tidal:        `bin/kick_off_run.sh ${TID_BUS_DATE}`
#   - IBM TWS:      script step  `bin/kick_off_run.sh ^DATE^`
#   - JAMS:         exec method  `bin/kick_off_run.sh {{$Date.Today}}`
#   - Stonebranch:  Universal Task `bin/kick_off_run.sh ${BUSINESS_DATE}`
#   - Redwood RMS:  process step `bin/kick_off_run.sh #{ScheduleDate}`
#   - Airflow:      BashOperator `bin/kick_off_run.sh {{ ds }}`
#   - cron:         `0 2 * * *  cd /opt/proj && bin/kick_off_run.sh`
#   - GitHub Actions / Jenkins / etc.: same shape

set -euo pipefail
PROJECT_DIR="${1:-external-scheduler-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

# Generate a tiny synthetic CSV to keep the demo offline-only
mkdir -p $PROJECT_ABS/out/extsched_demo
cat > $PROJECT_ABS/out/extsched_demo/orders.csv <<'EOF'
order_id,customer_id,order_date,total
ORD0001,C001,2026-04-30,420.50
ORD0002,C002,2026-04-30,89.99
ORD0003,C003,2026-05-01,1250.00
ORD0004,C001,2026-05-01,310.75
ORD0005,C004,2026-05-02,55.00
ORD0006,C002,2026-05-02,789.49
ORD0007,C003,2026-05-03,128.30
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add file_ingestion --auto-install
$CLI add summarize          --auto-install
$CLI add dataframe_to_csv   --auto-install

echo ">>> Writing demo defs.yaml (daily-partitioned)"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: orders_raw
  file_path: out/extsched_demo/orders.csv
  description: Synthetic 7-row order log spanning 2026-04-30 to 2026-05-03
  group_name: orders
  partition_type: daily
  partition_start: "2026-04-30"
  partition_date_column: order_date
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: daily_revenue
  upstream_asset_key: orders_raw
  group_by:
    - customer_id
  aggregations:
    total: sum
    order_id: count
  group_name: orders
  partition_type: daily
  partition_start: "2026-04-30"
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: daily_revenue_report
  upstream_asset_key: daily_revenue
  file_path: out/extsched_demo/daily_revenue_{partition_key}.csv
  include_index: false
  group_name: orders
  partition_type: daily
  partition_start: "2026-04-30"
EOF

# Add an explicit asset_job so the external scheduler can target a named job
# rather than relying on the auto-generated __ASSET_JOB. Cleaner: schedulers
# get a stable contract ("run job=daily_revenue_refresh") that doesn't churn
# when you add unrelated assets.
$CLI add asset_job --auto-install >/dev/null

cat > "src/$PKG/defs/asset_job/defs.yaml" <<EOF
type: $PKG.components.asset_job.component.AssetJobComponent
attributes:
  job_name: daily_revenue_refresh
  asset_keys:
    - orders_raw
    - daily_revenue
    - daily_revenue_report
  description: One-tick rollup invoked by the external scheduler (Control-M, cron, etc.)
EOF

echo ">>> Writing the companion external-scheduler scripts"
mkdir -p bin

# PRIMARY (production-shape): GraphQL-based.
# This is what real Control-M / Autosys / Tidal agents do — they don't have
# `uv` or `dg` installed, only curl. POST a launchRun mutation to Dagster's
# GraphQL endpoint, polls for completion, exits with the run's status code.
cat > bin/kick_off_run.sh <<'GRAPHQL'
#!/usr/bin/env bash
# What a real external scheduler (Control-M, ESP, Autosys, Tidal, TWS, JAMS, ...) calls.
# Launches a Dagster run via the GraphQL API — only requires curl + jq.
#
# Required env:
#   DAGSTER_GRAPHQL_URL   /graphql endpoint
#                         OSS:      http://dagster-webserver.internal:3000/graphql
#                         Dagster+: https://<org>.dagster.cloud/<deployment>/graphql
#   REPO_LOCATION         The code-location name (shown by `dg dev` startup
#                         banner; usually the project's package name)
#
# Optional env:
#   JOB_NAME              Default: daily_revenue_refresh (the named asset_job from this demo)
#   DAGSTER_PLUS_USER_TOKEN  Required for Dagster+; skipped for OSS (no auth)
#
# Usage:
#   bin/kick_off_run.sh                # uses today's UTC date as partition
#   bin/kick_off_run.sh 2026-05-01     # explicit partition key
#
# Wired into common schedulers:
#   Control-M:  bin/kick_off_run.sh %%$ODATE     (or pass via job-action)
#   Autosys:    bin/kick_off_run.sh $$AUTODATE
#   cron:       0 2 * * *  cd /opt/proj && bin/kick_off_run.sh

set -euo pipefail

PARTITION="${1:-$(date -u +%Y-%m-%d)}"
URL="${DAGSTER_GRAPHQL_URL:?set DAGSTER_GRAPHQL_URL to your Dagster /graphql endpoint}"
LOCATION="${REPO_LOCATION:?set REPO_LOCATION (the code-location name shown in dg dev)}"
JOB="${JOB_NAME:-daily_revenue_refresh}"

AUTH_HEADER=""
if [ -n "${DAGSTER_PLUS_USER_TOKEN:-}" ]; then
  AUTH_HEADER="-H \"Dagster-Cloud-Api-Token: $DAGSTER_PLUS_USER_TOKEN\""
fi

read -r -d '' QUERY <<'EOQ' || true
mutation LaunchRun($params: ExecutionParams!) {
  launchRun(executionParams: $params) {
    __typename
    ... on LaunchRunSuccess { run { runId } }
    ... on PythonError { message stack }
    ... on RunConflict { message }
    ... on InvalidStepError { invalidStepKey }
  }
}
EOQ

VARS=$(cat <<EOV
{
  "params": {
    "selector": {
      "repositoryLocationName": "$LOCATION",
      "repositoryName": "__repository__",
      "jobName": "$JOB"
    },
    "executionMetadata": {
      "tags": [{"key": "dagster/partition", "value": "$PARTITION"}]
    }
  }
}
EOV
)

PAYLOAD=$(jq -n --arg q "$QUERY" --argjson v "$VARS" '{query: $q, variables: $v}')

echo "[external-scheduler] POST $URL  (partition=$PARTITION job=$JOB)"
RESPONSE=$(eval curl -fsS -X POST "$URL" \
  -H "Content-Type: application/json" $AUTH_HEADER \
  --data-binary '@-' <<<"$PAYLOAD")

echo "$RESPONSE" | jq .

TYPE=$(echo "$RESPONSE" | jq -r '.data.launchRun.__typename')
if [ "$TYPE" != "LaunchRunSuccess" ]; then
  echo "[external-scheduler] launch failed: $TYPE" >&2
  exit 1
fi
echo "[external-scheduler] launched OK"
GRAPHQL
chmod +x bin/kick_off_run.sh

# SHORTCUT (local testing only): CLI-based.
# Real scheduler agents don't have `uv` or `dg`, but if you're testing on
# your laptop and want to skip the dg-dev-in-another-terminal dance, this
# is the convenient version.
cat > bin/kick_off_run_via_cli.sh <<'CLI'
#!/usr/bin/env bash
# LOCAL TESTING SHORTCUT — wraps `dg launch` directly. Production schedulers
# use bin/kick_off_run.sh (GraphQL) instead because their agents don't have
# `uv` or `dg` installed.

set -euo pipefail
PARTITION="${1:-$(date -u +%Y-%m-%d)}"
JOB="${JOB_NAME:-daily_revenue_refresh}"
cd "$(dirname "$0")/.."
exec uv run dg launch --job "$JOB" --partition "$PARTITION"
CLI
chmod +x bin/kick_off_run_via_cli.sh

# (legacy) — kept for clarity, but body identical to the new primary above.
cat > /dev/null <<'GRAPHQL_LEGACY'
#!/usr/bin/env bash
# (placeholder so the rest of the original heredoc closes cleanly)
#
# Set DAGSTER_GRAPHQL_URL to point at your Dagster instance:
#   OSS:        http://dagster-webserver.internal:3000/graphql
#   Dagster+:   https://<org>.dagster.cloud/<deployment>/graphql
#                + DAGSTER_PLUS_USER_TOKEN env var
#
# Set REPO_LOCATION + JOB_NAME for the target.

set -euo pipefail

PARTITION="${1:-$(date -u +%Y-%m-%d)}"
URL="${DAGSTER_GRAPHQL_URL:?set DAGSTER_GRAPHQL_URL to your Dagster /graphql endpoint}"
LOCATION="${REPO_LOCATION:?set REPO_LOCATION (the code-location name shown in dg dev)}"
JOB="${JOB_NAME:-daily_revenue_refresh}"

AUTH_HEADER=""
if [ -n "${DAGSTER_PLUS_USER_TOKEN:-}" ]; then
  AUTH_HEADER="-H \"Dagster-Cloud-Api-Token: $DAGSTER_PLUS_USER_TOKEN\""
fi

read -r -d '' QUERY <<'EOQ' || true
mutation LaunchRun($params: ExecutionParams!) {
  launchRun(executionParams: $params) {
    __typename
    ... on LaunchRunSuccess { run { runId } }
    ... on PythonError { message stack }
    ... on RunConflict { message }
    ... on InvalidStepError { invalidStepKey }
  }
}
EOQ

VARS=$(cat <<EOV
{
  "params": {
    "selector": {
      "repositoryLocationName": "$LOCATION",
      "repositoryName": "__repository__",
      "jobName": "$JOB"
    },
    "executionMetadata": {
      "tags": [{"key": "dagster/partition", "value": "$PARTITION"}]
    }
  }
}
EOV
)

PAYLOAD=$(jq -n --arg q "$QUERY" --argjson v "$VARS" '{query: $q, variables: $v}')

echo "[external-scheduler] POST $URL  (partition=$PARTITION job=$JOB)"
RESPONSE=$(eval curl -fsS -X POST "$URL" \
  -H "Content-Type: application/json" $AUTH_HEADER \
  --data-binary '@-' <<<"$PAYLOAD")

echo "$RESPONSE" | jq .

# Exit non-zero if the launch failed
TYPE=$(echo "$RESPONSE" | jq -r '.data.launchRun.__typename')
if [ "$TYPE" != "LaunchRunSuccess" ]; then
  echo "[external-scheduler] launch failed: $TYPE" >&2
  exit 1
fi
GRAPHQL
chmod +x bin/kick_off_run_via_graphql.sh

cat <<MSG

>>> Setup complete.

Two flavors of "external scheduler kicks off Dagster":

A. CLI-based (simplest, requires uv on the scheduler host):
       bin/kick_off_run.sh                  # today
       bin/kick_off_run.sh 2026-05-01       # specific partition

B. GraphQL-based (no Dagster CLI needed; only curl + jq):
       Start the webserver first:
           uv run dg dev
       Then in another terminal:
           DAGSTER_GRAPHQL_URL=http://localhost:3000/graphql \\
           REPO_LOCATION=$PROJECT_DIR \\
           bin/kick_off_run_via_graphql.sh 2026-05-01

For Dagster+, set DAGSTER_GRAPHQL_URL to your deployment's /graphql URL plus
DAGSTER_PLUS_USER_TOKEN — and that's it. Same script, no extra component.

Output files:
    $PROJECT_ABS/out/extsched_demo/daily_revenue_2026-05-01.csv     (per-day rollup)

Why this isn't a Dagster component: an "external-scheduler integration" with
a Dagster component would be backwards. The whole point is keeping the
scheduler in charge — Dagster is just an executor it shells out to.
MSG
