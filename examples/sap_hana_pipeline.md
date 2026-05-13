# SAP HANA → Dagster pipeline blueprint

Query SAP HANA Cloud, on-premise HANA, or HANA-on-Azure directly via SQL → Dagster asset.

When you want **raw table-level access** (versus the OData layer of S/4HANA), HANA SQL is the right entry point. Calculation Views, CDS Views, and ABAP-replicated tables all show up here.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ SAP HANA                                            │
   │   • Cloud:    myhana.hanacloud.ondemand.com:443     │
   │   • On-prem:  hana01.acme.com:30015                 │
   │   • Azure:    same as Cloud (preview)               │
   └─────────────────────────────┬───────────────────────┘
                                 │ SQLAlchemy + sqlalchemy-hana + hdbcli
                                 │ TLS, encrypted
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ sap_hana_ingestion (asset)                          │
   │   SELECT ... FROM "_SYS_BIC"."yourpackage.YourCV"   │
   │   WHERE BUSINESS_DATE = '{partition_key}'           │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Downstream transforms + sinks                       │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| [`sap_hana_resource`](https://dagster-component-ui.vercel.app/c/sap_hana_resource) | community | Register HANA connection once (URL builder + helpers) |
| [`sap_hana_ingestion`](https://dagster-component-ui.vercel.app/c/sap_hana_ingestion) | community | SQL → DataFrame asset |
| [`dataframe_to_table`](https://dagster-community-components-cli.vercel.app/c/dataframe_to_table) | community | Write DataFrame BACK to a HANA table (or any SQLAlchemy DB) |

## defs.yaml — HANA Cloud (basic auth, TLS)

```yaml
# defs/hana_customer_metrics/defs.yaml
type: dagster_community_components.SapHanaIngestionComponent
attributes:
  asset_name: hana_customer_metrics
  host: myhana.hanacloud.ondemand.com
  port: 443
  user: DAGSTER_RO
  password_env_var: HANA_PASSWORD
  encrypt: true              # required for HANA Cloud
  validate_certificate: true

  # SQL with partition_key substitution:
  query: |
    SELECT CUSTOMER_ID, REGION, REVENUE, BUSINESS_DATE
    FROM SAPABAP1.CUSTOMER_METRICS
    WHERE BUSINESS_DATE = TO_DATE('{partition_key}', 'YYYY-MM-DD')

  partition_type: daily
  partition_start: '2024-01-01'

  group_name: sap_hana
  kinds: [hana, sql]
```

## defs.yaml — HANA on-prem (multi-tenant DB)

On-prem HANA systems often have a system DB on port 30013 and tenant DBs on 30015 or similar:

```yaml
host: hana01.acme.com
port: 30015         # system port; HDI tenants typically use 3MM15 where MM is the instance number
database: HDB       # tenant DB name
user: DAGSTER_RO
password_env_var: HANA_PASSWORD
encrypt: true
validate_certificate: false   # if internal CA isn't trusted by your runtime
```

## defs.yaml — HANA Cloud via Calculation View

HANA Calculation Views appear as SQL views in the `_SYS_BIC` schema. The package + view name forms a dotted SQL identifier that needs quoting:

```yaml
query: |
  SELECT *
  FROM "_SYS_BIC"."salesanalytics.cv_DailySales/CV_DAILY_SALES"
  WHERE BUSINESS_DATE = '{partition_key}'
```

Note the inner `/CV_DAILY_SALES` segment — HANA's view URI convention.

## Connection-only via `sap_hana_resource`

If you have multiple HANA-reading assets, register the connection once:

```yaml
# resources/sap_hana.yaml
type: dagster_community_components.SapHanaResourceComponent
attributes:
  resource_key: hana
  host: myhana.hanacloud.ondemand.com
  port: 443
  user: DAGSTER_RO
  password_env_var: HANA_PASSWORD
  encrypt: true
```

Custom assets can then:

```python
@asset(required_resource_keys={"hana"})
def my_hana_asset(context):
    engine = context.resources.hana.get_engine()
    return pd.read_sql("SELECT ...", engine)
```

The `sap_hana_ingestion` component currently takes connection settings inline (mirroring the resource fields) — they don't conflict; use whichever fits.

## Writing back — `dataframe_to_table` against HANA

```yaml
# defs/hana_curated_dim/defs.yaml — write DataFrame → HANA table
type: dagster_community_components.DataframeToTableComponent
attributes:
  asset_name: hana_dim_customer_loaded
  upstream_asset_key: dim_customer
  destination_url_env_var: HANA_URL    # 'hana://user:pwd@host:443?encrypt=true'
  destination_table: DIM_CUSTOMER
  destination_schema: DAGSTER_OWNED
  if_exists: append          # or 'replace'
  group_name: sap_hana
```

`destination_url_env_var` should hold a full SQLAlchemy URL (use the resource's `url()` helper to mint it).

## Partitioning patterns

### Daily — date column filter
```yaml
query: SELECT * FROM SALES_FACT WHERE BUSINESS_DATE = TO_DATE('{partition_key}', 'YYYY-MM-DD')
partition_type: daily
partition_start: '2024-01-01'
```

### Per-customer — static partition
```yaml
query: SELECT * FROM SALES_FACT WHERE CUSTOMER_ID = '{partition_key}'
partition_type: static
partition_values: "C001,C002,C003"
```

### Dynamic — sensor-driven (e.g. ABAP-replicated CDS view with new entries)
```yaml
partition_type: dynamic
dynamic_partition_name: hana_business_dates
```

Pair with a `sql_monitor` sensor that polls `SELECT DISTINCT BUSINESS_DATE FROM SALES_FACT` and registers new dates as partitions.

## Driver dependencies

```
hdbcli>=2.18.0           # SAP-supplied; official Python driver, ships native binaries
sqlalchemy-hana>=0.5.0   # open-source SQLAlchemy dialect for HANA
sqlalchemy>=1.4
pandas>=1.5.0
```

All three are on PyPI — no SAP-internal repo access needed. `hdbcli` ships pre-built wheels for common platforms (Linux x86_64, macOS arm64, Windows).

## Performance tips

- **Push down to HANA.** HANA's columnar store is extremely fast — let SQL do the heavy aggregation, not pandas. Don't `SELECT *` from a 100M-row fact when you can `GROUP BY` in HANA.
- **Connection pooling.** The `sap_hana_resource`'s `get_engine()` uses SQLAlchemy connection pooling. For high-throughput pipelines, consider increasing `pool_size` (custom wrapper).
- **Calculation Views are pre-aggregated.** Often faster to read a CV than to re-derive the same aggregation in SQL. Check `_SYS_BIC` for what's exposed.
- **Don't disable certificate validation in prod.** `validate_certificate: false` is for dev / self-signed only.

## Asset check — table-level health

Use the existing `sql_check` family or build a custom check:

```yaml
type: dagster_community_components.SqlCheckComponent   # if available
attributes:
  asset_key: sap_hana/customer_metrics
  destination_url_env_var: HANA_URL
  query: SELECT COUNT(*) FROM SAPABAP1.CUSTOMER_METRICS WHERE BUSINESS_DATE = CURRENT_DATE
  expect_min: 1
```

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — read S/4HANA via OData (when raw HANA isn't exposed)
- [`sap_datasphere_pipeline.md`](sap_datasphere_pipeline.md) — federated views on top of HANA
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
