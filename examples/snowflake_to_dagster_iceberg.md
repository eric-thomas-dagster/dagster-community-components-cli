# Snowflake → Dagster (cross-engine Iceberg)

A focused walkthrough for one common cross-vendor scenario: **Snowflake writes Iceberg tables to S3 using its managed catalog; Dagster reads them downstream for analytics and ML**.

This is a more specific version of the [`iceberg_pipeline.md`](iceberg_pipeline.md) walkthrough — same components, focused on the Snowflake-as-writer flow.

> **Related blueprint:** [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) covers the larger Snowflake → Iceberg → Databricks pattern. This walkthrough is the simpler Snowflake → Dagster slice.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ Snowflake (write side)                              │
   │   CREATE ICEBERG TABLE my_db.public.orders ...      │
   │   CATALOG='SNOWFLAKE'                               │
   │   EXTERNAL_VOLUME='s3_vol'                          │
   │   BASE_LOCATION='orders/'                           │
   └─────────────────────────────┬───────────────────────┘
                                 │ Snowflake commits snapshots
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Snowflake-managed Iceberg REST catalog              │
   │   /api/v2/catalogs/MY_CAT/iceberg                   │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ iceberg_ingestion (Dagster asset)                   │
   │   reads via PyIceberg REST client                   │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Downstream Dagster: transform / ML / write back     │
   └─────────────────────────────────────────────────────┘
```

## Why Snowflake → Iceberg → Dagster

Snowflake's managed Iceberg catalog has been the breakout cross-vendor feature of 2024-25:

- **Snowflake stays the source of truth for writes** — your data team's SQL workflows are unchanged
- **Iceberg files in your S3** — egress / lock-in concerns drop
- **Any Iceberg reader can join the lake** — Databricks, Trino, Spark, Dremio, Dagster
- **No Snowflake compute needed for downstream reads** — Dagster pulls directly from S3 via PyIceberg

Dagster's role: orchestrate the downstream consumers (ML training sets, BI extracts, freshness checks) without paying Snowflake compute costs for every read.

## Step 1 (in Snowflake) — write an Iceberg table

```sql
-- One-time: create the external volume + catalog integration
CREATE OR REPLACE EXTERNAL VOLUME s3_vol
  STORAGE_LOCATIONS = ((
    NAME = 'my-bucket'
    STORAGE_PROVIDER = 'S3'
    STORAGE_BASE_URL = 's3://my-bucket/snowflake-iceberg/'
    STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/snowflake-iceberg-role'
  ));

CREATE OR REPLACE CATALOG INTEGRATION my_cat
  CATALOG_SOURCE = 'OBJECT_STORE'
  TABLE_FORMAT = 'ICEBERG'
  ENABLED = TRUE;

-- Then: create the Iceberg table (Snowflake-managed)
CREATE ICEBERG TABLE my_db.public.orders (
  order_id STRING,
  customer_id STRING,
  amount NUMBER(18, 2),
  order_date DATE,
  status STRING
)
CATALOG = 'SNOWFLAKE'         -- Snowflake manages snapshots/metadata
EXTERNAL_VOLUME = 's3_vol'
BASE_LOCATION = 'orders/';

-- Insert (Snowflake commits an Iceberg snapshot)
INSERT INTO my_db.public.orders VALUES ('O001', 'C001', 99.50, '2024-05-10', 'shipped');
```

## Step 2 (one-time) — mint a Snowflake PAT for the catalog

The Snowflake-managed catalog uses a Snowflake **Personal Access Token (PAT)** for OAuth.

1. In Snowflake: `Account → Users → <your-user> → Personal Access Tokens → Create`
2. Set a name + role binding (use a service role with read on `my_db.public.orders`)
3. Copy the token
4. `export SNOWFLAKE_PAT=<token>`

The role binding becomes `PRINCIPAL_ROLE:<role-name>` in the `scope` field.

## Step 3 — read from Dagster

```yaml
# defs/orders_iceberg/defs.yaml
type: dagster_community_components.IcebergIngestionComponent
attributes:
  asset_name: orders_iceberg
  catalog_type: rest
  catalog_properties:
    uri: https://<account>-<orgname>.snowflakecomputing.com/api/v2/catalogs/MY_CAT/iceberg
    credential: "${SNOWFLAKE_PAT}"
    warehouse: MY_DB.PUBLIC          # the Snowflake DB.SCHEMA scope
    scope: PRINCIPAL_ROLE:DAGSTER_RO
  namespace: PUBLIC
  table_name: orders
  select_columns: [order_id, customer_id, amount, order_date, status]
  row_filter: order_date >= '{partition_key}'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: lakehouse
  kinds: [iceberg, snowflake, lakehouse]
```

## Step 4 — observability (lineage)

Declare the Snowflake-owned table explicitly so Dagster's asset graph shows who owns it:

```yaml
# defs/orders_external/defs.yaml
type: dagster_community_components.ExternalIcebergTableAsset
attributes:
  asset_key: snowflake/orders
  catalog_name: my_cat
  namespace: PUBLIC
  table_name: orders
  warehouse: s3://my-bucket/snowflake-iceberg/orders/
  owner_engine: snowflake   # tags asset with kinds: [iceberg, lakehouse, snowflake]
  description: "Orders fact table — written by Snowflake's managed Iceberg catalog"
  group_name: lakehouse
```

Then on `orders_iceberg`, add `deps: [snowflake/orders]` to wire the lineage edge.

## AWS credentials — IAM strategy

PyIceberg follows fsspec conventions. For S3 access to the Snowflake-written files:

| Runtime | Credentials |
|---|---|
| Laptop / dev | `aws sso login` or `aws configure` → env vars or `~/.aws/credentials` |
| EC2 / ECS | Instance profile / Task IAM role (zero-config) |
| EKS (recommended) | IRSA — `serviceAccount` annotated with `eks.amazonaws.com/role-arn` |
| Dagster+ Hybrid | Inject AWS env vars via deployment config |

The role needs `s3:GetObject` + `s3:ListBucket` on the bucket prefix where Snowflake writes Iceberg files.

## Trade-offs vs alternatives

| | Snowflake → Iceberg → Dagster (this) | Snowflake-direct (dagster-snowflake) |
|---|---|---|
| Snowflake compute cost per read | 0 | charged per query |
| Latency | depends on S3 + Iceberg manifest parsing | depends on Snowflake warehouse |
| Concurrency | unlimited (S3 reads) | bounded by warehouse |
| Format constraints | only Iceberg-table data | any Snowflake object |
| Best for | High-volume reads, ML training sets, cross-tool federation | Real-time BI, ad-hoc joins |

For ML / scheduled batch / cross-tool patterns, the Iceberg path is dramatically cheaper. For interactive analytics, Snowflake direct is still right.

## Trade-offs & gotchas

- **PAT lifetime.** Snowflake PATs have lifetimes (default 90 days). Build a calendar reminder before expiry.
- **External volume must be Snowflake-writable AND Dagster-readable.** Easiest pattern: Snowflake writes via an assumed IAM role; Dagster's runtime role gets `s3:GetObject` on the same prefix.
- **Snapshot expiration.** Snowflake's `ALTER ICEBERG TABLE ... DROP SNAPSHOTS` can invalidate version-pinned reads. Coordinate retention if backfills span weeks.
- **Snowflake-managed catalog is fundamentally different from a federated Iceberg catalog.** Snowflake stays the writer; you can't write from another engine to a Snowflake-managed table. For multi-writer, switch the table to `CATALOG='ICEBERG_REST'` against an external catalog like Polaris or Lakekeeper.

## See also

- [`iceberg_pipeline.md`](iceberg_pipeline.md) — generic Iceberg walkthrough
- [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) — full cross-vendor blueprint
- [`databricks_delta_to_dagster.md`](databricks_delta_to_dagster.md) — sister scenario: Databricks → Dagster
- [Snowflake-managed Iceberg docs](https://docs.snowflake.com/en/user-guide/tables-iceberg)
- [Snowflake REST catalog](https://docs.snowflake.com/en/user-guide/tables-iceberg-rest-snowflake)
