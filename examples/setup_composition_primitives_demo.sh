#!/usr/bin/env bash
# Composition primitives demo — wire up 5 declare-and-run job components
# into one project. No external auth, no SaaS dependencies. SQLite for
# the SQL primitives, httpbin.org for the HTTP primitives, an in-package
# Python function for the callable primitive.
#
# WHAT THIS DEMONSTRATES
#   Dagster runs lots of "small jobs" alongside assets — nightly VACUUMs,
#   uptime heartbeats, calling internal services, kicking off cleanup
#   tasks. The community registry has a family of `*_job` components
#   that wrap each pattern declaratively (YAML, no Python). This demo
#   installs five of them in one project and proves they all load +
#   execute.
#
# Components exercised:
#   - python_callable_job          — runs a Python function as an op job
#   - http_webhook_job             — fires an HTTP request as an op job
#   - observability_heartbeat_job  — multi-target heartbeat (HTTP / Slack / Teams / PagerDuty)
#   - warehouse_maintenance_job    — runs N SQL statements sequentially
#   - sql_command_job              — runs a single multi-statement SQL block
#
# Asset graph: none. This is the *jobs* side of Dagster — schedules + on-demand
# kicks that don't produce data assets.
#
# COST: \$0 — SQLite + httpbin.org/get, no auth, no SaaS.

set -euo pipefail
PROJECT_DIR="${1:-composition-primitives-demo}"
SQLITE_DB="/tmp/${PROJECT_DIR}.db"

echo ">>> Cleaning prior SQLite db"
rm -f "$SQLITE_DB"

echo ">>> Pre-creating SQLite seed tables (so VACUUM/SELECT statements have something to chew on)"
python3 - <<PY
import sqlite3
db = "$SQLITE_DB"
con = sqlite3.connect(db)
con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
con.executemany("INSERT INTO orders (total) VALUES (?)", [(9.99,), (14.50,), (3.25,)])
con.executemany("INSERT INTO customers (name) VALUES (?)", [("Alice",), ("Bob",)])
con.commit()
con.close()
print(f"Seeded {db}")
PY

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q requests sqlalchemy

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 composition-primitive components"
for c in python_callable_job http_webhook_job observability_heartbeat_job \
         warehouse_maintenance_job sql_command_job; do
  $CLI add $c --auto-install
done

echo ">>> Writing in-package Python callable for python_callable_job"
mkdir -p "src/$PKG/tasks"
cat > "src/$PKG/tasks/__init__.py" <<'PY'
def cleanup(days: int = 30) -> dict:
    """A no-op cleanup task — proves python_callable_job wires + executes."""
    return {"deleted_rows": 0, "older_than_days": days, "status": "ok"}
PY

echo ">>> Writing 5 defs.yaml (one per primitive)"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: nightly_cleanup
  callable_path: $PKG.tasks:cleanup
  kwargs:
    days: 30"

write_yaml "http_webhook_job" "type: $PKG.components.http_webhook_job.component.HttpWebhookJobComponent
attributes:
  job_name: ping_status_endpoint
  url: https://httpbin.org/status/200
  method: GET
  expected_status: 200
  timeout_seconds: 15"

write_yaml "observability_heartbeat_job" "type: $PKG.components.observability_heartbeat_job.component.ObservabilityHeartbeatJobComponent
attributes:
  job_name: platform_heartbeat
  message: 'community-components demo heartbeat'
  http_heartbeat_url: https://httpbin.org/get"

write_yaml "warehouse_maintenance_job" "type: $PKG.components.warehouse_maintenance_job.component.WarehouseMaintenanceJobComponent
attributes:
  job_name: warehouse_nightly_maintenance
  connection_string_env: WAREHOUSE_URL
  statements:
    - 'ANALYZE orders'
    - 'ANALYZE customers'
    - 'SELECT COUNT(*) FROM orders'
  autocommit: true
  fail_fast: false"

write_yaml "sql_command_job" "type: $PKG.components.sql_command_job.component.SqlCommandJobComponent
attributes:
  job_name: refresh_summaries
  connection_string_env: WAREHOUSE_URL
  sql: |
    CREATE TABLE IF NOT EXISTS daily_revenue (day TEXT, total REAL);
    DELETE FROM daily_revenue;
    INSERT INTO daily_revenue SELECT date('now'), SUM(total) FROM orders;"

echo ">>> Exporting WAREHOUSE_URL pointing at the seeded SQLite db"
echo "export WAREHOUSE_URL='sqlite:///$SQLITE_DB'" > .env.demo

cat <<MSG

>>> Setup complete.

Validate the 5 primitives loaded:
    cd $PROJECT_DIR
    export WAREHOUSE_URL='sqlite:///$SQLITE_DB'
    uv run dg check defs
    uv run dg list defs

Launch any of the 5 jobs (no schedules are RUNNING by default — pure on-demand):
    uv run dg launch --job nightly_cleanup              # Python callable
    uv run dg launch --job ping_status_endpoint         # HTTP GET to httpbin
    uv run dg launch --job platform_heartbeat           # HTTP GET, with Slack/Teams/PD optional
    uv run dg launch --job warehouse_nightly_maintenance # SQL statements vs sqlite
    uv run dg launch --job refresh_summaries            # multi-stmt SQL block vs sqlite

Browse them in the UI:
    uv run dg dev   # http://localhost:3000 → Jobs tab

This demo never needs Slack / PagerDuty / Postgres / Redis. SQLite + httpbin.org
covers all five. To re-target at a real warehouse: change WAREHOUSE_URL to your
DSN (postgresql://, snowflake://, etc.). The components themselves are
agnostic — they just speak SQLAlchemy or HTTP.
MSG
