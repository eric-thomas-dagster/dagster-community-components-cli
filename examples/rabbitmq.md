# RabbitMQ end-to-end — local docker, no auth
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Exercise the RabbitMQ community-component family against a local AMQP broker. Same components target self-hosted clusters / Amazon MQ / CloudAMQP unchanged — only the `amqp_url_env_var` content changes.

## Components used

| Component | Source | Role |
|---|---|---|
| `rabbitmq_to_database_asset` | community | Consume N messages → write rows to a database table via SQLAlchemy |
| `rabbitmq_monitor` | community | Sensor: poll a queue, trigger a job on new messages |
| `rabbitmq_observation_sensor` | community | Emit `AssetObservation` on a queue (depth / consumer count / health) |
| `python_callable_job` | community | Target job for the streams monitor |

## Run

```bash
bash setup_rabbitmq_demo.sh
cd rabbitmq-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets rabbitmq_orders_ingest
sqlite3 /tmp/rabbitmq-demo.db 'SELECT COUNT(*) FROM raw_orders;'   # 30
```

Cleanup: `docker rm -f dg-rabbitmq-demo`.

## YAML shape

```yaml
type: dagster_component_templates.RabbitMQToDatabaseAssetComponent
attributes:
  asset_name: rabbitmq_orders_ingest
  amqp_url_env_var: RABBITMQ_URL            # amqp://[user:pass@]host:port[/vhost]
  queue_name: orders
  database_url_env_var: DATABASE_URL         # SQLAlchemy URL
  table_name: raw_orders
  max_messages: 100
  poll_timeout_seconds: 5
  if_exists: append                          # 'append' | 'replace' | 'fail'
```

```yaml
type: dagster_component_templates.RabbitMQMonitorSensorComponent
attributes:
  sensor_name: rabbitmq_orders_sensor
  host: localhost
  queue_name: orders
  job_name: process_rabbitmq_messages
  port: 5672
  virtual_host: /
  use_tls: false
  max_messages_per_poll: 50
  minimum_interval_seconds: 30
  default_status: stopped
```

## Demo notes

The setup script uses `rabbitmq:4-management` with two non-obvious flags:

- `--hostname rabbit` — pins the Erlang node name so the .erlang.cookie file isn't regenerated on each boot.
- `--user rabbitmq:rabbitmq` — forces the process to run as the `rabbitmq` user, sidestepping a Docker Desktop / VirtioFS permission race that causes RabbitMQ to fail with `Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces` on subsequent restarts of the same container name.

Queue declaration + seeding uses the **v4 rabbitmqadmin CLI** which is different from v3:

```bash
# v3 style (does NOT work in rabbitmq:4-management)
rabbitmqadmin declare queue name=orders

# v4 style
rabbitmqadmin declare queue --name orders --type classic --durable true
rabbitmqadmin publish message --routing-key orders --payload '{"k":"v"}'
```

## Production retargeting

```yaml
# CloudAMQP / Amazon MQ
export RABBITMQ_URL='amqps://user:pass@your-broker.cloudamqp.com:5671/vhost'

# rabbitmq_monitor / observation_sensor:
use_tls: true
port: 5671
```

The component graph stays the same — only env vars + `use_tls` flag flip.

## See also

- [`kafka.md`](kafka.md) — sibling streaming family
- [`redis.md`](redis.md) — sibling streams family (key-value-style streams)
- [`composition_primitives.md`](composition_primitives.md) — small jobs (HTTP webhook, SQL maintenance)
