#!/usr/bin/env bash
# Precisely Connect ETL — sensor-only demo scaffold.
#
# WHAT THIS DEMONSTRATES
#   The `precisely_job_sensor` community component. Precisely owns the run;
#   Dagster polls the documented Job Status endpoint and emits a RunRequest
#   when a run reaches terminal SUCCESS.
#
# Asset graph:
#   precisely_downstream_job  (regular Dagster @job — no-op stub)
#   precisely_etl_done        (sensor that triggers the job on SUCCESS)
#
# COST: $0 — sensor starts in 'stopped' state. No Precisely creds required
#       for the dg-dev compile-check; flip default_status to 'running' once
#       you've got a real job_run_id.

set -euo pipefail
PROJECT_DIR="${1:-precisely-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11+
uv add -q requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing precisely_job_sensor"
$CLI add precisely_job_sensor --auto-install

echo ">>> Writing sensor defs.yaml"
mkdir -p "src/$PKG/defs/precisely_job_sensor"
cat > "src/$PKG/defs/precisely_job_sensor/defs.yaml" <<EOF
type: $PKG.components.precisely_job_sensor.component.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_run_id: "REPLACE-WITH-REAL-RUN-ID"
  host_env_var: PRECISELY_HOST
  api_token_env_var: PRECISELY_API_TOKEN
  job_name: precisely_downstream_job
  minimum_interval_seconds: 60
  default_status: stopped
EOF

echo ">>> Adding a downstream Dagster job for the sensor to trigger"
mkdir -p "src/$PKG/defs/precisely_downstream"
cat > "src/$PKG/defs/precisely_downstream/definitions.py" <<EOF
"""No-op downstream job — the sensor fires a RunRequest at this job when a
Precisely Connect ETL run reaches terminal SUCCESS. Replace the op body
with whatever you'd run after Precisely lands new data."""
import dagster as dg


@dg.op
def receive_precisely_signal(context: dg.OpExecutionContext) -> None:
    config = context.op_config or {}
    context.log.info(
        f"Precisely run {config.get('precisely_job_run_id', '(unset)')} "
        f"finished with status {config.get('precisely_status', '(unset)')}."
    )
    context.log.info("Run downstream work here.")


@dg.job
def precisely_downstream_job():
    receive_precisely_signal()


defs = dg.Definitions(jobs=[precisely_downstream_job])
EOF

echo ""
echo "============================================================"
echo "Precisely sensor-only demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export PRECISELY_HOST='https://your-precisely-host'        # placeholder OK for compile-check"
echo "  export PRECISELY_API_TOKEN='your-token'                    # placeholder OK for compile-check"
echo "  uv run dg dev                                              # UI at http://localhost:3000"
echo ""
echo "The sensor is registered in 'stopped' state with a placeholder run-id."
echo "Once you have a real Precisely job-run ID:"
echo ""
echo "  1. Edit src/$PKG/defs/precisely_job_sensor/defs.yaml"
echo "  2. Set job_run_id to the real Precisely run-id"
echo "  3. Set default_status: running"
echo "  4. Restart dg dev — the sensor polls and fires on terminal SUCCESS"
echo ""
