# Azure SQL Database demo

DataFrame → Azure SQL serverless via [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) (SQLAlchemy +
pymssql). Same pipeline shape as the movies_sql / cars_sql demos — flip
`DATABASE_URL` to `postgresql://...` or `mysql://...` and the same components
land data there instead. That's the point: one sink component, every SQL
backend.

```
synthetic_data_generator → dataframe_to_table → Azure SQL serverless (`orders` table)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | 100 synthetic e-commerce orders |
| 2 | [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) | sink | Write via SQLAlchemy + pymssql |

The same component would write to a different SQL flavor by changing only
the URL prefix (and the appropriate Python driver). That's why we don't
ship a dedicated "Azure SQL writer" component — [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) is it.

## Related per-DB components in the registry

If you want connection sharing or auto-table-per-asset semantics, the
registry has dedicated components:

| Backend | IO Manager | Resource |
|---|---|---|
| Azure SQL / SQL Server | [`mssql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mssql_io_manager) | (not yet — generic SQLAlchemy works) |
| PostgreSQL | [`postgres_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/postgres_io_manager) | [`postgres_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/postgres_resource) |
| MySQL | [`mysql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mysql_io_manager) | [`mysql_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mysql_resource) |

The IO managers automatically persist each asset's DataFrame to a per-asset
table; the resources expose a connection that downstream Python ops can
use for ad-hoc queries. For the simple "land DataFrame in SQL" pattern,
[`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) is the lightest weight option.

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.Sql` provider | `az provider register --namespace Microsoft.Sql --wait` |
| SQL Server + Database | See "Provisioning" below |
| Firewall rules | Allow Azure services + your current IP |
| `DATABASE_URL` env var | `mssql+pymssql://<user>:<urlencoded-pass>@<server>.database.windows.net:1433/demo` |

## Provisioning (one-time, ~3 min)

Note: Azure SQL Database has regional capacity constraints. If `eastus` rejects
your `az sql server create` call with `RegionDoesNotAllowProvisioning`, try
`westus3`, `eastus2`, `centralus`, or `westus2` — capacity moves around.

```bash
RG=dagster-demo-rg
SQL_SERVER=dgsql$(openssl rand -hex 4)
SQL_USER=dagsteradmin
SQL_PASS="P$(openssl rand -hex 12)!Aa"
LOC=westus3   # try eastus first; fall back as needed

az group create --name "$RG" --location "$LOC"
az sql server create -g "$RG" -n "$SQL_SERVER" -l "$LOC" \
    --admin-user "$SQL_USER" --admin-password "$SQL_PASS"

# Firewall: Azure services + current IP
az sql server firewall-rule create -g "$RG" -s "$SQL_SERVER" -n AllowAzure \
    --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
MYIP=$(curl -s https://api.ipify.org)
az sql server firewall-rule create -g "$RG" -s "$SQL_SERVER" -n AllowMyIP \
    --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# Serverless DB with auto-pause after 60 min idle
az sql db create -g "$RG" -s "$SQL_SERVER" -n demo \
    --edition GeneralPurpose --family Gen5 --capacity 1 \
    --compute-model Serverless --auto-pause-delay 60

# Build DATABASE_URL
PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$SQL_PASS")
export DATABASE_URL="mssql+pymssql://$SQL_USER:$PASS_ENC@$SQL_SERVER.database.windows.net:1433/demo"
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_sql_demo.sh | bash
cd azure-sql-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Provisioning | server + serverless DB ready; first connection auto-resumed it |
| [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) | 100 rows written to `dbo.orders` in 3.4s |
| Verification | `SELECT TOP 5 ... ORDER BY total DESC` returned the expected high-value rows |

Sample output:

```
order_id     customer_id   total   status
ORD00000059  CUST000973   775.43  cancelled
ORD00000095  CUST000947   723.13  shipped
ORD00000048  CUST000360   673.77  delivered
ORD00000055  CUST000291   635.00  pending
ORD00000003  CUST000734   629.43  cancelled
```

## Cost

Serverless GP_S_Gen5_1 with auto-pause after 60 min idle. While paused: storage
only (~$0.10/GB/month). When resumed for a query: ~$0.52 per vCore-hour billed
in 1-second increments. For the ~3-second materialization here, cost is
fractions of a cent.

## Teardown

```bash
az sql db delete -g dagster-demo-rg -s <server> -n demo --yes
az sql server delete -g dagster-demo-rg -n <server> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- Switch `if_exists: replace` → `append` for incremental loads
- Add [`dataframe_from_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_table) upstream to read from an existing Azure SQL
  table (e.g. for migrations) — flip the same `DATABASE_URL`
- Swap the URL prefix to `postgresql+psycopg://...` or `mysql+pymysql://...`
  to land in Azure Database for PostgreSQL / MySQL — see those demos
