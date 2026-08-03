# Azure Service Bus producer + consumer

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

Mirror to the Event Hubs round-trip but for Service Bus —
ordered queues + topics with DLQ + sessions + transactions.

## Components used

| Component | Category | Role |
|---|---|---|
| `dataframe_to_servicebus` (NEW) | sink | Push DataFrame rows as JSON messages |
| `servicebus_to_database_asset` | ingestion | Drain messages → DB table |
| `servicebus_monitor` | sensor | Trigger ingestion on new messages |

## Status

Code-validated. To run end-to-end:

```bash
RG=dagster-demo-rg
SB=dgsb$(openssl rand -hex 4)
az servicebus namespace create -g "$RG" -n "$SB" -l eastus --sku Basic
az servicebus queue create -g "$RG" --namespace-name "$SB" -n demo-queue
SB_CONN=$(az servicebus namespace authorization-rule keys list \
    -g "$RG" --namespace-name "$SB" --name RootManageSharedAccessKey \
    --query primaryConnectionString -o tsv)
export SB_CONNECTION_STRING="$SB_CONN"
```

## defs.yaml

```yaml
# Producer
type: dagster_component_templates.DataframeToServiceBusComponent
attributes:
  asset_name: orders_to_servicebus
  upstream_asset_key: orders_raw
  connection_string_env_var: SB_CONNECTION_STRING
  destination_name: demo-queue
  destination_type: queue              # 'queue' | 'topic'
  session_id_column: customer_id        # required if queue has sessions enabled
  batch_size: 100

# Consumer (existing)
type: dagster_component_templates.ServicebusToDatabaseAssetComponent
attributes:
  asset_name: orders_from_servicebus
  deps: [orders_to_servicebus]
  connection_string_env_var: SB_CONNECTION_STRING
  source_name: demo-queue
  source_type: queue
  database_url_env_var: DATABASE_URL
  table_name: orders_received
  if_exists: replace
  max_messages: 200
```

## Service Bus vs Event Hubs

| Pattern | SB | EH |
|---|---|---|
| Ordered FIFO | ✓ | partial (per-partition) |
| DLQ | ✓ | ✗ |
| Sessions / per-key ordering | ✓ | partial |
| Transactions | ✓ | ✗ |
| Throughput | medium | high |
| Per-msg cost | $$ | $ |

Use SB when you need enterprise messaging semantics; use EH for
high-throughput event streaming.

## See also

<!-- TODO: link related walkthroughs -->
