# Auto-profile every materialization — histograms, quantiles, correlations
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~2 minutes.

## What this demo shows

Two assets, one per shape. Both emit the same `AssetObservation` shape:
`profile` (full nested JSON: per-column dtype / null_ratio / distinct_count
/ min / max / mean / std / top_value_ratio) plus flat `profile_row_count`
and `profile_column_count` tags for cheap sensor filtering.

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_orders` (Python decorator) | Compute returns a 500-row DataFrame; profile emitted |
| 2 | `py_orders` (Python decorator) | Same DataFrame again; second snapshot lands in event log |
| 3 | `yaml_customers` (YAML composability) | Inner `SyntheticDataGeneratorComponent` runs (500 customer rows, 10 cols); outer profile intercepts the returned DataFrame + emits observation |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to auto-profile:

```python
# src/<pkg>/defs/py_orders.py
import pandas as pd
import dagster as dg
from dagster_community_components import profile

@dg.asset(group_name="python_decorator")
@profile(categorical_max_distinct=10, top_n_columns=5)
def py_orders(context):        # ← NO `-> pd.DataFrame` annotation:
    return pd.DataFrame(...)   #   @profile yields dg.Output(df, metadata=)
                               #   under the hood; the @dg.asset fn
                               #   is effectively a generator.
```

**Shape 2: YAML composability — the money shot.** `ProfileAssetComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@profile @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_customers/defs.yaml
type: dagster_community_components.ProfileAssetComponent
attributes:
  categorical_max_distinct: 10
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 500
      random_state: 42
```

**One asset is registered** (`yaml_customers`) — no duplication. The outer `ProfileAssetComponent` extracts the DataFrame the inner returns, computes null_ratio / distinct_count / numeric stats per column, and emits it as one `AssetObservation`. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `CachedAssetComponent { wraps: ProfileAssetComponent { wraps: SnowflakeQueryComponent { ... } } }` = `@cached @profile @snowflake-compute` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `ProfileAssetComponent` stacks over **any DCC component whose compute returns a `pandas.DataFrame`** with zero user Python:

- `ProfileAssetComponent { wraps: SnowflakeQueryComponent }` — profile every query result
- `ProfileAssetComponent { wraps: BigqueryQueryComponent }` — same for BigQuery
- `ProfileAssetComponent { wraps: DataframeFromCsvComponent }` — profile ingested CSVs
- `ProfileAssetComponent { wraps: RestApiFetcherComponent }` — profile API responses
- `ProfileAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Components used

| Component | What it does |
|---|---|
| `profile_asset` (`@profile` decorator + `ProfileAssetComponent`) | Auto-emit per-column data profile as `AssetObservation` on every materialization. Per column: `dtype`, `null_count`, `null_ratio`, `distinct_count`. For numerics: `min`, `max`, `mean`, `std`. For categoricals (< `categorical_max_distinct`): `top_value_ratio`. Optional `custom_probes` for user-defined `mod:fn` extensions. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. |

## Why this belongs in Dagster

- **Drift detection is free** — every materialization is a JSON snapshot. Query `fetch_observations()`, diff adjacent snapshots, alert when a null_ratio / distinct_count / row_count delta trips a threshold. No external metrics store.
- **Same primitive, different surface** — Python + YAML users get identical behavior + identical observation shape. Same sensor targets.
- **Composability at the component layer** — wrap ANY DCC component whose compute returns a DataFrame with the profile behavior without editing that component's config.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_profile_asset_demo.sh | bash
cd profile-asset-demo
uv run dg dev
```

## Pair with a sensor — drift detection

```python
@dg.sensor(name="profile_drift_watcher")
def profile_drift_watcher(context):
    import json
    from dagster import DagsterInstance
    inst = context.instance
    for asset_name in ("yaml_customers",):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=2,
        ).records))
        if len(recs) < 2:
            continue
        prev = json.loads(dict(recs[0].asset_observation.metadata)["profile"].value)
        curr = json.loads(dict(recs[1].asset_observation.metadata)["profile"].value)
        for col, curr_stats in curr.get("columns", {}).items():
            prev_stats = prev.get("columns", {}).get(col, {})
            null_delta = curr_stats.get("null_ratio", 0) - prev_stats.get("null_ratio", 0)
            if null_delta > 0.05:
                # 5+ pt jump in null_ratio for one column → page data-eng
                ...
```

## After the demo — inspect in the UI

```bash
cd profile-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows every profile snapshot as one row. Expand the `profile` metadata field for the full per-column JSON.

## See also

- [`profile_asset` component reference](https://dagster-component-ui.vercel.app/c/profile_asset)
- [`cached_asset` walkthrough](cached_asset.md) — same `wraps:` pattern, different primitive (skip compute on cache hit)
- [`throttle_asset` walkthrough](throttle_asset.md) — cross-run rate limiting
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
