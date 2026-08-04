# NATS end-to-end — local docker, JetStream, no auth
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Exercise the NATS family against a local `nats-server -js` (JetStream enabled). Same components target Synadia Cloud / self-hosted NATS clusters unchanged — only `servers` / `nats_url_env_var` content changes.

## Components used

| Component | Source | Role |
|---|---|---|
| `nats_to_database_asset` | community | Pull from JetStream consumer → write rows to a database table |
| `nats_monitor` | community | Sensor: poll JetStream, trigger a job on new messages |
| `nats_observation_sensor` | community | Emit `AssetObservation` on a stream (message count / freshness) |
| `python_callable_job` | community | Target job for nats_monitor |

## Run

```bash
bash setup_nats_demo.sh
cd nats-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets nats_events_ingest
sqlite3 /tmp/nats-demo.db 'SELECT COUNT(*) FROM raw_events;'   # 25
```

Cleanup: `docker rm -f dg-nats-demo`.

## YAML shape

```yaml
type: dagster_component_templates.NATSToDatabaseAssetComponent
attributes:
  asset_name: nats_events_ingest
  nats_url_env_var: NATS_URL                # nats://[user:pass@]host:port
  subject: events.demo
  use_jetstream: true
  stream_name: EVENTS
  consumer_name: dagster-ingest             # durable pull consumer name
  database_url_env_var: DATABASE_URL
  table_name: raw_events
  max_messages: 100
  fetch_timeout_seconds: 5.0
```

## Demo notes

The `nats:latest` Docker image is **minimal / distroless** — no shell, no `nats` CLI. The setup script uses a `natsio/nats-box` sidecar container for all stream / consumer / publish operations.

The demo pre-creates the durable consumer with `--deliver=all` before publishing, so the Dagster asset can read historical messages. JetStream's default delivery policy is `new` — without pre-creating the consumer, only messages published *after* the asset's first run would be visible. For production, decide explicitly: `deliver_policy=new` for tail-only consumption, `deliver_policy=all` for backfill on first run.

## Production retargeting

```yaml
# Synadia Cloud
export NATS_URL='tls://connect.ngs.global:4222'
# (plus credentials via NATS_CREDS file; component currently assumes URL-only auth)

# Self-hosted multi-server cluster
export NATS_URL='nats://nats-1:4222,nats://nats-2:4222,nats://nats-3:4222'
```

## Known component fix in this session

`nats_to_database_asset` previously called `js.subscribe(...)` and then `.fetch()` on the result — but `js.subscribe` returns a `PushSubscription`, which has no `.fetch()`. Switched to `js.pull_subscribe(...)` so the pull-style batch fetch works. Without this fix, the asset crashes with `AttributeError: 'PushSubscription' object has no attribute 'fetch'`.

## See also

- [`kafka.md`](kafka.md) — sibling streaming family (broker semantics)
- [`rabbitmq.md`](rabbitmq.md) — sibling AMQP family
- [`redis.md`](redis.md) — sibling streams family (Redis Streams)
