# Apache Pulsar — ingest, monitor, observe (Docker)

**Components:** `pulsar_to_database_asset`, `pulsar_monitor`, `pulsar_observation_sensor`, `python_callable_job`

**Script:** [`setup_pulsar_demo.sh`](./setup_pulsar_demo.sh)
**Cost:** $0 — Pulsar runs locally in standalone mode via Docker
**Validated:** end-to-end against `apachepulsar/pulsar:latest`, 25 seeded messages → SQLite table.

## Why this exists

Pulsar is the "unified queue + stream" broker (persistent topics with pub-sub AND queueing semantics, geo-replication built in, no ZooKeeper). If you're on Pulsar and using Dagster, you want three things:

- **Ingest** — pull messages off a topic into a warehouse / DB in scheduled batches (Dagster asset materialization).
- **Sensor-drive jobs** — new messages arrive → trigger a job.
- **Observe** — surface topic lag / message counts as Dagster observations for lineage + monitoring.

This demo exercises all three components against a real Pulsar broker in Docker.

```
Local Pulsar (standalone, apachepulsar/pulsar:latest)
        ↓
persistent://public/default/events  (25 seeded JSON messages)
        ↓
├── pulsar_events_ingest    ← pulsar_to_database_asset → SQLite raw_events table
├── pulsar_events_sensor    ← pulsar_monitor            → triggers python_callable_job on new messages
└── pulsar_events_obs       ← pulsar_observation_sensor → asset observations for lag/count
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `docker` — Pulsar runs locally in standalone mode

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pulsar_demo.sh -o setup_pulsar_demo.sh
chmod +x setup_pulsar_demo.sh
./setup_pulsar_demo.sh
```

## What the script does

1. Starts `apachepulsar/pulsar:latest` in standalone mode on `localhost:6650` (broker) + `localhost:8081` (admin HTTP). First boot takes 25–45s.
2. Seeds 25 JSON messages to `persistent://public/default/events` via a Python producer sidecar (`pulsar-client`). *Note: the `pulsar-client` CLI's `-m` flag has a longstanding bug that splits JSON payloads byte-by-byte — the script sidesteps this with the Python client.*
3. Scaffolds a Dagster project + installs `pulsar-client`, `pandas`, `sqlalchemy`, plus 4 components via the CLI.
4. Writes 4 `defs.yaml`:
   - `pulsar_events_ingest` — reads up to 100 messages from the topic and lands them in a SQLite `raw_events` table.
   - `pulsar_events_sensor` — polls the topic, fires `process_pulsar_messages` job when messages arrive.
   - `pulsar_events_obs` — emits asset observations every 60 seconds.
   - `python_callable_job` — placeholder target job for the sensor.

## Verify

```bash
cd pulsar-demo
uv run dg check defs
uv run dg launch --assets pulsar_events_ingest
sqlite3 /tmp/pulsar-demo.db 'SELECT COUNT(*) FROM raw_events;'    # 25
```

## Extensions

- **Real sink.** Replace the SQLite `database_url` with Postgres / MySQL / Snowflake / BigQuery — `pulsar_to_database_asset` uses SQLAlchemy, so any URL that library speaks works.
- **Real target job.** Replace `python_callable_job` (which is a `os.path:exists` no-op) with the actual downstream processing you want to trigger on new messages.
- **StreamNative Cloud / geo-replicated Pulsar.** Swap `service_url: pulsar://localhost:6650` for `pulsar+ssl://<cluster>.streamnative.g.snio.cloud:6651` and add `auth_params_env_var: PULSAR_TOKEN`. Same YAML shape.
- **Schema registry.** Point the component at a Pulsar schema-registry-backed topic; deserialization stays the same.

## Cleanup

```bash
docker rm -f dg-pulsar-demo
```

## Related

- [Kafka](./kafka.md) — same three-component shape (ingest + monitor + observation) against Kafka.
- [NATS](./nats.md) — same shape, NATS JetStream instead.
- [RabbitMQ](./rabbitmq.md) — same shape, AMQP.
- [MQTT](./mqtt.md) — same shape, IoT-oriented.
