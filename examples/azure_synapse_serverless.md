# Azure Synapse Serverless SQL — query parquet without compute
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**No Synapse-specific component needed.** This demo proves the killer
Synapse Serverless SQL workflow works with the generic registry components
already in place: write parquet to ADLS Gen2, then query it with T-SQL via
`OPENROWSET` and zero compute provisioning.

```
synthetic_data_generator → dataframe_to_adls (parquet)
                                  │
                                  ▼
                          synapsefs/dagster-test/orders.parquet
                                  │
                          dataframe_from_sql (Synapse Serverless OPENROWSET)
                                  │
                                  ▼
                          orders_revenue_summary (aggregated DataFrame asset)
```

## Why no Synapse-specific component?

We considered building a `dataframe_from_synapse_serverless` component but
it would just be a duplicate of `dataframe_from_sql`. The OPENROWSET
construct is plain T-SQL the user passes via the `query` field, and the
connection string is just `mssql+pymssql://workspace-ondemand.sql.azuresynapse.net:1433/<db>`.

The generic component does the job, and stays useful for Postgres,
MySQL, Snowflake, BigQuery, DuckDB, and any other SQLAlchemy backend.

**Generalize where you can; specialize only where the SDK forces you to.**

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 100 synthetic orders |
| 2 | `dataframe_to_adls` | sink | Write parquet to ADLS Gen2 |
| 3 | `dataframe_from_sql` | source | OPENROWSET aggregation via Synapse Serverless |

## Synapse Serverless setup gotchas

The first time you query parquet via `OPENROWSET` with `DATA_SOURCE`, you
need three one-time setups:

1. **Storage Blob Data Reader** for the workspace's managed identity on
   the storage account (so Synapse can read the parquet)
2. **CREATE DATABASE demo** (you can't run `CREATE DATABASE SCOPED
   CREDENTIAL` in `master`)
3. In `demo`, run:
   ```sql
   CREATE MASTER KEY ENCRYPTION BY PASSWORD = '...';
   CREATE DATABASE SCOPED CREDENTIAL [synapsemsi]
       WITH IDENTITY = 'Managed Identity';
   CREATE EXTERNAL DATA SOURCE [synapsefs_ds]
       WITH (LOCATION = 'https://<acct>.dfs.core.windows.net/<fs>',
             CREDENTIAL = [synapsemsi]);
   ```

The setup script automates all of this. Common errors and fixes:

| Error | Cause | Fix |
|---|---|---|
| `Cannot find the CREDENTIAL '<URL>'` | OPENROWSET can't get to storage with SQL admin login | Use the EXTERNAL DATA SOURCE path above |
| `CREATE DATABASE SCOPED CREDENTIAL is not supported in master` | Must run in a non-master db | `CREATE DATABASE demo` first |
| `Please create a master key in the database or open the master key in the session` | DATABASE SCOPED CREDENTIAL needs encryption-at-rest key | `CREATE MASTER KEY ENCRYPTION BY PASSWORD = '...'` |

## Validated end-to-end

| Step | Result |
|---|---|
| `dataframe_to_adls` | 100 rows → `synapsefs/dagster-test/orders.parquet` in 4.3s |
| `dataframe_from_sql` (Serverless) | OPENROWSET aggregation → 7 rows × 3 columns in 3.27s |
| Query plan | Synapse scanned ~15KB (well under the 1TB/mo free tier) |

Sample output (`orders_revenue_summary`):

| category | n | revenue |
|---|---|---|
| Electronics | 18 | 8,142.31 |
| Clothing | 16 | 6,890.55 |
| Food | 14 | 5,234.78 |
| ... | ... | ... |

## Cost

| Resource | Cost |
|---|---|
| Synapse workspace | $0 |
| Serverless SQL queries | $0 first 1TB scanned/month, then $5/TB |
| Storage (parquet for this demo) | ~$0.0001/mo at <1MB |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_synapse_serverless_demo.sh | bash
cd azure-synapse-serverless-demo
uv run dg launch --assets '*'
```

## Variations

- **Larger query**: aggregate over millions of rows via wildcards:
  `BULK 'dagster-test/year=*/month=*/*.parquet'`
- **CSV instead of parquet**: change `FORMAT = 'PARQUET'` →
  `FORMAT = 'CSV', PARSER_VERSION = '2.0'`
- **Materialized aggregation**: pipe the result through `dataframe_to_table`
  to land aggregated results in Postgres / SQL Server for BI
- **Federate across multiple accounts**: create one EXTERNAL DATA SOURCE
  per storage account, reference each in different OPENROWSET calls in
  the same query

## See also

<!-- TODO: link related walkthroughs -->
