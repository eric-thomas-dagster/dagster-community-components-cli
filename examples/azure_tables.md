# Azure Tables Round-Trip
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** against live infrastructure.

100 synthetic orders → DataFrame → Azure Table Storage → OData filter
read-back → CSV report.

```
synthetic_data_generator → dataframe_to_azure_table → azure_table_reader → dataframe_to_csv
                                  │                          │
                                  └─→ Azure Table Storage ───┘
                                          (dagsterorders)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 100 synthetic orders |
| 2 | `dataframe_to_azure_table` | sink | Upsert each row keyed by (customer_id PK, order_id RK) |
| 3 | `azure_table_reader` | source | Read with OData filter `total gt 500.0` |
| 4 | `dataframe_to_csv` | sink | High-value-orders report |

## Validated end-to-end

| Step | Result |
|---|---|
| `dataframe_to_azure_table` | 100 entities upserted (~1s) |
| `azure_table_reader` | Filtered to ~9 high-value orders |
| `dataframe_to_csv` | `/tmp/azure_tables_high_value_orders.csv` written |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_tables_demo.sh | bash
cd azure-tables-demo
uv run dg launch --assets '*'
```

## Cost

Fractions of a cent. Tables are billed at $0.045/GB/mo + $0.00036 per 10K
transactions. This demo: <1KB data, <300 transactions.

## When to use Tables vs Cosmos DB

| Need | Tables | Cosmos DB |
|---|---|---|
| Cheap simple key-value | ✓ | $$$ |
| Global distribution / consistency tuning | ✗ | ✓ |
| SQL queries / secondary indexes | ✗ (OData only) | ✓ |
| Use existing Azure Storage account | ✓ | ✗ (separate Cosmos account) |

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
