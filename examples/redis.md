# Redis end-to-end — local docker, no auth
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Exercise the Redis community-component family against a single-container Redis running locally. The same components target managed Redis (ElastiCache / MemoryStore / Redis Cloud / Azure Cache) unchanged — only the connection fields change.

## Components used

| Component | Source | Role |
|---|---|---|
| `redis_resource` | community | Shared connection — `host` / `port` / `db` / `ssl` |
| `redis_streams_monitor` | community | Sensor: poll a Redis stream, trigger a job on new entries |
| `redis_stream_observation_sensor` | community | Emit `AssetObservation` on a stream (freshness / health) |
| `cache_invalidation_job` | community | Op job: `DEL` keys matching a glob pattern (`session:*`, `cache:*`) |
| `python_callable_job` | community | Target job for the streams_monitor sensor |

## Architecture

```
   ┌────────────────────────────┐
   │ Redis (Docker, port 6379)  │
   │   stream: events:incoming  │
   │     5 seeded entries       │
   │   keys: session:abc / def  │
   │         / ghi (3 seeded)   │
   └────┬──────────────┬────────┘
        │              │
   poll/observe   batch delete
        ▼              ▼
   ┌────────────┐  ┌──────────────────────┐
   │ sensors    │  │ cache_invalidation   │
   │  - streams │  │  flush 'session:*'   │
   │    monitor │  └──────────────────────┘
   │  - stream  │
   │    obs     │
   └────────────┘
```

## Prerequisites

- **Docker daemon running.**

## Run

```bash
bash setup_redis_demo.sh
cd redis-demo
source .env.demo

uv run dg check defs
uv run dg list defs    # 1 resource, 2 jobs, 2 sensors
```

Run the cache flush + verify keys are gone:

```bash
uv run dg launch --job session_cache_flush
docker exec dg-redis-demo redis-cli KEYS 'session:*'   # (empty)
```

Run the placeholder target job (what `redis_streams_monitor` would trigger on each new stream entry):

```bash
uv run dg launch --job process_stream_entries
```

Both should return `RUN_SUCCESS`.

The two sensors (`events_stream_sensor`, `events_stream_obs`) load and are visible in `dg list defs` / `dg dev`. They stay STOPPED by default — toggle on in the UI when you want them firing.

## YAML shape

```yaml
# Shared resource
type: dagster_component_templates.RedisResourceComponent
attributes:
  resource_key: redis_resource
  host: localhost
  port: 6379
  db: 0
  ssl: false               # set true for ElastiCache TLS / Redis Cloud
```

```yaml
# Sensor: poll stream, trigger job on new entries
type: dagster_component_templates.RedisStreamsMonitorSensorComponent
attributes:
  sensor_name: events_stream_sensor
  stream_name: events:incoming
  job_name: process_stream_entries
  host: localhost
  port: 6379
  db: 0
  password_env_var: REDIS_PASSWORD       # optional
  max_entries_per_poll: 100
  start_from_beginning: true
  minimum_interval_seconds: 30
```

```yaml
# Observation sensor: emit AssetObservation on each check
type: dagster_component_templates.RedisStreamObservationSensorComponent
attributes:
  sensor_name: events_stream_obs
  asset_key: streaming/events
  stream_name: events:incoming
  check_interval_seconds: 60
```

```yaml
# Cache invalidation — delete keys matching a glob
type: dagster_component_templates.CacheInvalidationJobComponent
attributes:
  job_name: session_cache_flush
  redis_url_env: REDIS_URL              # redis://[user:pass@]host:port[/db]
  pattern: 'session:*'                   # or null → FLUSHDB the whole db
  db: 0
  batch_size: 500                        # SCAN cursor batch size
```

## Trade-offs & gotchas

- **No data piped into Dagster assets.** This family is sensor + job-centric. To bring Redis-stream entries into the asset graph, pair `redis_streams_monitor` with a `kafka_to_database_asset`-style downstream that drains a window of entries on each trigger.
- **`pattern: null` flushes the whole db.** Be explicit (`session:*`, `cache:*`) unless you really mean FLUSHDB.
- **Streams require Redis 5+.** The Alpine 7-image we use is fine; older Redis 3/4 won't have `XADD` / `XREAD`.
- **Stream consumer groups not yet exposed.** `start_from_beginning` reads from `$0` / `$` — for shared consumer groups (multiple Dagster instances draining the same stream cooperatively), you'd need a `consumer_group:` field added to the component.

## Production retargeting

```yaml
# ElastiCache / Redis Cloud (TLS, primary endpoint)
host: clustercfg.my-cluster.0gswlt.use1.cache.amazonaws.com
port: 6379
ssl: true
password_env_var: REDIS_AUTH_TOKEN
```

Cache invalidation pattern usage examples:

| Pattern | Effect |
|---|---|
| `session:*` | Drop all session keys (e.g., after deploy) |
| `cache:user:*:profile` | Drop per-user profile caches |
| `tenant:42:*` | Drop everything for a removed tenant |
| `null` (omit pattern) | `FLUSHDB` the entire database |

## See also

- [`kafka.md`](kafka.md) — sibling streaming family (broker semantics, not key-value)
- [`composition_primitives.md`](composition_primitives.md) — other small-job wrappers (HTTP webhook, SQL maintenance)
- [`mongodb.md`](mongodb.md) — sibling database family (read/write components instead of streams)
