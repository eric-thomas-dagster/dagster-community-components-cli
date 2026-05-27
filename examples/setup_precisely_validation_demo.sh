#!/usr/bin/env bash
# Precisely Connect ETL — two-component demo scaffold (external asset + sensor).
#
# WHAT THIS DEMONSTRATES
#   The Precisely Connect ETL integration is a two-component pattern:
#
#     1. `external_precisely_job` — declare-only AssetSpec. Surfaces the
#         Precisely ETL job as a real Dagster asset in the catalog,
#         marked `dagster.observability_type=external`. Downstream
#         assets can `deps: [...]` against it. Dagster never "runs" it.
#
#     2. `precisely_job_sensor` — polls Precisely's documented
#         `GET /projects/{jobRunId}/status` endpoint. On terminal
#         SUCCESS (`COMPLETED` / `COMPLETED_WITH_WARNINGS`) it:
#           - emits `AssetMaterialization(asset_key=<external asset>)`
#             — lights up materialization history on the external asset
#           - fires a `RunRequest` against a downstream Dagster job
#
#   The asset_key on both components is the same string —
#   `precisely/etl/load_customers` — that's the glue that ties sensor
#   events to the external asset.
#
# Asset graph after scaffold:
#   precisely/etl/load_customers   (external asset, declare-only)
#   precisely_downstream_job       (no-op @job — sensor fires RunRequest here)
#   precisely_etl_done             (sensor — observes + emits materialization)
#
# COST: $0 — sensor starts in 'stopped' state. No Precisely creds required
#       for the dg-dev compile-check; flip default_status to 'running' and
#       set a real job_run_id once you've got one.

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

echo ">>> Installing external_precisely_job (catalog-presence component)"
$CLI add external_precisely_job --auto-install

echo ">>> Installing precisely_job_sensor (status-polling sensor)"
$CLI add precisely_job_sensor --auto-install

# Shared asset key — the sensor's asset_key field MUST match the external
# asset's asset_key field for materialization events to land on the right
# asset in the catalog.
ASSET_KEY="precisely/etl/load_customers"

echo ">>> Writing external_precisely_job defs.yaml"
mkdir -p "src/$PKG/defs/external_precisely_job"
cat > "src/$PKG/defs/external_precisely_job/defs.yaml" <<EOF
type: $PKG.components.external_precisely_job.component.ExternalPreciselyJobAsset
attributes:
  asset_key: $ASSET_KEY
  job_id: "REPLACE-WITH-REAL-JOB-ID"
  host: https://your-precisely-host
  group_name: precisely
  description: |
    Precisely Connect ETL job that lands customer records.
    Materialized externally; Dagster observes via precisely_job_sensor.
EOF

echo ">>> Writing precisely_job_sensor defs.yaml"
mkdir -p "src/$PKG/defs/precisely_job_sensor"
cat > "src/$PKG/defs/precisely_job_sensor/defs.yaml" <<EOF
type: $PKG.components.precisely_job_sensor.component.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_run_id: "REPLACE-WITH-REAL-RUN-ID"
  host_env_var: PRECISELY_HOST
  api_token_env_var: PRECISELY_API_TOKEN
  job_name: precisely_downstream_job
  # asset_key matches the external_precisely_job above — terminal-SUCCESS
  # AssetMaterialization events land on that external asset in the catalog.
  asset_key: $ASSET_KEY
  minimum_interval_seconds: 60
  default_status: stopped
EOF

echo ">>> Adding a downstream Dagster job for the sensor's RunRequest target"
mkdir -p "src/$PKG/defs/precisely_downstream"
cat > "src/$PKG/defs/precisely_downstream/definitions.py" <<EOF
"""No-op downstream job — the sensor fires a RunRequest at this job when a
Precisely Connect ETL run reaches terminal SUCCESS. The richer signal is
the AssetMaterialization on the external asset (see external_precisely_job
defs.yaml); this job just demonstrates the RunRequest side of the sensor
contract. Replace the op body with whatever you'd run after Precisely
lands new data."""
import dagster as dg


@dg.op
def receive_precisely_signal(context: dg.OpExecutionContext) -> None:
    context.log.info(
        "Precisely sensor fired — terminal SUCCESS observed. "
        "Inspect the materialization event on the precisely/etl/load_customers "
        "asset for the Precisely run_id and status."
    )
    context.log.info("Run downstream work here.")


@dg.job
def precisely_downstream_job():
    receive_precisely_signal()


defs = dg.Definitions(jobs=[precisely_downstream_job])
EOF

echo ""
echo "============================================================"
echo "Precisely two-component demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "Components scaffolded:"
echo "  1. external_precisely_job → catalog asset $ASSET_KEY"
echo "  2. precisely_job_sensor   → emits materializations + RunRequests"
echo "  3. precisely_downstream_job → no-op job the sensor fires"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export PRECISELY_HOST='https://your-precisely-host'        # placeholder OK for compile-check"
echo "  export PRECISELY_API_TOKEN='your-token'                    # placeholder OK for compile-check"
echo "  uv run dg dev                                              # UI at http://localhost:3000"
echo ""
echo "The sensor is registered in 'stopped' state with placeholder ids."
echo "Once you have a real Precisely job + job-run ID:"
echo ""
echo "  1. Edit src/$PKG/defs/external_precisely_job/defs.yaml"
echo "       - Set job_id to the stable Precisely job id"
echo "       - Set host to the real Precisely Connect ETL host"
echo "  2. Edit src/$PKG/defs/precisely_job_sensor/defs.yaml"
echo "       - Set job_run_id to the real Precisely run-id"
echo "       - Set default_status: running"
echo "  3. Restart dg dev — the sensor polls and on terminal SUCCESS"
echo "     emits both an AssetMaterialization (visible on the external"
echo "     asset's materialization history) and a RunRequest (visible"
echo "     in the runs tab against precisely_downstream_job)."
echo ""
