# Snowflake — single-entity components (companion to `snowflake_workspace`)
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

This walkthrough exists as the **single-entity counterpart** to the [`snowflake_workspace.md`](snowflake_workspace.md) auto-discovery demo. For most teams the workspace component is the right shape — it scans your Snowflake account and brings every task / stored proc / dynamic table / stream / pipe / external table / alert / MV into Dagster's catalog in one go.

**When to use this walkthrough instead:**

- You want to wire **one specific Snowflake task** (not all of them) into a Dagster project
- You're embedding a single Snowflake entity into a larger pipeline and don't want a full account scan
- You're building a focused demo that exercises a known task / proc / dynamic table by name
- You want fine-grained per-entity control over `deps`, `automation_condition`, `partitions_def`, etc.

Both shapes can co-exist in the same project. The workspace component handles auto-discovery; single-entity components handle the deliberate exceptions.

## Components used

- `external_snowflake_openflow_flow`
- `snowflake_dynamic_table_refresh_asset`
- `snowflake_openflow_status_sensor`
- `snowflake_stored_procedure_call_asset`
- `snowflake_task_completion_sensor`
- `snowflake_task_execute_asset`

## Six new single-entity components

| Component | Purpose | Replaces in `snowflake_workspace` |
|---|---|---|
| [`snowflake_task_execute_asset`](https://dagster-component-ui.vercel.app/c/snowflake_task_execute_asset) | `EXECUTE TASK <db>.<schema>.<name>` on one task | The auto-discovered task assets |
| [`snowflake_stored_procedure_call_asset`](https://dagster-component-ui.vercel.app/c/snowflake_stored_procedure_call_asset) | `CALL <db>.<schema>.<proc>(args)` on one stored procedure | The auto-discovered stored-proc assets |
| [`snowflake_dynamic_table_refresh_asset`](https://dagster-component-ui.vercel.app/c/snowflake_dynamic_table_refresh_asset) | `ALTER DYNAMIC TABLE ... REFRESH` on one DT | The auto-discovered dynamic-table assets |
| [`snowflake_task_completion_sensor`](https://dagster-component-ui.vercel.app/c/snowflake_task_completion_sensor) | Poll `TASK_HISTORY` for one task; emit `AssetMaterialization` on each new SUCCESS | The workspace observation sensor (task half) |
| [`external_snowflake_openflow_flow`](https://dagster-component-ui.vercel.app/c/external_snowflake_openflow_flow) | Declare one OpenFlow process group as a catalog node (`AssetSpec` only) | The auto-discovered OpenFlow observable assets |
| [`snowflake_openflow_status_sensor`](https://dagster-component-ui.vercel.app/c/snowflake_openflow_status_sensor) | Poll `SNOWFLAKE.TELEMETRY.EVENTS` for one flow's activity; emit `AssetMaterialization` | The workspace observation sensor (OpenFlow half) |

## Pairing pattern

For tasks and OpenFlow flows the natural pairing is **action asset + completion sensor**:

```
snowflake_task_execute_asset (Dagster materializes)
    ↑
    │  same asset_key
    ↓
snowflake_task_completion_sensor (Snowflake-scheduler-fired runs)
```

This gives you a single catalog asset whose materialization history reflects **both** Dagster-driven `EXECUTE TASK` calls AND Snowflake's autonomous cron firings of the same task. Customers see one timeline, regardless of who triggered the run.

For OpenFlow, the pattern uses an **external asset** (Dagster never executes OpenFlow flows; OpenFlow owns the runtime):

```
external_snowflake_openflow_flow (declare-only AssetSpec)
    ↑
    │  same asset_key
    ↓
snowflake_openflow_status_sensor (poll TELEMETRY.EVENTS; emit AssetMaterialization)
```

## Demo

```bash
# Set up Snowflake env (see setup_snowflake_environment.sh first)
export SNOWFLAKE_ACCOUNT=<your-account>
export SNOWFLAKE_USER=<your-user>
export SNOWFLAKE_PASSWORD=<your-password>      # or PAT / keypair
export SNOWFLAKE_WAREHOUSE=DAGSTER_DEMO_WH
export SNOWFLAKE_ROLE=SYSADMIN

bash setup_snowflake_single_entity_demo.sh single-entity-demo
cd single-entity-demo
uv run dg dev
```

The setup script scaffolds a project with all 6 single-entity components wired to entities the [`setup_snowflake_environment.sh`](setup_snowflake_environment.sh) seed creates (`DAILY_REFRESH_ORDERS`, `TRANSFORM_CUSTOMERS`, `CUSTOMER_SUMMARY_DT`, etc.). `dg dev` loads the project; you can materialize each asset individually.

## Comparison: workspace vs. single-entity at a glance

| Aspect | `snowflake_workspace` | Single-entity components |
|---|---|---|
| **What it does** | Scans an account; auto-includes every task / proc / DT / stream / pipe / external table / alert / MV / Openflow flow | Brings one named entity into the project |
| **Configuration weight** | One `defs.yaml`, one set of credentials, ~30 lines | One `defs.yaml` per entity, ~15 lines each |
| **Best for** | Snowflake-heavy teams who want "everything in Dagster" | Selective adoption; focused demos; per-entity tuning |
| **Asset key control** | Auto-derived from entity name | Customer sets explicit `asset_key` per defs.yaml |
| **Per-entity `deps` / `partitions_def`** | Via the workspace `<entity_name>_overrides:` shape | Native — set on the component directly |
| **Account permissions needed** | `SHOW` privileges across the schema for auto-discovery | Just what's needed for the one entity |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_single_entity_demo.sh \
  -o setup_snowflake_single_entity_demo.sh
bash setup_snowflake_single_entity_demo.sh
```

## See also

- [`snowflake_workspace.md`](snowflake_workspace.md) — primary walkthrough using the workspace auto-discovery component
- [`snowflake_demo_account_requirements.md`](snowflake_demo_account_requirements.md) — permission / tier reference for the full Snowflake surface
- [`setup_snowflake_environment.sh`](setup_snowflake_environment.sh) — environment seed that creates the demo entities this walkthrough exercises
