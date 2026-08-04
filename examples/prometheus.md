# Prometheus full demo (push + pull)
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

**Validated end-to-end** against live infrastructure.

30 synthetic orders → unpivot to long form → push as gauges via
pushgateway → Prometheus scrapes → query back via PromQL → CSV report.

```
synthetic_data_generator → unpivot → dataframe_to_prometheus
                                              │
                                          pushgateway:9091
                                              │
                                       Prometheus scrapes (5s)
                                              │
                                  dataframe_from_prometheus → dataframe_to_csv
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 30 synthetic orders (wide format) |
| 2 | `unpivot` | transformation | Wide → long: 4 metric columns → 30×N long rows |
| 3 | `dataframe_to_prometheus` | sink | Push as gauges with (category, metric) labels |
| 4 | `dataframe_from_prometheus` | source | PromQL `sum by (category) (orders_metric{metric="total"})` |
| 5 | `dataframe_to_csv` | sink | Aggregated revenue-by-category report |

## Pattern: wide → long → metrics

The wide-to-long reshape is what enables the multi-metric pattern. With
the existing `unpivot` transform + `dataframe_to_prometheus` sink, you
don't need a Prometheus-specific transform layer.

## Validated end-to-end

- Local Prometheus + pushgateway containers (Docker)
- 4 sample series pushed (region × category) → Prometheus scraped → query
  returned all 4 with correct values

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_prometheus_demo.sh | bash
cd prometheus-demo
# Push first, wait for scrape, then query
uv run dg launch --assets '+orders_metrics_pushed'
sleep 8
uv run dg launch --assets '+revenue_report'
```

## Backends

The same components work against ANY Prometheus-compatible HTTP API:

- **Open-source Prometheus** (this demo)
- **Cortex / Thanos / Mimir** — pass `extra_headers: {X-Scope-OrgID: tenant1}`
- **VictoriaMetrics** — drop-in
- **Azure Managed Prometheus** — `bearer_token_env_var` from `az account
  get-access-token --resource https://prometheus.monitor.azure.com`
- **Grafana Cloud** — Bearer token from Grafana Cloud API key
- **AWS Managed Prometheus** — needs sigv4 sidecar/proxy

## See also

<!-- TODO: link related walkthroughs -->
