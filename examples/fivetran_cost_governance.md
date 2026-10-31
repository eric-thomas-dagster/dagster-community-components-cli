# Fivetran cost governance

Catch runaway Fivetran connector costs before the bill arrives, not after. Chains `FivetranUsageObservationSensorComponent` with a budget-threshold check and `SlackMessageSendComponent`.

```
fivetran_usage_observer (sensor, demo_mode)
         |  (emits AssetObservation deltas per table)
         v
over_budget_fivetran_tables → fivetran_budget_alert
  (budget-threshold glue)       (Slack, dry_run)
```

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_fivetran_cost_governance_demo.sh | bash
```

Cost: $0. `demo_mode: true` on the sensor, `dry_run: true` on the Slack step — no Fivetran account, no warehouse, no Slack workspace needed.

## Why this is two separate verification steps, not one `dg launch`

Sensors run on the Dagster **daemon** (`dg dev`, or a deployed schedule/sensor daemon), not via `dg launch` — there's no clean way to make a sensor "tick" from a one-shot CLI command the way an asset materializes. So this demo verifies the sensor's real tick logic directly via `dagster.build_sensor_context()` — the exact technique [this component's own test suite uses](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/observations/fivetran_usage_observation_sensor/tests/test_change_detection.py) — rather than requiring you to run a live daemon process just to see this demo work.

The budget-check → Slack-alert half is then demoed as a real one-shot `dg launch`, using the same synthetic MAR numbers the sensor's own `demo_mode` produces at tick 0 (hardcoded in a small glue asset, `over_budget_fivetran_tables`). No generic "read an asset observation and alert on it" component exists in the registry, so this step is ordinary Dagster user code, not a new component. In production, this glue asset would instead call `context.instance.get_latest_observation_event(asset_key)` for each tracked table — since the sensor runs continuously via the daemon, there'd be real observation history to read by the time this asset runs.

## What it does

1. **`fivetran_usage_observation_sensor`** (`demo_mode: true`) reports Monthly Active Rows as a **delta**, not a cumulative level — correct for Dagster+ Insights custom metrics, which sum whatever's reported under a key every tick. Verified directly: tick 1 reports all 3 tables' initial totals; tick 2 reports only the deltas for the 2 tables that actually changed, and **silently skips** the unchanged one (`postgres_orders.customers`, flat at 4200) — exactly the behavior documented in the component's own module docstring.
2. A small **budget-threshold glue asset** (`over_budget_fivetran_tables`) flags `postgres_orders.customers` (4200 rows) as over a 2,000-row demo budget.
3. **`slack_message_send`** (`dry_run: true`) logs the alert instead of sending it — flip to `false` with a real `SLACK_BOT_TOKEN` for live sending.

## Verified

Fresh end-to-end run, byte-for-byte as shipped:

```
TICK 1 (every table reports its initial total as a delta):
  postgres_orders/orders              delta= 1000  month-to-date=1000
  postgres_orders/customers           delta= 4200  month-to-date=4200
  salesforce/opportunity              delta=  300  month-to-date=300
TICK 2 (only CHANGED tables emit; postgres_orders.customers is unchanged -> correctly skipped):
  postgres_orders/orders              delta=  250  month-to-date=1250
  salesforce/opportunity              delta=   15  month-to-date=315
```

...followed by a real `dg launch` → `RUN_SUCCESS`, with the dry-run Slack alert firing exactly for the over-budget table.

## Production variants

- Set `demo_mode: false`, a real `resource_key` (any Dagster resource exposing `.get_connection()`), and your real fully-qualified `INCREMENTAL_MAR` table name — then run the Dagster daemon so the sensor actually ticks on `check_interval_seconds`.
- Replace the glue asset with one reading real observation history instead of hardcoded demo numbers.
- Register `fivetran/mar` as a Dagster+ Insights custom metric to chart MAR cost over time across every synced table.
