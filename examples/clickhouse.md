# ClickHouse — DataFrame → ClickHouse end-to-end (live, Docker)

Single-container Docker walkthrough. Spins up `clickhouse/clickhouse-server` locally, scaffolds a Dagster project that generates synthetic order data, bulk-loads it into ClickHouse via the official `clickhouse-connect` client, and declares the destination table as an external asset for catalog lineage.

**Live-validated** — `setup_clickhouse_demo.sh` + `dg launch --assets '*'` materializes 10,000 rows into `analytics.orders` against a real ClickHouse container. Verified at component release time; promotion to `live` on each component's manifest entry.

## Components exercised

| Component | Role |
|---|---|
| [`synthetic_data_generator`](https://dagster-component-ui.vercel.app/c/synthetic_data_generator) | Generates 10,000 synthetic orders (`schema_type: orders`) → Pandas DataFrame |
| [`clickhouse_resource`](https://dagster-component-ui.vercel.app/c/clickhouse_resource) | ClickHouse connection (clickhouse-connect HTTP client + SQLAlchemy URL) |
| [`dataframe_to_clickhouse`](https://dagster-component-ui.vercel.app/c/dataframe_to_clickhouse) | Bulk-insert via `client.insert_df()` — ~1M rows/sec per client |
| [`external_clickhouse_table`](https://dagster-component-ui.vercel.app/c/external_clickhouse_table) | Declare-only catalog entry for `analytics.orders` |

## Asset graph

```
orders_clean (synthetic_data_generator)
      │
      ▼
clickhouse_orders_load (dataframe_to_clickhouse)
      │
      ▼
clickhouse/analytics/orders (external_clickhouse_table — declare-only)
```

## Run it

```bash
# 1. Scaffold + bring up ClickHouse + install components
bash setup_clickhouse_demo.sh clickhouse-demo

# 2. Set env vars
cd clickhouse-demo
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=

# 3. Materialize all assets
uv run dg launch --assets '*'

# OR launch the UI to browse the graph + click-to-materialize
uv run dg dev
# → http://localhost:3000
```

## Verify

```bash
# Should print 10000 rows + date range
curl 'http://localhost:18123/?query=SELECT+count(*),min(order_date),max(order_date)+FROM+analytics.orders'
```

Expected output: `10000  2026-04-26 22:30:16  2026-05-27 22:14:00` (date range depends on when you run — synthetic_data_generator uses recent dates).

## What the setup script does

1. **Starts ClickHouse Server in Docker** (`clickhouse/clickhouse-server:latest`) on host ports 18123 (HTTP) + 19000 (native).
2. **Pre-creates the destination table** `analytics.orders` via HTTP POST. Schema matches `synthetic_data_generator`'s `orders` shape exactly (11 columns: `order_id`, `customer_id`, `order_date`, `category`, `num_items`, `subtotal`, `shipping`, `tax`, `total`, `status`, `region`).
3. **Scaffolds the Dagster project** with `uvx create-dagster project`.
4. **Installs the 4 components** via the community CLI (`--refresh` on first call to bust the manifest cache).
5. **Overwrites the CLI-installed example defs.yamls** with demo-specific configuration — points `dataframe_to_clickhouse` at `orders_clean` from the upstream synthetic generator, configures host/port/auth env vars.

## Cleanup

```bash
docker rm -f clickhouse-demo-server
rm -rf /tmp/clickhouse-demo
```

## Common issues

- **`Code: 164 ... Cannot execute query in readonly mode`** — DDL over GET fails. The setup script uses POST for `CREATE DATABASE` / `CREATE TABLE`. If you adapt this to a different demo, use `curl -X POST --data '<sql>'`.
- **`Unrecognized column 'X' in table orders`** — your CREATE TABLE doesn't match the DataFrame's columns. clickhouse-connect's `insert_df()` requires column-name parity. Pre-create the table with every column the upstream emits, or add a `column_names:` field to the `dataframe_to_clickhouse` config to write a subset.
- **`Component not found: clickhouse_resource`** — your CLI cache is stale. The script uses `--refresh` on the first add to force a fresh manifest fetch. If running individual commands manually, prepend `--refresh` to any first call after a registry update.

## See also

- [`vendors/clickhouse.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/clickhouse.md) — full ClickHouse vendor page
- [ClickHouse docs](https://clickhouse.com/docs/)
- [clickhouse-connect Python client](https://clickhouse.com/docs/en/integrations/python)
