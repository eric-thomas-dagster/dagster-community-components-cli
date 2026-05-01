# Partitioned Earthquakes — daily backfillable pipeline

The earthquakes pipeline, made backfillable: each daily partition queries the
USGS historical API for that one day. Backfill any range, materialize
individual days, view per-partition output in the Dagster UI.

## Pipeline

```
rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json
       (all 5 components partitioned daily — DailyPartitionsDefinition)
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | Daily-partitioned GET to the USGS historical query API; uses `{partition_date}` / `{partition_date_next}` URL templating |
| 2 | `json_flatten` | transformation | Flatten partition-by-partition |
| 3 | `select_columns` | transformation | Same as the unpartitioned demo |
| 4 | `sort` | transformation | By magnitude per partition |
| 5 | `dataframe_to_json` | sink | Per-partition file: `/tmp/earthquakes/{partition_date}.jsonl` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_partitioned_earthquakes_demo.sh | bash
cd partitioned-earthquakes-demo
uv run dg launch --assets '*' --partition 2026-04-15
```

To backfill a range:

```bash
uv run dg launch --assets '*' --partition-range 2026-04-10...2026-04-15
```

Or open the Dagster UI (`uv run dg dev`) and pick partitions there.

## Output

One JSONL file per materialized day:

```
/tmp/earthquakes/2026-04-15.jsonl
/tmp/earthquakes/2026-04-16.jsonl
...
```

## What this demo shows

- Format-string templating in URL fields (`{partition_date}`,
  `{partition_date_next}`) — the fetcher substitutes the active partition key
  at run-time without any hand-rolled date math.
- Format-string templating in sink `file_path` — every partition writes to
  its own file.
- Same component definitions as the unpartitioned demo; partitioning is opt-in
  via three additional fields (`partition_type`, `partition_start`,
  `partition_date_column`).
