#!/usr/bin/env bash
# Snowflake single-entity components — companion to the workspace demo.
#
# WHAT THIS DEMONSTRATES
#   6 single-entity Snowflake components wired against the same demo
#   entities that setup_snowflake_environment.sh seeds. Use this when
#   you want fine-grained per-entity control instead of the
#   auto-discovery workspace component.
#
#   - snowflake_task_execute_asset           (EXECUTE TASK DAILY_REFRESH_ORDERS)
#   - snowflake_task_completion_sensor       (poll TASK_HISTORY; emit AssetMaterialization)
#   - snowflake_stored_procedure_call_asset  (CALL TRANSFORM_CUSTOMERS())
#   - snowflake_dynamic_table_refresh_asset  (REFRESH CUSTOMER_SUMMARY_DT)
#   - external_snowflake_openflow_flow       (declare CUSTOMER_SYNC as external)
#   - snowflake_openflow_status_sensor       (poll TELEMETRY for CUSTOMER_SYNC)
#
# REQUIRED ENV VARS (set before running)
#   SNOWFLAKE_ACCOUNT     Snowflake account identifier
#   SNOWFLAKE_USER        Snowflake username
#   SNOWFLAKE_PASSWORD    Password (or use PAT / keypair env vars)
#   SNOWFLAKE_WAREHOUSE   e.g. DAGSTER_DEMO_WH
#   SNOWFLAKE_ROLE        e.g. SYSADMIN
#
# COST: only the Snowflake credit cost of materializing the assets you launch.

set -euo pipefail
PROJECT_DIR="${1:-snowflake-single-entity-demo}"
SF_DATABASE="${SNOWFLAKE_DATABASE:-DAGSTER_DEMO}"
SF_SCHEMA="${SNOWFLAKE_SCHEMA:-STAGING}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11+
uv add -q snowflake-connector-python
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 single-entity Snowflake components"
for c in snowflake_task_execute_asset snowflake_stored_procedure_call_asset \
         snowflake_dynamic_table_refresh_asset snowflake_task_completion_sensor \
         external_snowflake_openflow_flow snowflake_openflow_status_sensor; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for each component"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# 1. Task execute asset — Dagster-driven EXECUTE TASK
write_yaml "snowflake_task_execute_asset" "type: $PKG.components.snowflake_task_execute_asset.component.SnowflakeTaskExecuteAssetComponent
attributes:
  asset_key: snowflake/tasks/refresh_orders
  task_name: DAILY_REFRESH_ORDERS
  database: $SF_DATABASE
  schema: $SF_SCHEMA
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
  password_env_var: SNOWFLAKE_PASSWORD
  warehouse_env_var: SNOWFLAKE_WAREHOUSE
  role_env_var: SNOWFLAKE_ROLE
  group_name: snowflake
  description: 'Refresh ORDERS via EXECUTE TASK. Materialization history also reflects Snowflake-scheduler-fired runs (via the paired completion sensor).'"

# 2. Task completion sensor — paired with the asset above
write_yaml "snowflake_task_completion_sensor" "type: $PKG.components.snowflake_task_completion_sensor.component.SnowflakeTaskCompletionSensorComponent
attributes:
  sensor_name: refresh_orders_completion
  task_name: DAILY_REFRESH_ORDERS
  database: $SF_DATABASE
  schema: $SF_SCHEMA
  asset_key: snowflake/tasks/refresh_orders     # SAME as execute asset above
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
  password_env_var: SNOWFLAKE_PASSWORD
  warehouse_env_var: SNOWFLAKE_WAREHOUSE
  role_env_var: SNOWFLAKE_ROLE
  minimum_interval_seconds: 60
  default_status: stopped                       # flip to running once seeded"

# 3. Stored procedure call asset
write_yaml "snowflake_stored_procedure_call_asset" "type: $PKG.components.snowflake_stored_procedure_call_asset.component.SnowflakeStoredProcedureCallAssetComponent
attributes:
  asset_key: snowflake/procs/transform_customers
  procedure_name: TRANSFORM_CUSTOMERS
  database: $SF_DATABASE
  schema: ANALYTICS
  arguments: []
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
  password_env_var: SNOWFLAKE_PASSWORD
  warehouse_env_var: SNOWFLAKE_WAREHOUSE
  role_env_var: SNOWFLAKE_ROLE
  group_name: snowflake"

# 4. Dynamic table refresh asset
write_yaml "snowflake_dynamic_table_refresh_asset" "type: $PKG.components.snowflake_dynamic_table_refresh_asset.component.SnowflakeDynamicTableRefreshAssetComponent
attributes:
  asset_key: snowflake/dt/customer_summary
  dynamic_table_name: CUSTOMER_SUMMARY_DT
  database: $SF_DATABASE
  schema: ANALYTICS
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
  password_env_var: SNOWFLAKE_PASSWORD
  warehouse_env_var: SNOWFLAKE_WAREHOUSE
  role_env_var: SNOWFLAKE_ROLE
  group_name: snowflake"

# 5. External OpenFlow flow asset (declare-only — pair with sensor)
write_yaml "external_snowflake_openflow_flow" "type: $PKG.components.external_snowflake_openflow_flow.component.ExternalSnowflakeOpenflowFlowAsset
attributes:
  asset_key: snowflake/openflow/customer_sync
  flow_name: customer_sync
  group_name: snowflake
  description: 'OpenFlow process group syncing customer records. Activity surfaced via openflow_status_sensor.'"

# 6. OpenFlow status sensor — paired with the external asset above
write_yaml "snowflake_openflow_status_sensor" "type: $PKG.components.snowflake_openflow_status_sensor.component.SnowflakeOpenflowStatusSensorComponent
attributes:
  sensor_name: customer_sync_activity
  flow_name: customer_sync
  asset_key: snowflake/openflow/customer_sync   # SAME as external asset above
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
  password_env_var: SNOWFLAKE_PASSWORD
  warehouse_env_var: SNOWFLAKE_WAREHOUSE
  role_env_var: SNOWFLAKE_ROLE
  minimum_interval_seconds: 120
  default_status: stopped"

echo ""
echo "============================================================"
echo "Snowflake single-entity demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "Project structure:"
echo "  src/$PKG/defs/"
echo "    ├── snowflake_task_execute_asset/         (materializable)"
echo "    ├── snowflake_task_completion_sensor/     (stopped by default)"
echo "    ├── snowflake_stored_procedure_call_asset/ (materializable)"
echo "    ├── snowflake_dynamic_table_refresh_asset/ (materializable)"
echo "    ├── external_snowflake_openflow_flow/     (declare-only catalog node)"
echo "    └── snowflake_openflow_status_sensor/     (stopped by default)"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  uv run dg dev                          # UI at http://localhost:3000"
echo ""
echo "Materialize the three action assets from the UI:"
echo "  - snowflake/tasks/refresh_orders       (calls EXECUTE TASK)"
echo "  - snowflake/procs/transform_customers  (calls the proc)"
echo "  - snowflake/dt/customer_summary        (refreshes the dynamic table)"
echo ""
echo "Then turn on the two sensors (Sensors tab in the UI) to also surface"
echo "Snowflake-scheduler-fired runs + OpenFlow activity in the catalog."
echo ""
