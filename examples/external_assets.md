# External assets — declare 21 integrations in one code location
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** — `dg check` passes with all 21 external_*_asset
components loaded. They're declare-only (no compute), so this validates
that every external integration in the registry produces a valid
`AssetSpec` with the right kinds + metadata, and supports the canonical
partition shape.

```
warehouses (5):    snowflake, bigquery, databricks, clickhouse, sql
object_stores (5): s3, gcs, adls, sftp, sharepoint
streaming (11):    kafka, kinesis, eventhubs, pubsub, sqs, servicebus,
                   pulsar, nats, rabbitmq, mqtt, redis_stream
```

## Components used

- `external_adls_asset`
- `external_bigquery_table`
- `external_clickhouse_table`
- `external_databricks_table`
- `external_delta_table`
- `external_eventhubs_asset`
- `external_gcs_asset`
- `external_iceberg_table`
- `external_kafka_asset`
- `external_kinesis_asset`
- `external_mqtt_asset`
- `external_nats_asset`
- `external_pubsub_asset`
- `external_pulsar_asset`
- `external_rabbitmq_asset`
- `external_redis_stream_asset`
- `external_s3_asset`
- `external_servicebus_asset`
- `external_sftp_path`
- `external_sharepoint_library`
- `external_snowflake_table`
- `external_sql_asset`
- `external_sqs_asset`

## Cost

**$0.** No backend reached. Each component just builds an `AssetSpec`
with metadata describing the external resource (account, region, URI,
etc.). Once you point at a real backend, Dagster tracks the asset's
freshness, lineage, and metadata — but the demo itself is offline.

## Components used (21)

| Family | Component | Asset key |
|---|---|---|
| Warehouse | `external_snowflake_table` | `snowflake/raw/orders` (partitioned: dynamic by tenant) |
| Warehouse | `external_bigquery_table` | `bigquery/analytics/page_views` |
| Warehouse | `external_databricks_table` | `databricks/silver/sessions` |
| Warehouse | `external_clickhouse_table` | `clickhouse/events/clicks` |
| Warehouse | `external_sql_asset` | `sql/orders` |
| Object store | `external_s3_asset` | `s3/raw/orders` |
| Object store | `external_gcs_asset` | `gcs/raw/orders` |
| Object store | `external_adls_asset` | `adls/raw/orders` |
| Object store | `external_sftp_path` | `sftp/incoming/orders` |
| Object store | `external_sharepoint_library` | `sharepoint/Documents` |
| Streaming | `external_kafka_asset` | `kafka/orders` |
| Streaming | `external_kinesis_asset` | `kinesis/orders-stream` |
| Streaming | `external_eventhubs_asset` | `eventhubs/orders` |
| Streaming | `external_pubsub_asset` | `pubsub/orders-topic` |
| Streaming | `external_sqs_asset` | `sqs/orders-queue` |
| Streaming | `external_servicebus_asset` | `servicebus/orders` |
| Streaming | `external_pulsar_asset` | `pulsar/orders` |
| Streaming | `external_nats_asset` | `nats/orders` |
| Streaming | `external_rabbitmq_asset` | `rabbitmq/orders` |
| Streaming | `external_mqtt_asset` | `mqtt/sensors/temp` |
| Streaming | `external_redis_stream_asset` | `redis_stream/orders` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_external_assets_demo.sh | bash
cd external-assets-demo
uv run dg check defs        # validates all 21 components load
uv run dg dev               # http://localhost:3000 → Assets graph
```

## What this validates

- Every `external_*_asset` component declares `dg.AssetSpec(...)` with
  the right metadata, asset kinds, and (optional) `partitions_def`.
- The canonical partition shape works on declare-only assets — the
  Snowflake table in this demo is `partition_type: dynamic,
  dynamic_partition_name: tenants` (multi-tenant SaaS pattern).
- Schema validation passes: every required field is set, no
  unrecognized fields slip through.
- The asset graph has 21 sources grouped by integration kind, ready to
  connect with downstream `dataframe_to_*` sinks or
  `lineage_to_<catalog>` exports.

## Next step: connect a downstream

Once external assets are declared, point a downstream at one to start
materializing:

```yaml
# Materialize a transformation from the Snowflake source
type: dagster_component_templates.DataFrameTransformerComponent
attributes:
  asset_name: orders_clean
  upstream_asset_key: snowflake/raw/orders
  drop_duplicates: true
  filter_expression: "amount > 0"
```

Or pair with `PerPartitionBackfillJob` to drive the dynamic-partition
Snowflake source per-tenant — see `partitions.md`.

## See also

<!-- TODO: link related walkthroughs -->
