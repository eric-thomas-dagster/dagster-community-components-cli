# Azure Database for MySQL Flexible Server demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

DataFrame → Azure MySQL Flexible Server via `dataframe_to_table` (SQLAlchemy
+ pymysql). Same pipeline shape as `azure_sql` and `azure_postgres` — flip
the URL prefix, the same components write to a different SQL backend.

```
synthetic_data_generator → dataframe_to_table → Azure MySQL ('orders' table)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 100 synthetic e-commerce orders |
| 2 | `dataframe_to_table` | sink | Write via SQLAlchemy + pymysql |

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.DBforMySQL` provider | `az provider register --namespace Microsoft.DBforMySQL --wait` |
| Flexible Server + database | See "Provisioning" below |
| Firewall rule | Your current IP allowed |
| `DATABASE_URL` env var | `mysql+pymysql://<user>:<urlencoded-pass>@<server>.mysql.database.azure.com:3306/demo` |

## Provisioning (one-time, ~2 min)

Try `eastus` first; fall back to `westus3`, `eastus2`, or `centralus` if
capacity is restricted.

```bash
RG=dagster-demo-rg
MY_SERVER=dgmy$(openssl rand -hex 4)
MY_USER=dagsteradmin
MY_PASS="P$(openssl rand -hex 12)!Aa"
LOC=westus3

az group create --name "$RG" --location "$LOC"
az mysql flexible-server create -g "$RG" -n "$MY_SERVER" -l "$LOC" \
    --admin-user "$MY_USER" --admin-password "$MY_PASS" \
    --sku-name Standard_B1ms --tier Burstable \
    --storage-size 32 --version 8.0.21 \
    --public-access 0.0.0.0 --yes
az mysql flexible-server db create -g "$RG" -s "$MY_SERVER" -d demo

MYIP=$(curl -s https://api.ipify.org)
az mysql flexible-server firewall-rule create -g "$RG" -n "$MY_SERVER" \
    --rule-name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# Demo simplification: disable require_secure_transport so we can use a
# simple pymysql URL without bundling a DigiCert root CA file.
az mysql flexible-server parameter set -g "$RG" --server-name "$MY_SERVER" \
    --name require_secure_transport --value OFF

PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$MY_PASS")
export DATABASE_URL="mysql+pymysql://$MY_USER:$PASS_ENC@$MY_SERVER.mysql.database.azure.com:3306/demo"
```

## SSL note for production

Azure MySQL Flexible Server enforces SSL by default
(`require_secure_transport=ON`). For production keep it ON and bundle the
DigiCert root CA, then point pymysql at it via:

```
mysql+pymysql://...?ssl_ca=/path/to/DigiCertGlobalRootCA.crt.pem
```

The cert is published at
<https://learn.microsoft.com/azure/mysql/flexible-server/how-to-connect-tls-ssl>.
The demo turns SSL off only to keep the URL clean.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_mysql_demo.sh | bash
cd azure-mysql-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Provisioning | server + db created in westus3 |
| `dataframe_to_table` | 100 rows written to `orders` in 1.99s |
| Verification | `SELECT * FROM orders ORDER BY total DESC LIMIT 5` returned high-value rows |

## Cost

| Resource | Cost |
|---|---|
| B1ms Burstable, 32GB storage | ~$0.018/hr (~$13/mo) |
| Cannot auto-pause | Stop manually with `az mysql flexible-server stop` |

## Teardown

```bash
az mysql flexible-server delete -g dagster-demo-rg -n <server> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- Swap `if_exists: replace` → `append` for incremental loads
- Use `mysql_io_manager` instead for auto-table-per-asset semantics
- Use `mysql_resource` for ad-hoc queries from other ops

## See also

<!-- TODO: link related walkthroughs -->
