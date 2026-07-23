# ClickHouse (advanced) — IO manager + observation sensor

Docker-local end-to-end for the ClickHouse **code-level** components — the ones the base [`clickhouse.md`](clickhouse.md) demo doesn't exercise. Same `clickhouse/clickhouse-server` image, extra components wired.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_clickhouse_advanced_demo.sh \
  -o setup_clickhouse_advanced_demo.sh
bash setup_clickhouse_advanced_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + Docker. Cost: $0.

## What gets validated

| Component | Role | Prior status |
|---|---|---|
| `clickhouse_resource` | Shared connection (host / port / auth) | `live` (base demo) |
| `clickhouse_io_manager` | Every `pd.DataFrame` asset auto-persists as `analytics.<asset_name>` table via `dagster-clickhouse-pandas` | `code` → now `live` |
| `external_clickhouse_table` | Declare a ClickHouse table as a first-class Dagster asset | `live` (base demo) |
| `clickhouse_table_observation_sensor` | Periodic health probe: `row_count` / `size_bytes` / `active_parts` / `engine` / `last_modified` | `code` → now `live` |

## The chain

```
clickhouse/clickhouse-server:latest  (container: clickhouse-demo-server)
   └─ analytics.orders  ← 100 seed rows (95 active + 5 cancelled)

┌────────────────────────────────────┐
│ clickhouse/analytics/orders        │
│ (external_clickhouse_table —       │
│  declare-only, ClickHouse owns     │
│  the lifecycle)                    │
└────────────────┬───────────────────┘
                 │
                 ▼
┌────────────────────────────────────┐
│ clickhouse_orders_observation      │
│ (sensor, 60s cadence)              │
│ Emits AssetObservation with:       │
│   row_count, size_bytes,           │
│   active_parts, engine,            │
│   last_modified                    │
└────────────────────────────────────┘

Project-wide `io_manager` = ClickhousePandasIOManager
   → any DataFrame @asset you add will materialize as an
     analytics.<asset_name> table (no `dataframe_to_clickhouse`
     wiring needed — the IO manager handles it)
```

## Verifying end-to-end

```bash
# Sensor's output on the seeded 100 rows — this is what shows up in the UI's
# AssetObservation events after you turn the sensor ON.
uv run python -c "
from pathlib import Path
from dagster import load_from_defs_folder, build_sensor_context
defs = load_from_defs_folder(path_within_project=Path('src/clickhouse_advanced_demo').resolve())
result = defs.get_sensor_def('clickhouse_orders_observation')(build_sensor_context())
for ev in result.asset_events:
    print(ev.asset_key, dict(ev.metadata))
"
# → row_count=100, size_bytes=~4300, active_parts=1, engine=MergeTree
```

## Docker-image note

`clickhouse/clickhouse-server:latest` binds host `:18123` (HTTP) + `:19000` (native). If port 8123 is free on your host you can override with `CH_PORT_HTTP=8123`. First-boot is 60-90 seconds; setup script polls the `/ping` endpoint up to 90s before proceeding.

The container name `clickhouse-demo-server` is shared with the base [`clickhouse.md`](clickhouse.md) demo — run either one (it reuses the container). Delete + recreate for a clean state:

```bash
docker rm -f clickhouse-demo-server
```

## Cleanup

```bash
docker rm -f clickhouse-demo-server
```

## Why a separate demo (vs. extending `clickhouse.md`)

The base demo validates the reader/writer/resource round-trip pattern (DataFrame in, DataFrame out). The IO manager pattern is fundamentally different — it's a project-wide slot, and mixing "explicit sink" and "auto-persist via IO manager" in the same demo would obscure both. Sibling demos let each shape stand on its own.

## Related

- [`clickhouse.md`](clickhouse.md) — the base round-trip demo (resource + writer + external table)
- [`doris_starrocks.md`](doris_starrocks.md) — same OSS-MPP shape for Doris + StarRocks
