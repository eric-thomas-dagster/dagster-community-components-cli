# Dremio → Dagster pipeline blueprint
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Query a Dremio cluster (OSS or Cloud) from Dagster and materialize the result as an asset. Demonstrated end-to-end against a **local Dremio in Docker** — no Dremio Cloud account needed.

Dremio is SAP's lakehouse query engine (acquired 2024-25). It federates SQL across Iceberg / Delta / Snowflake / Postgres / S3 / ADLS / etc. — anywhere SQL can land. Perfect upstream for Dagster: write the SQL once, swap the underlying source without touching the pipeline.

## Architecture

```
   ┌────────────────────────────────────────────┐
   │ Dremio OSS in Docker                       │
   │   coordinator: http://localhost:9047       │
   │   Sample dataset: Samples / SF_incidents   │
   │   PAT auth                                 │
   └─────────────────┬──────────────────────────┘
                     │ SQL via REST (POST /api/v3/sql)
                     ▼
   ┌────────────────────────────────────────────┐
   │ dremio_ingestion (asset)                   │
   │   submit job → poll → page results         │
   │   → pandas DataFrame                       │
   └─────────────────┬──────────────────────────┘
                     │
                     ▼
   ┌────────────────────────────────────────────┐
   │ summarize (asset)                          │
   │   group_by category, count, etc.           │
   └─────────────────┬──────────────────────────┘
                     │
                     ▼
   ┌────────────────────────────────────────────┐
   │ dataframe_to_parquet (sink)                │
   │   ./output/incidents_by_category.parquet   │
   └────────────────────────────────────────────┘
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dremio_pipeline_demo.sh | bash
```

The script:
1. Starts Dremio OSS in Docker (waits for it to be ready)
2. Walks you through creating an admin user + PAT in the UI (1 minute)
3. Scaffolds a Dagster project with `dremio_ingestion` → `summarize` → `dataframe_to_parquet`
4. Validates the pipeline end-to-end

> **Why interactive setup?** Dremio's REST `/apiv2/bootstrap/firstuser` is locked behind UI-only consent in recent versions; PATs can only be minted from the UI. The script handles everything else automatically.

## Components used

| Component | Source | Role |
|---|---|---|
| `dremio_ingestion` | community | Submit SQL → poll job → fetch results → pandas DataFrame |
| `summarize` | community | Per-group aggregations |
| `dataframe_to_parquet` | community | Write curated parquet |

## defs.yaml — Dremio Cloud (production)

```yaml
type: dagster_community_components.DremioIngestionComponent
attributes:
  asset_name: customer_revenue
  host: https://my-org.dremio.cloud
  auth_type: pat
  auth_token_env_var: DREMIO_PAT
  query: |
    SELECT customer_id, SUM(revenue) AS total
    FROM "@my-space"."customers"
    WHERE business_date = DATE '{partition_key}'
    GROUP BY customer_id
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: dremio
  kinds: [dremio, sql]
```

## defs.yaml — Dremio OSS (Docker / on-prem)

```yaml
type: dagster_community_components.DremioIngestionComponent
attributes:
  asset_name: incidents_by_category
  host: http://localhost:9047
  auth_type: pat
  auth_token_env_var: DREMIO_PAT
  query: |
    SELECT category, COUNT(*) AS n_incidents
    FROM "Samples"."samples.dremio.com"."SF_incidents2016.json"
    GROUP BY category
    ORDER BY n_incidents DESC
  group_name: dremio
  kinds: [dremio, sql]
```

## REST vs Arrow Flight

| | REST | Apache Arrow Flight SQL |
|---|---|---|
| **Performance** | Slower (JSON over HTTPS, paginated 500 rows at a time) | Columnar Arrow over gRPC — 5–50× faster |
| **Deps** | `requests` (already installed) | `pyarrow>=10` (~50MB) |
| **Port** | 9047 (same as web UI) | 32010 (separate) |
| **When to use** | Default — fine up to ~100k rows | Bigger result sets, perf-critical paths |

To switch to Flight, set `transport: flight` in the YAML. The Dagster project gains a `pyarrow` dependency but the SQL stays identical.

## PAT vs legacy password auth

Dremio 24.0+ supports Personal Access Tokens — the recommended path:

1. Web UI → username → **Account Settings** → **Personal Access Tokens** → **Create**
2. Set lifetime + scope → copy the token
3. `export DREMIO_PAT=<token>`
4. `auth_type: pat` + `auth_token_env_var: DREMIO_PAT`

Older OSS / hardened-policy environments without PAT support fall back to password:

```yaml
auth_type: password
auth_username_env_var: DREMIO_USER
auth_password_env_var: DREMIO_PASSWORD
```

The component calls `/apiv2/login` to mint a session token and uses `Authorization: _dremio<token>` per Dremio's REST convention.

## Source name quoting

Dremio paths use `@-` (personal space marker) and `.` characters that aren't valid SQL identifiers. Always quote:

```sql
SELECT * FROM "@my-space"."customers"
SELECT * FROM "Samples"."samples.dremio.com"."SF_incidents2016.json"
SELECT * FROM "DataLake"."events"."2026"."05"."13"
```

## Federating sources

Dremio's strength is querying across multiple sources in one SQL statement. Register an Iceberg source, a Snowflake source, and a Postgres source in Dremio's UI, then:

```sql
SELECT
  o.order_id,
  o.customer_id,
  c.name AS customer_name,
  i.product_name
FROM "Postgres"."public"."orders" o
JOIN "Snowflake"."CRM"."CUSTOMERS" c USING (customer_id)
JOIN "S3 Iceberg"."catalog"."products" i USING (product_id)
WHERE o.order_date = DATE '{partition_key}'
```

Dremio compiles + pushes-down per source; Dagster sees one asset.

## Partitioning

```yaml
query: |
  SELECT * FROM "DataLake"."orders"
  WHERE order_date = DATE '{partition_key}'
partition_type: daily
partition_start: '2024-01-01'
```

`{partition_key}` is substituted per run. Re-running a partition pulls the same window again — Dremio's caching handles the re-read efficiently.

## Trade-offs & gotchas

- **REST is slow for big results.** Pagination at 500 rows/page caps throughput. Use Flight for anything bigger than ~100k rows.
- **Job submission is async.** The component polls every `poll_interval_seconds` (default 1.0). Long-running queries hit `poll_timeout_seconds` (default 600 = 10 min) — tune up for big aggregations.
- **PAT expiration.** Tokens have lifetimes. Build expiry alerts so Dagster doesn't silently fail on token expiry day.

## See also

- [`odata_pipeline.md`](odata_pipeline.md) — OData v2/v4 — covers most SAP cloud + Dynamics 365
- [`sap_hana_pipeline.md`](sap_hana_pipeline.md) — SAP HANA direct SQL via `sap_hana_ingestion`
- [Dremio REST API docs](https://docs.dremio.com/cloud/api/) (cloud) / [Dremio OSS docs](https://docs.dremio.com/software/) (OSS)
