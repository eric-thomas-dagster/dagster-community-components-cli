# OpenTelemetry full-stack demo (metrics + logs + traces)

Push synthetic pipeline data through all three OpenTelemetry signals
(metrics, logs, traces) to a single OTLP/HTTP endpoint. **One set of
sinks → ANY OTel-compatible backend**: OTel collector, Honeycomb,
Lightstep, Datadog (OTLP intake), Splunk Observability, Grafana Cloud,
New Relic, AWS X-Ray (via collector), GCP Cloud Operations (via collector).

```
synthetic_data_generator → orders_raw  ──┬─→ dataframe_to_otlp_metrics  (orders.total counter, by category + status)
                                          ├─→ dataframe_to_otlp_logs     (one log per order, body=order_id)
                                          └─→ dataframe_to_otlp_traces   (one span per order, grouped by customer_id trace)
                                              │
                                              ▼
                                   OTel collector / vendor backend
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | 30 synthetic orders |
| 2 | [`dataframe_to_otlp_metrics`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_metrics) | sink | Push counter `orders.total` with category/status attributes |
| 3 | [`dataframe_to_otlp_logs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_logs) | sink | One log per order with order_id body, customer_id/category/total attributes |
| 4 | [`dataframe_to_otlp_traces`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_traces) | sink | One span per order, trace_id derived from customer_id (groups orders into customer journeys) |

## Why one demo, three signals

OpenTelemetry's value is the **shared abstraction** — a single SDK,
endpoint, and config for metrics + logs + traces. All three sinks here
share the same `endpoint` config and bearer-token auth pattern; swapping
your backend is changing one URL.

## Backend swap matrix

| Backend | endpoint | bearer_token_env_var | extra_headers |
|---|---|---|---|
| Local OTel collector | `http://localhost:4318` | — | — |
| Honeycomb | `https://api.honeycomb.io` | `HONEYCOMB_API_KEY` | `{x-honeycomb-dataset: production}` |
| Lightstep / ServiceNow CMP | `https://ingest.lightstep.com` | `LIGHTSTEP_TOKEN` | — |
| Datadog (OTLP intake) | `https://api.datadoghq.com` | — | `{DD-API-KEY: <key>}` |
| Splunk Observability | `https://ingest.<realm>.signalfx.com` | `SPLUNK_TOKEN` | — |
| New Relic | `https://otlp.nr-data.net` (US) or `https://otlp.eu01.nr-data.net` (EU) | — | `{api-key: <key>}` |
| Grafana Cloud | `https://otlp-gateway-<region>.grafana.net/otlp` | — | `{Authorization: Basic <base64-of-instance-id:api-key>}` |
| AWS X-Ray / CloudWatch | `http://localhost:4318` (via AWS Distro for OTel collector) | — | — |

## Prerequisites

| Need | How to get it |
|---|---|
| Docker | for the local OTel collector |
| Python 3.10+ + `uv` | standard |

## Provisioning

The setup script handles this — starts a local OTel collector container
that logs all received signals to stdout (great for local dev /
verification). Swap `OTLP_ENDPOINT` env var to point at any vendor.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_opentelemetry_demo.sh | bash
cd opentelemetry-demo
uv run dg launch --assets '*'
```

## Verify

The local OTel collector logs every received signal:

```bash
docker logs dg-otel-demo 2>&1 | tail -100
```

Look for:
- `Name: orders.total` (the counter metric)
- `Body: Str(ORD00000001)` (log records)
- `Name : ORD00000001` (spans)

## Validated end-to-end

| Step | Result |
|---|---|
| Metrics | 2 region samples → 3 data points received by collector ✓ |
| Logs | 2 log records (INFO + WARN) with `service.name=dagster_log_test`, attributes preserved ✓ |
| Traces | 3 spans (`fetch_orders`, `process_orders`, `publish_metrics`) received with timing intact ✓ |

(Validated against the `otel/opentelemetry-collector-contrib:latest`
container with the standard `otlp` receiver + `debug` exporter.)

## Cost

| Resource | Cost |
|---|---|
| Local OTel collector (Docker) | $0 |
| Honeycomb / Lightstep / etc. (free tiers) | typically $0 for low volume |

## Teardown

```bash
docker rm -f dg-otel-demo
```

## Variations

- **Single signal**: drop the sinks you don't need (e.g. only emit
  metrics, no logs/traces)
- **Multi-vendor fan-out**: add multiple sink components, each pointing
  at a different backend — test data sovereignty / multi-region setups
- **Production tracing**: replace [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) with real
  ETL outputs and use `trace_id_column` to group related work into
  meaningful traces
- **Custom attributes**: add any DataFrame columns to `attribute_columns`
  to pivot/filter on them in your tracing UI
