#!/usr/bin/env bash
# sql_monitor demo — DuckDB end-to-end.
#
# sql_monitor is the "sensor that triggers a job on every new row"
# shape (vs sql_observation_sensor which just observes counts). Each
# row past the watermark becomes one RunRequest with the row's payload
# threaded through as op config. Perfect fit for a duckdb + tiny
# python_callable_job demo.
#
# Shape (2 components + 1 tiny Python callable for the target op):
#   sql_monitor (sensor, polls watermark_column, emits RunRequest per new row)
#     └── python_callable_job (fires the callable with the row payload)
#
# The DuckDB fixture seeds a `sales_events` table with 3 rows; the
# sensor's first tick emits 3 RunRequests. Insert a 4th row later →
# next tick emits exactly 1 RunRequest.

set -euo pipefail
PROJECT_DIR="${1:-sql-monitor-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q 'sqlalchemy>=2.0.0' 'duckdb>=1.0.0' 'duckdb-engine>=0.13.0'
uv add -q 'yarl<1.24'   # workaround: yarl 1.24.0 only ships cp310 wheels
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 2 community components"
$CLI add sql_monitor          --auto-install
$CLI add python_callable_job  --auto-install

# ── Seed DuckDB with a small events table + a monotonic id + a
#    updated_at watermark. The sensor uses updated_at to detect new
#    rows and id as the RunRequest key.
DUCKDB_PATH="$PROJECT_ABS/out/events.duckdb"
DUCKDB_URL="duckdb:///$DUCKDB_PATH"
echo ">>> Seeding $DUCKDB_PATH"
uv run python - <<PY
import duckdb
c = duckdb.connect("$DUCKDB_PATH")
c.execute("DROP TABLE IF EXISTS sales_events")
c.execute("""
CREATE TABLE sales_events (
    id           INTEGER PRIMARY KEY,
    customer     VARCHAR,
    amount_usd   DOUBLE,
    updated_at   TIMESTAMP
);
""")
c.execute("""
INSERT INTO sales_events VALUES
    (1, 'Acme',    120.00, '2026-09-10 09:00:00'),
    (2, 'Widgets', 240.00, '2026-09-10 09:05:00'),
    (3, 'Cogs',     87.50, '2026-09-10 09:12:00');
""")
n = c.execute("SELECT COUNT(*) FROM sales_events").fetchone()[0]
print(f"    seeded {n} events")
PY

# ── Tiny Python callable the sensor-triggered job invokes. Reads
#    op config (populated per-row by the sensor) and logs it. Prod
#    equivalent would fan out to a message queue, mark a downstream
#    row as processed, etc.
mkdir -p "src/$PKG/processors"
cat > "src/$PKG/processors/__init__.py" <<'PY'
PY
cat > "src/$PKG/processors/handle_event.py" <<'PY'
"""Fired once per new sales_events row detected by sql_monitor."""
from typing import Any


def handle(context: Any, **_kwargs) -> None:
    """The sensor threads the row payload into op config; log it."""
    op_cfg = getattr(context.op_execution_context, "op_config", None) if hasattr(context, "op_execution_context") else None
    op_cfg = op_cfg or getattr(context, "op_config", None) or {}
    context.log.info(f"handling event: {op_cfg}")
PY

echo ">>> Writing defs.yaml"

# sql_monitor — polls sales_events, emits one RunRequest per new row.
cat > "src/$PKG/defs/sql_monitor/defs.yaml" <<EOF
type: $PKG.components.sql_monitor.component.SQLMonitorSensorComponent
attributes:
  sensor_name: sales_events_monitor
  connection_string_env_var: DUCKDB_URL
  table_name: sales_events
  watermark_column: updated_at
  id_column: id
  job_name: process_sales_event
  batch_size: 100
  minimum_interval_seconds: 30
  default_status: stopped
EOF

# The job the sensor fires (once per new row).
cat > "src/$PKG/defs/python_callable_job/defs.yaml" <<EOF
type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_sales_event
  callable_path: $PKG.processors.handle_event:handle
EOF

cat <<MSG

>>> Setup complete.

Export the connection string:

    export DUCKDB_URL='$DUCKDB_URL'

Validate:

    cd $PROJECT_DIR && uv run dg check defs

Fire the sensor once + inspect the RunRequests (one per new row):

    cd $PROJECT_DIR && DUCKDB_URL='$DUCKDB_URL' uv run python -c "
import os; os.environ['DUCKDB_URL']='$DUCKDB_URL'
from dagster import build_sensor_context, DagsterInstance
from ${PKG}.definitions import defs
resolved = defs() if callable(defs) else defs
sensor = resolved.get_sensor_def('sales_events_monitor')
result = sensor(build_sensor_context(instance=DagsterInstance.ephemeral()))
print(f'{len(result.run_requests)} run requests emitted')
for rr in result.run_requests:
    print(f'  run_key={rr.run_key}  config={rr.run_config}')
"

Or start the UI (sensor is default-stopped; toggle on):

    cd $PROJECT_DIR && DUCKDB_URL='$DUCKDB_URL' uv run dg dev

Insert a new row to demo incremental detection:

    uv run python -c "
import duckdb
duckdb.connect('$DUCKDB_PATH').execute(
    \"INSERT INTO sales_events VALUES (99, 'NewCo', 999.00, '2026-09-10 12:00:00')\"
)
print('inserted row; next sensor tick emits 1 RunRequest for it')
"
MSG
