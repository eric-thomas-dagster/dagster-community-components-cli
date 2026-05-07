# New Relic + Dynatrace observability sinks

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

SaaS observability sinks for New Relic and Dynatrace. Push
DataFrame rows as logs (NR), events (Dynatrace), or pull metrics back
via NRQL (NR) / Metrics API v2 (Dynatrace).

## Components

**New Relic:**
- `newrelic_resource` — REST/NerdGraph wrapper
- `dataframe_to_newrelic_logs` — push log events
- `newrelic_nrql_query` — NRQL → DataFrame

**Dynatrace:**
- `dynatrace_resource` — REST API v2 wrapper
- `dataframe_to_dynatrace_events` — push events to annotate timeline
- `dynatrace_metrics_query` — metrics → DataFrame

## Status

Code-validated against each vendor's published API. To run end-to-end
you need an account at the respective vendor:

- New Relic: free tier with 100 GB/month + 1 user (no credit card)
- Dynatrace: 15-day free trial

## NR auth + setup

```bash
# Sign up at newrelic.com → User menu → API Keys → User key
export NEW_RELIC_API_KEY=NRAK-xxxxxxxxxxxxxxxxx
export NEW_RELIC_ACCOUNT_ID=1234567   # found in URL bar
```

## Dynatrace auth + setup

```bash
# Settings → Access tokens → Generate (scopes: events.ingest, metrics.read)
export DT_API_TOKEN=dt0c01.AAAA...
export DT_ENV_URL=https://abc12345.live.dynatrace.com
```

## defs.yaml example — New Relic logs

```yaml
type: dagster_component_templates.DataframeToNewRelicLogsComponent
attributes:
  asset_name: orders_to_newrelic
  upstream_asset_key: orders_raw
  api_key_env_var: NEW_RELIC_API_KEY
  region: US
  log_type: dagster_orders_etl
  message_column: order_id
```

## defs.yaml example — Dynatrace events

```yaml
type: dagster_component_templates.DataframeToDynatraceEventsComponent
attributes:
  asset_name: deployments_to_dynatrace
  upstream_asset_key: deployments_log
  environment_url: https://abc12345.live.dynatrace.com
  api_token_env_var: DT_API_TOKEN
  event_type: CUSTOM_DEPLOYMENT
  title_column: deployment_name
  entity_selector: 'type(SERVICE),tag("env:prod")'
```

## OpenTelemetry alternative

Both NR and Dynatrace also accept OTLP/HTTP — you can use the
universal `dataframe_to_otlp_logs` / `dataframe_to_otlp_metrics` sinks
instead, with the appropriate OTLP endpoint + bearer token. See
[opentelemetry_demo.md](opentelemetry_demo.md). One sink per signal,
many backends.
