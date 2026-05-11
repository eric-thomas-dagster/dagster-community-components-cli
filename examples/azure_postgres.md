# Azure Database for PostgreSQL Flexible Server demo

DataFrame → Azure PostgreSQL Flexible Server via [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table)
(SQLAlchemy + psycopg2). Same pipeline shape as the `azure_sql` /
`azure_mysql` demos — flip the URL prefix, the same components write to a
different SQL backend.

```
synthetic_data_generator → dataframe_to_table → Azure PostgreSQL ('orders' table)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | 100 synthetic e-commerce orders |
| 2 | [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) | sink | Write via SQLAlchemy + psycopg2 |

## Per-DB components in the registry

| Backend | IO Manager | Resource |
|---|---|---|
| Azure PostgreSQL | [`postgres_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/postgres_io_manager) | [`postgres_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/postgres_resource) |
| Azure MySQL | [`mysql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mysql_io_manager) | [`mysql_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mysql_resource) |
| Azure SQL | [`mssql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mssql_io_manager) | [`mssql_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mssql_resource) |

For "land DataFrame in SQL" the lightest path is [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table). For
auto-table-per-asset semantics, use [`postgres_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/postgres_io_manager) instead.

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.DBforPostgreSQL` provider | `az provider register --namespace Microsoft.DBforPostgreSQL --wait` |
| Flexible Server + database | See "Provisioning" below |
| Firewall rule | Your current IP allowed |
| `DATABASE_URL` env var | `postgresql+psycopg2://<user>:<urlencoded-pass>@<server>.postgres.database.azure.com:5432/demo?sslmode=require` |

## Provisioning (one-time, ~2 min)

Try `eastus` first; fall back to `westus3`, `eastus2`, or `centralus` if
capacity is restricted.

```bash
RG=dagster-demo-rg
PG_SERVER=dgpg$(openssl rand -hex 4)
PG_USER=dagsteradmin
PG_PASS="P$(openssl rand -hex 12)!Aa"
LOC=westus3

az group create --name "$RG" --location "$LOC"
az postgres flexible-server create -g "$RG" -n "$PG_SERVER" -l "$LOC" \
    --admin-user "$PG_USER" --admin-password "$PG_PASS" \
    --sku-name Standard_B1ms --tier Burstable \
    --storage-size 32 --version 16 \
    --public-access 0.0.0.0 --yes
az postgres flexible-server db create -g "$RG" -s "$PG_SERVER" -d demo

MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create -g "$RG" -n "$PG_SERVER" \
    --rule-name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$PG_PASS")
export DATABASE_URL="postgresql+psycopg2://$PG_USER:$PASS_ENC@$PG_SERVER.postgres.database.azure.com:5432/demo?sslmode=require"
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_postgres_demo.sh | bash
cd azure-postgres-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Provisioning | server + db created in westus3 |
| [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) | 100 rows written to `orders` in 3.6s |
| Verification | `SELECT * FROM orders ORDER BY total DESC LIMIT 5` returned high-value rows |

## Cost

| Resource | Cost |
|---|---|
| B1ms Burstable, 32GB storage | ~$0.018/hr (~$13/mo) |
| Cannot auto-pause | Stop manually with `az postgres flexible-server stop` |

## Teardown

```bash
az postgres flexible-server delete -g dagster-demo-rg -n <server> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- Swap `if_exists: replace` → `append` for incremental loads
- Add [`dataframe_from_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_table) upstream to read from an existing Postgres
  table — flip the same `DATABASE_URL`
- Use [`postgres_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/postgres_io_manager) instead for auto-table-per-asset semantics
- Use [`postgres_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/postgres_resource) to expose a connection to other ops for ad-hoc
  queries
