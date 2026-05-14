# Kafka end-to-end — local broker, no SaaS, no auth

Full validation of the Kafka community-component family against a single-container Kafka broker running locally in KRaft mode (no Zookeeper, no managed cluster, no auth). The same components work unchanged against MSK / Confluent Cloud / Strimzi / self-hosted clusters — just swap `bootstrap_servers` + `security_protocol`.

## Components used

| Component | Source | Role |
|---|---|---|
| `external_kafka_asset` | community | Declare-only `AssetSpec` for the topic (lineage / catalog) |
| `kafka_resource` | community | Shared Kafka connection config — referenced by sensors and observations |
| `kafka_to_database_asset` | community | Consume N messages → write to a database table via SQLAlchemy |
| `kafka_monitor` | community | Sensor: poll Kafka, trigger a job when new messages arrive |
| `kafka_observation_sensor` | community | Emit `AssetObservation` events on the external Kafka asset |
| `python_callable_job` | community | Target job kicked off by `kafka_monitor` (placeholder) |

## Architecture

```
   ┌─────────────────────────────┐
   │ Kafka broker (KRaft, Docker)│
   │ localhost:9092              │
   │ topic: events (50 JSON msgs)│
   └────────────┬────────────────┘
                │
        ┌───────┴────────┬───────────────────────┬────────────────────┐
        │                │                       │                    │
        ▼                ▼                       ▼                    ▼
   external/events    kafka_events_ingest    kafka_events_sensor     kafka_events_observation
   (declare-only)     (consume + insert)     (sensor)                (sensor)
                      → SQLite raw_events    triggers job            → AssetObservation on
                                              process_kafka_messages   external/events
```

## Prereqs

- **Docker daemon must be running.** The setup script starts a single `bitnami/kafka:latest` container in KRaft mode on port 9092. If Docker isn't up, the script exits early with a clear message.

## Run it

```bash
bash setup_kafka_demo.sh
cd kafka-demo
source .env.demo
uv run dg check defs
uv run dg list defs
```

Drain the topic into SQLite:

```bash
uv run dg launch --assets kafka_events_ingest
```

Verify what landed:

```bash
sqlite3 /tmp/kafka-demo.db 'SELECT COUNT(*) FROM raw_events;'
# 50
sqlite3 /tmp/kafka-demo.db 'SELECT * FROM raw_events LIMIT 3;'
```

Cleanup:

```bash
docker rm -f dg-kafka-demo
```

## Why each component

### `external_kafka_asset` — declare-only

The Kafka topic exists outside Dagster's execution model — Dagster doesn't *write* to it, it reads. Declaring it as an `external_kafka_asset` puts the topic in the asset graph as a node so downstream consumers (the ingestion asset, the observation sensor) have lineage. No execution happens against this asset.

### `kafka_resource` — shared config

Centralizes the broker connection so sensors and observation components reference one place. `bootstrap_servers`, `security_protocol`, optional SASL — wire once, every downstream component reuses it.

### `kafka_to_database_asset` — batch consume

Connects to Kafka with the configured consumer group, polls until `max_messages` or `poll_timeout_seconds`, parses each message body as JSON, and writes the resulting rows to a SQLAlchemy table. Idempotent only at the consumer-group level (offsets are committed) — runs are NOT replayable, so design for the asset to be append/replace per run, not per-partition-key.

For partition-bounded reads, set `partition_type: daily` + `partition_start_date`; the asset will pull the topic windowed by that day.

### `kafka_monitor` — sensor

Polls Kafka on a fixed interval. When it sees new messages on the topic, it emits a `RunRequest` for the configured `job_name`. Use case: "kick off a downstream pipeline when ≥N new messages arrive." Defaults to `STOPPED` — turn on in the UI when you're ready.

In this demo the target is `python_callable_job` calling `os.path:exists` as a placeholder — substitute your real job (warehouse load, dbt run, alert dispatch, etc.).

### `kafka_observation_sensor` — health signal

Emits an `AssetObservation` on `external/events` every `check_interval_seconds`, recording metadata like lag, current offset, message count. Doesn't trigger runs — it's a freshness/health pulse for the catalog. Pair with Dagster's freshness UI to see "this topic was last observed N minutes ago."

## What you can't simulate locally

This demo covers the 4 sunny-path components. Things you'll only encounter against a real production Kafka:

- **SASL/SSL.** Set `security_protocol: SASL_SSL` + `sasl_mechanism: PLAIN | SCRAM-SHA-256 | SCRAM-SHA-512` + `sasl_username_env_var` / `sasl_password_env_var`. Local KRaft mode uses PLAINTEXT.
- **Schema Registry.** Components consume JSON message bodies directly. For Avro/Protobuf with a Confluent Schema Registry, the message bodies need decoding first — out of scope for this family today.
- **Multi-partition rebalancing.** Demo uses `partitions: 1`. With N>1, multiple consumer instances will rebalance ownership — kafka_to_database_asset is single-process so it gets all partitions assigned to its consumer group.
- **Compacted topics.** Components don't currently filter tombstones (`value=null`) — they'd land as rows with all-null columns.

## Production retargeting

Production Kafka changes affect only the `bootstrap_servers` / `security_protocol` / `sasl_*` fields. The component graph stays identical:

```yaml
# Confluent Cloud
bootstrap_servers: pkc-12345.us-east-1.aws.confluent.cloud:9092
security_protocol: SASL_SSL
sasl_mechanism: PLAIN
sasl_username_env_var: CC_API_KEY
sasl_password_env_var: CC_API_SECRET

# AWS MSK with IAM
bootstrap_servers: b-1.mycluster.kafka.us-east-1.amazonaws.com:9098
security_protocol: SASL_SSL
sasl_mechanism: AWS_MSK_IAM

# Self-hosted SCRAM
bootstrap_servers: kafka-01:9092,kafka-02:9092,kafka-03:9092
security_protocol: SASL_SSL
sasl_mechanism: SCRAM-SHA-512
```

## See also

- [`external_assets.md`](external_assets.md) — declare-only asset family (incl. `external_kafka_asset`)
- [`composition_primitives.md`](composition_primitives.md) — `python_callable_job` and the other small-job wrappers
- [`s3_pipeline.md`](s3_pipeline.md) — same Docker-backed local-validation pattern (Minio for S3)
