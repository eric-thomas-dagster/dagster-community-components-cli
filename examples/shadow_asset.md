# Dual-run new vs old — vendor swap without risking production
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in under a minute (plus `uv run dg launch`
startup overhead).

## What this demo shows

Two assets, one per shape. Both dual-run primary + shadow implementations,
diff outputs, and emit the same `shadow_match` observations. **Production
always uses the primary result** — the shadow only reports.

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_order_totals` (Python decorator) | primary materializes; shadow diff → `shadow_match=false` (mode=dataframe, diff_rows=1) |
| 2 | `yaml_order_totals` (YAML composability) | primary materializes; shadow diff → `shadow_match=false` (mode=dataframe, diff_rows=100) |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when both primary + shadow are Python callables in the same project:

```python
# src/<pkg>/defs/py_order_totals.py
import pandas as pd
import dagster as dg
from dagster_community_components import shadow

def _new_report_impl(context):
    return pd.DataFrame({
        "customer_id": [1, 2, 3],
        "total_usd":  [100.00, 200.00, 305.00],  # last row diverges: 300 vs 305
    })

@dg.asset(group_name="python_decorator")
@shadow(_new_report_impl, enforce_match=False)
def py_order_totals(context):
    return pd.DataFrame({
        "customer_id": [1, 2, 3],
        "total_usd":  [100.00, 200.00, 300.00],  # the primary result — what production sees
    })
```

**Shape 2: YAML composability — the money shot.** `ShadowAssetComponent` wraps **two DCC components side-by-side**. The primary (`wraps:`) materializes; the shadow (`shadow_wraps:`) runs alongside and its output is diffed. Zero Python — perfect for vendor swaps:

```yaml
# src/<pkg>/defs/yaml_order_totals/defs.yaml
type: dagster_community_components.ShadowAssetComponent
attributes:
  enforce_match: false          # false (default) = observe; true = fail-on-mismatch
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_order_totals
      schema_type: customers
      row_count: 100
      random_state: 42
  shadow_wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_order_totals
      schema_type: customers
      row_count: 100
      random_state: 43        # differs → same shape, different rows → mismatch
```

**One asset is registered** (`yaml_order_totals`) — the shadow's assets are NOT registered. Only the primary's assets materialize. The shadow's compute is invoked side-by-side and its return value is diffed.

Add or remove the outer wrap without touching the inner components' config. Stack arbitrarily deep — `ShadowAssetComponent { wraps: BudgetAssetComponent { wraps: SnowflakeQueryComponent }, shadow_wraps: BigqueryQueryComponent }` = `@shadow @budget @snowflake vs @bigquery` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write two Python callables and reference them via `compute:` / `shadow_compute:` to use `@shadow`. That's fine but requires user Python.

With `wraps:`, decorator components stack over **any DCC component** with zero user Python. This is the direct fit for vendor swaps:

- `ShadowAssetComponent { wraps: SnowflakeQueryComponent, shadow_wraps: BigqueryQueryComponent }` — validate a warehouse migration end-to-end
- `ShadowAssetComponent { wraps: OpenAiAgent, shadow_wraps: AnthropicAgent }` — compare LLM providers with the same prompt
- `ShadowAssetComponent { wraps: RestApiFetcherComponent (v2 API), shadow_wraps: RestApiFetcherComponent (v1 API) }` — validate an API version upgrade
- `ShadowAssetComponent { wraps: DataframeFromSqlComponent (new query), shadow_wraps: DataframeFromSqlComponent (legacy query) }` — validate a query rewrite
- `ShadowAssetComponent { wraps: NewIngestionComponent, shadow_wraps: OldIngestionComponent, enforce_match: true }` — release gate that blocks on drift

## Diff strategy

Ordered fallback (from `_diff()` in `shadow_asset/component.py`):

| Type | Comparison |
|---|---|
| Both `None` | match |
| `pandas.DataFrame` | shape + column set + first-500-row equality |
| `list` / `tuple` | element equality |
| `dict` | key-value equality |
| Anything else | `primary == shadow` |

Diff details land as typed `AssetObservation` metadata (`shadow_match`, `shadow_diff_mode`, `shadow_diff_rows`, `shadow_extra_cols`, `shadow_missing_cols`) so sensors and BI can query drift over time.

## Components used

| Component | What it does |
|---|---|
| `shadow_asset` (`@shadow` decorator + `ShadowAssetComponent`) | Dual-run primary + shadow, diff outputs, emit `shadow_match` observation. Shadow exceptions are trapped — primary always wins unless `enforce_match=true`. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. Two instances with different `random_state` values simulate a vendor mismatch. |

## Why this belongs in Dagster

- **Every run is an event log entry** — `shadow_match`, `shadow_diff_rows`, `shadow_extra_cols`, `shadow_missing_cols`. A sensor can escalate after N consecutive mismatches.
- **Same primitive, different surface** — Python + YAML users get identical behavior. Same observation events. Same sensor targets.
- **Composability at the component layer** — you can wrap ANY DCC component with shadow instrumentation without editing that component's config.
- **Fits the migration playbook** — ship shadow → verify convergence via the event log → flip primary → drop shadow. Every stage tracked.

## Migration playbook

```
1. Deploy:  ShadowAssetComponent { wraps: OldVendor, shadow_wraps: NewVendor }
            → observe-only. Old vendor is still what production sees.

2. Wait:    N days / M runs, watching shadow_match observations for convergence.

3. Enforce: ShadowAssetComponent { wraps: OldVendor, shadow_wraps: NewVendor,
                                    enforce_match: true }
            → any mismatch fails the run. Release gate.

4. Flip:    ShadowAssetComponent { wraps: NewVendor, shadow_wraps: OldVendor }
            → new vendor is production; old vendor is the shadow.

5. Drop:    Just:  NewVendorComponent { ... }
            → shadow retired. Migration complete.
```

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_shadow_asset_demo.sh | bash
cd shadow-asset-demo
uv run dg dev
```

## Pair with a sensor — alert on repeat mismatches

```python
@dg.sensor(name="shadow_drift_alert")
def shadow_drift_alert(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    mismatches = [
        r for r in recs
        if (r.asset_observation.tags or {}).get("shadow_match") == "false"
    ]
    if len(mismatches) >= 5:
        # 5+ mismatches in last 100 obs — page migration owner
        ...
```

## After the demo — inspect in the UI

```bash
cd shadow-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `shadow_match` tag + `shadow_diff_mode` / `shadow_diff_rows` / `shadow_extra_cols` / `shadow_missing_cols` metadata for every materialization.

## See also

- [`shadow_asset` component reference](https://dagster-component-ui.vercel.app/c/shadow_asset)
- [`cached_asset` walkthrough](cached_asset.md) — same "wraps" pattern, caching primitive
- [`throttle_asset` walkthrough](throttle_asset.md) — same "wraps" pattern, rate-limiting primitive
- [`snapshot_asset` walkthrough](snapshot_asset.md) — pair with shadow for retro-diff of migration candidates
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
