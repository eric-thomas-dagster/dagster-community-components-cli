# USGS Earthquakes — REST + JSON pipeline

A 5-component pipeline that pulls the USGS public earthquake feed, flattens
nested GeoJSON, selects + sorts fields, writes JSONL.

## Pipeline

```
rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET the USGS daily feed (no auth) |
| 2 | `json_flatten` | transformation | Flatten the `properties` dict from each feature |
| 3 | `select_columns` | transformation | Keep `mag`, `place`, `time`, `url`, `tsunami` |
| 4 | `sort` | transformation | Order by magnitude (descending) |
| 5 | `dataframe_to_json` | sink | Write `/tmp/earthquakes.jsonl` (one record per line) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_earthquakes_demo.sh | bash
cd earthquakes-demo
uv run dagster asset materialize --select '*' -m definitions
```

## Output

```jsonl
{"mag":4.7,"place":"southeast of the Loyalty Islands","time":...,"tsunami":0,"url":"..."}
{"mag":4.5,"place":"central Mid-Atlantic Ridge","time":...,"tsunami":0,"url":"..."}
```

## What this demo shows

- REST ingestion with `json_path` extraction (the USGS response wraps the
  earthquake list under `features`).
- `json_flatten` turns nested dict columns into flat scalar columns.
- JSONL output preserves record-per-line structure for downstream tools.
