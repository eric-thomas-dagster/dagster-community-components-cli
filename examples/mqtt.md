# MQTT end-to-end — local docker, no auth
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Exercise the MQTT family against a local Eclipse Mosquitto broker. Same components target HiveMQ Cloud / AWS IoT Core / managed brokers unchanged — only `broker_host` / TLS / auth env vars change.

## Components used

| Component | Source | Role |
|---|---|---|
| `mqtt_to_database_asset` | community | Subscribe + collect-for-N-seconds → write rows to a database table |
| `mqtt_monitor` | community | Sensor: poll subscribe-and-collect window, trigger a job on new messages |
| `mqtt_observation_sensor` | community | Emit `AssetObservation` on a topic |
| `python_callable_job` | community | Target job for mqtt_monitor |

## Run

```bash
bash setup_mqtt_demo.sh
cd mqtt-demo
source .env.demo
uv run dg check defs

# MQTT is fire-and-forget. Start a publisher in one shell, asset in another.
# (Background the publisher with a small delay so it lands during the asset's collect window.)
( docker run --rm --network host eclipse-mosquitto:2 sh -c "
    sleep 5
    for i in \$(seq 1 30); do
      mosquitto_pub -h localhost -p 1883 -t sensors/factory/temp \
        -m \"{\\\"sensor_id\\\":\$i,\\\"temp_c\\\":\$((20+i%10))}\" -q 1
      sleep 0.3
    done" >/dev/null & )
uv run dg launch --assets mqtt_sensors_ingest
sqlite3 /tmp/mqtt-demo.db 'SELECT COUNT(*) FROM raw_sensor_readings;'   # 30
```

Cleanup: `docker rm -f dg-mqtt-demo`.

## YAML shape

```yaml
type: dagster_component_templates.MQTTToDatabaseAssetComponent
attributes:
  asset_name: mqtt_sensors_ingest
  broker_host_env_var: MQTT_BROKER_HOST
  topic: sensors/factory/temp           # MQTT wildcards: '#' / '+' supported
  database_url_env_var: DATABASE_URL
  table_name: raw_sensor_readings
  collect_seconds: 15
  max_messages: 100
```

## Demo notes

- **Mosquitto 2.x requires explicit `allow_anonymous true` config.** The setup script writes a minimal mosquitto.conf inline.
- **MQTT is fire-and-forget by default.** Without QoS-2 + retained messages, a message published when no subscriber is connected is gone. The demo publishes *during* the asset's `collect_seconds` window via a background `mosquitto_pub` loop.
- **For "pull historical" semantics, use a broker with persistence** (HiveMQ Enterprise, EMQ X with replay) — out of scope for the community components today.

## Production retargeting

```yaml
# AWS IoT Core
broker_host: a1b2c3d4e5.iot.us-east-1.amazonaws.com
broker_port: 8883
use_tls: true
# Plus IAM/X.509 client certs — component currently assumes broker_host/port-only
# auth. Add SASL/TLS-cert fields to the component for IoT Core production use.

# HiveMQ Cloud
broker_host: abc12345.s1.eu.hivemq.cloud
broker_port: 8883
use_tls: true
username_env_var: HIVEMQ_USERNAME
password_env_var: HIVEMQ_PASSWORD
```

## See also

- [`kafka.md`](kafka.md), [`rabbitmq.md`](rabbitmq.md), [`nats.md`](nats.md) — sibling broker families
- [`composition_primitives.md`](composition_primitives.md) — small jobs
