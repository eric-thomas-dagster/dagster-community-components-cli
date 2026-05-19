#!/usr/bin/env bash
# External assets demo — declare 23 external (observable) assets in one
# code location and verify they all show up in the Dagster catalog with
# their kinds + metadata.
#
# WHAT THIS DEMONSTRATES
#   The full external_assets family. These are *declare-only*: each
#   component emits a `dg.AssetSpec(...)` with metadata + kinds + (optional)
#   partitions_def, but does NOT execute. They're how you bring an existing
#   Snowflake table / S3 prefix / Kafka topic / etc. into Dagster's catalog
#   for downstream lineage and freshness tracking.
#
#   Each component is wired to a placeholder host / endpoint / bucket. Dagster
#   never reaches out — the components only build AssetSpecs. So this demo
#   needs no real backend, runs in seconds, and validates that all 21
#   components load + appear in the asset graph.
#
# Asset graph (23 declare-only assets, one per integration):
#   snowflake/raw/orders    → ExternalSnowflakeTableAsset
#   bigquery/analytics/page_views → ExternalBigQueryTableAsset
#   databricks/silver/sessions → ExternalDatabricksTableAsset
#   clickhouse/events/clicks → ExternalClickHouseTableComponent
#   iceberg/sales/orders    → ExternalIcebergTableAsset
#   delta/lakehouse/events  → ExternalDeltaTableAsset
#   s3://demo-bucket/orders → ExternalS3Asset
#   gs://demo-bucket/orders → ExternalGcsAsset
#   az://demo-container/orders → ExternalAdlsAsset
#   kafka/orders            → ExternalKafkaAsset
#   kinesis/orders-stream   → ExternalKinesisAsset
#   eventhubs/orders        → ExternalEventHubsAsset
#   pubsub/orders-topic     → ExternalPubsubAsset
#   sqs/orders-queue        → ExternalSqsAsset
#   servicebus/orders       → ExternalServiceBusAsset
#   pulsar/orders           → ExternalPulsarAsset
#   nats/orders             → ExternalNatsAsset
#   rabbitmq/orders         → ExternalRabbitmqAsset
#   mqtt/sensors/temp       → ExternalMqttAsset
#   redis_stream/orders     → ExternalRedisStreamAsset
#   sql/orders              → ExternalSqlAsset
#   sftp/incoming/orders    → ExternalSftpPathAsset
#   sharepoint/Documents    → ExternalSharePointLibraryAsset
#
# Plus: orders_per_tenant uses the new partition shape (dynamic partitions
# on an external Snowflake table) — closes the loop on the original
# multi-tenant SaaS feedback that motivated the partition rework.
#
# COST: \$0 — fully local, no backend reached.

set -euo pipefail
PROJECT_DIR="${1:-external-assets-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 23 external_* components"
for c in external_adls_asset external_bigquery_table external_clickhouse_table \
         external_databricks_table external_delta_table external_eventhubs_asset \
         external_gcs_asset external_iceberg_table external_kafka_asset \
         external_kinesis_asset external_mqtt_asset external_nats_asset \
         external_pubsub_asset external_pulsar_asset external_rabbitmq_asset \
         external_redis_stream_asset external_s3_asset external_servicebus_asset \
         external_sftp_path external_sharepoint_library external_snowflake_table \
         external_sql_asset external_sqs_asset; do
  $CLI add $c --auto-install
done

echo ">>> Writing 23 declare-only defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "external_snowflake_table" "type: $PKG.components.external_snowflake_table.component.ExternalSnowflakeTableAsset
attributes:
  asset_key: snowflake/raw/orders
  account: myorg-us-east-1
  database: RAW
  schema_name: PUBLIC
  table_name: ORDERS
  group_name: warehouses
  partition_type: dynamic
  dynamic_partition_name: tenants"

write_yaml "external_bigquery_table" "type: $PKG.components.external_bigquery_table.component.ExternalBigQueryTableAsset
attributes:
  asset_key: bigquery/analytics/page_views
  project_id: my-gcp-project
  dataset_id: analytics
  table_id: page_views
  group_name: warehouses"

write_yaml "external_databricks_table" "type: $PKG.components.external_databricks_table.component.ExternalDatabricksTableAsset
attributes:
  asset_key: databricks/silver/sessions
  workspace_url: https://abc-12345.cloud.databricks.com
  schema_name: silver
  table_name: sessions
  group_name: warehouses"

write_yaml "external_clickhouse_table" "type: $PKG.components.external_clickhouse_table.component.ExternalClickHouseTableComponent
attributes:
  asset_key: clickhouse/events/clicks
  database: events
  table: clicks
  host_env_var: CLICKHOUSE_HOST
  group_name: warehouses"

write_yaml "external_iceberg_table" "type: $PKG.components.external_iceberg_table.component.ExternalIcebergTableAsset
attributes:
  asset_key: iceberg/sales/orders
  catalog_name: demo_catalog
  namespace: sales
  table_name: orders
  warehouse: s3://demo-bucket/warehouse
  catalog_type: rest
  owner_engine: snowflake
  group_name: lakehouse"

write_yaml "external_delta_table" "type: $PKG.components.external_delta_table.component.ExternalDeltaTableAsset
attributes:
  asset_key: delta/lakehouse/events
  table_uri: s3://demo-bucket/lakehouse/events
  owner_engine: spark
  group_name: lakehouse"

write_yaml "external_s3_asset" "type: $PKG.components.external_s3_asset.component.ExternalS3Asset
attributes:
  asset_key: s3/raw/orders
  bucket_name: demo-bucket
  prefix: orders/
  group_name: object_stores"

write_yaml "external_gcs_asset" "type: $PKG.components.external_gcs_asset.component.ExternalGcsAsset
attributes:
  asset_key: gcs/raw/orders
  bucket_name: demo-bucket
  group_name: object_stores"

write_yaml "external_adls_asset" "type: $PKG.components.external_adls_asset.component.ExternalAdlsAsset
attributes:
  asset_key: adls/raw/orders
  account_name: demoadls
  container_name: orders
  group_name: object_stores"

write_yaml "external_kafka_asset" "type: $PKG.components.external_kafka_asset.component.ExternalKafkaAsset
attributes:
  asset_key: kafka/orders
  bootstrap_servers: kafka:9092
  topic: orders
  group_name: streaming"

write_yaml "external_kinesis_asset" "type: $PKG.components.external_kinesis_asset.component.ExternalKinesisAsset
attributes:
  asset_key: kinesis/orders-stream
  stream_name: orders-stream
  group_name: streaming"

write_yaml "external_eventhubs_asset" "type: $PKG.components.external_eventhubs_asset.component.ExternalEventHubsAsset
attributes:
  asset_key: eventhubs/orders
  namespace: demohub.servicebus.windows.net
  eventhub_name: orders
  group_name: streaming"

write_yaml "external_pubsub_asset" "type: $PKG.components.external_pubsub_asset.component.ExternalPubsubAsset
attributes:
  asset_key: pubsub/orders-topic
  project_id: my-gcp-project
  topic_id: orders-topic
  group_name: streaming"

write_yaml "external_sqs_asset" "type: $PKG.components.external_sqs_asset.component.ExternalSqsAsset
attributes:
  asset_key: sqs/orders-queue
  queue_url: https://sqs.us-east-1.amazonaws.com/123456789012/orders
  group_name: streaming"

write_yaml "external_servicebus_asset" "type: $PKG.components.external_servicebus_asset.component.ExternalServiceBusAsset
attributes:
  asset_key: servicebus/orders
  namespace: demoservicebus.servicebus.windows.net
  group_name: streaming"

write_yaml "external_pulsar_asset" "type: $PKG.components.external_pulsar_asset.component.ExternalPulsarAsset
attributes:
  asset_key: pulsar/orders
  service_url: pulsar://pulsar:6650
  topic: persistent://public/default/orders
  group_name: streaming"

write_yaml "external_nats_asset" "type: $PKG.components.external_nats_asset.component.ExternalNatsAsset
attributes:
  asset_key: nats/orders
  servers: nats://nats:4222
  stream_name: orders
  group_name: streaming"

write_yaml "external_rabbitmq_asset" "type: $PKG.components.external_rabbitmq_asset.component.ExternalRabbitmqAsset
attributes:
  asset_key: rabbitmq/orders
  host: rabbitmq.internal
  queue_name: orders
  group_name: streaming"

write_yaml "external_mqtt_asset" "type: $PKG.components.external_mqtt_asset.component.ExternalMqttAsset
attributes:
  asset_key: mqtt/sensors/temp
  broker_host: mqtt.internal
  topic: sensors/temp
  group_name: streaming"

write_yaml "external_redis_stream_asset" "type: $PKG.components.external_redis_stream_asset.component.ExternalRedisStreamAsset
attributes:
  asset_key: redis_stream/orders
  stream_name: orders
  group_name: streaming"

write_yaml "external_sql_asset" "type: $PKG.components.external_sql_asset.component.ExternalSqlAsset
attributes:
  asset_key: sql/orders
  table_name: orders
  connection_string_env_var: SQL_CONN_STR
  group_name: warehouses"

write_yaml "external_sftp_path" "type: $PKG.components.external_sftp_path.component.ExternalSftpPathAsset
attributes:
  asset_key: sftp/incoming/orders
  host: sftp.internal
  remote_path: /incoming/orders
  group_name: object_stores"

write_yaml "external_sharepoint_library" "type: $PKG.components.external_sharepoint_library.component.ExternalSharePointLibraryAsset
attributes:
  asset_key: sharepoint/Documents
  site_url: https://contoso.sharepoint.com/sites/team
  library_name: Documents
  group_name: object_stores"

cat <<MSG

>>> Setup complete.

Validate all 21 declare-only assets load:
    cd $PROJECT_DIR
    uv run dg check defs

Browse them in the asset graph:
    uv run dg dev   # http://localhost:3000 → Assets graph

The 21 assets show up grouped by integration kind:
  - warehouses:    snowflake, bigquery, databricks, clickhouse, sql
  - object_stores: s3, gcs, adls, sftp, sharepoint
  - streaming:     kafka, kinesis, eventhubs, pubsub, sqs, servicebus,
                   pulsar, nats, rabbitmq, mqtt, redis_stream

Note: snowflake/raw/orders is partitioned (dynamic, by tenant). Combine
with PerPartitionBackfillJob to drive a multi-tenant catalog refresh.

This demo never reaches out to any backend — the components only build
AssetSpecs. Once you connect a real Snowflake / Kafka / S3, Dagster will
track the asset's freshness, lineage, and metadata automatically.
MSG
