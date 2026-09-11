# snapshot_asset — `@snapshot` decorator + `SnapshotAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no cloud, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in under a minute (plus `uv run dg launch`
startup overhead).

## What this demo shows

Two assets, one per shape. Both write point-in-time snapshots keyed by
`code_version + timestamp + run_id` and emit the same `snapshot_asset=written`
observations with `snapshot_path` / `snapshot_bytes` / `snapshot_format`
metadata — rollback becomes an event log query.

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_daily_report` (Python decorator) | writes 1 parquet snapshot to `.snap-py/py_daily_report/v1/` |
| 2 | `py_daily_report` (Python decorator) | writes 2nd parquet snapshot next to first |
| 3 | `yaml_daily_report` (YAML composability) | inner data-gen runs; outer writes 1 parquet snapshot to `.snap-yaml/yaml_daily_report/v1/` |
| 4 | `yaml_daily_report` (YAML composability) | inner data-gen runs; outer writes 2nd snapshot |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to snapshot:

```python
# src/<pkg>/defs/py_daily_report.py
import pandas as pd
import dagster as dg
from dagster_community_components import snapshot

SNAP_URI = "/tmp/.snap-py"

@dg.asset(code_version="v1", group_name="python_decorator")
@snapshot(uri=SNAP_URI, format="parquet")
def py_daily_report(context) -> pd.DataFrame:
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
```

**Shape 2: YAML composability — the money shot.** `SnapshotAssetComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@decorator @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_daily_report/defs.yaml
type: dagster_community_components.SnapshotAssetComponent
attributes:
  uri: /tmp/.snap-yaml
  format: parquet
  code_version: v1
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_daily_report
      schema_type: customers
      row_count: 100
      random_state: 42
```

**One asset is registered** (`yaml_daily_report`) — no duplication. The outer `SnapshotAssetComponent` intercepts the inner's compute output and serializes it to the snapshot URI. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `CachedAssetComponent { wraps: SnapshotAssetComponent { wraps: SnowflakeQueryComponent } }` = `@cached @snapshot @snowflake` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, decorator components stack over **any DCC component** with zero user Python:

- `SnapshotAssetComponent { wraps: SnowflakeQueryComponent }` — snapshot warehouse query results for rollback
- `SnapshotAssetComponent { wraps: LLMPromptExecutorComponent }` — snapshot LLM outputs for audit
- `SnapshotAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape
- `SnapshotAssetComponent { wraps: RestApiFetcherComponent }` — snapshot external API responses for replay
- `SnapshotAssetComponent { wraps: DataframeFromCsv }` — snapshot canonical inputs after ingestion

## Path shape

```
<uri>/<asset_name>/<code_version>/<timestamp>__<run_id_short>.<ext>
```

- `code_version` — from the asset's `code_version=` (falls back to `unknown`).
- `timestamp` — UTC `YYYYMMDDTHHMMSSZ`.
- `run_id_short` — first 12 chars of `context.run_id`.
- Extension auto-detected from the returned value type: `pandas.DataFrame` → `.parquet`, `dict`/`list` → `.json`, `bytes` → `.bin`, `str` → `.txt`, else `.pkl`. Explicit `format` override supported.

## Rollback pattern

Every snapshot leaves an `AssetObservation` in the event log. Rollback becomes a query:

```python
@dg.asset(deps=[daily_report])
def restore_report(context):
    from dagster import DagsterEventType, EventRecordsFilter
    import pandas as pd
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=50, ascending=False,
    )
    snapshots = [
        r for r in recs
        if r.asset_observation
        and r.asset_observation.tags.get("snapshot_asset") == "written"
    ]
    latest = snapshots[0].asset_observation.metadata["snapshot_path"]
    return pd.read_parquet(latest.value)
```

## Components used

| Component | What it does |
|---|---|
| `snapshot_asset` (`@snapshot` decorator + `SnapshotAssetComponent`) | Write point-in-time snapshots to any fsspec URI (local / `s3://` / `gs://` / `abfs://`) keyed by code_version + timestamp + run_id. Auto-detects format; optional `retention_days` prunes older snapshots. Emits `AssetObservation` per write. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. |

## Why this belongs in Dagster

- **`code_version`-aware paths** — rolling back "the last snapshot before we deployed v3" is a filesystem `find` (or event log query).
- **AssetObservation events** — rollback UIs and audit tools query the event log for `snapshot_asset=written` observations. No side database.
- **fsspec URIs** — same code writes local / S3 / GCS / Azure.
- **Complementary to `@cached`** — `@cached` skips compute; `@snapshot` always runs but saves a checkpoint. Combine for retro-cache access.
- **Composability at the component layer** — you can wrap ANY DCC component with snapshotting without editing that component's config.

## Retention

Set `retention_days: 30` and after every successful write, snapshots older than 30 days in the asset's folder are pruned. `snapshot_pruned_count` is emitted in the observation for auditability.

## Cost

**$0.** Fully offline. Snapshots land inside the project dir (`.snap-py/`, `.snap-yaml/`) so cleanup is `rm -rf <project>`.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snapshot_asset_demo.sh | bash
cd snapshot-asset-demo
uv run dg dev
```

## Pair with a sensor — auto-restore on failure

```python
@dg.sensor(name="restore_from_last_snapshot")
def restore_from_last_snapshot(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=50, ascending=False,
    )
    snapshots = [
        r for r in recs
        if (r.asset_observation.tags or {}).get("snapshot_asset") == "written"
    ]
    if not snapshots:
        return
    latest_path = snapshots[0].asset_observation.metadata["snapshot_path"].value
    # ... trigger restore job with latest_path as run config ...
```

## After the demo — inspect in the UI

```bash
cd snapshot-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `snapshot_asset=written` tag + `snapshot_path` / `snapshot_bytes` / `snapshot_format` metadata for every write.

## See also

- [`snapshot_asset` component reference](https://dagster-component-ui.vercel.app/c/snapshot_asset)
- [`cached_asset` walkthrough](cached_asset.md) — same "wraps" pattern, caching primitive (skips compute on hit)
- [`shadow_asset` walkthrough](shadow_asset.md) — dual-run + diff; snapshot both sides for retro comparison
- [`throttle_asset` walkthrough](throttle_asset.md) — same "wraps" pattern, rate-limiting primitive
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
