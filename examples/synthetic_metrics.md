# Synthetic time-series — registry-native generator

A 3-component pipeline that generates 30 days of synthetic hourly metrics
via `time_series_generator` (no external data, no upstream — pure
in-component generation), runs anomaly detection on the result, writes a
CSV. Demonstrates that you don't need an inline Python heredoc for
synthetic data: the registry has a real component for it.

## Pipeline

```
time_series_generator → anomaly_detection → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `time_series_generator` | analytics | Generate `cpu_pct` from scratch (`pattern_type: complex`, hourly, 30 days, base 100, noise 0.15, seeded) |
| 2 | `anomaly_detection` | analytics | z-score, threshold 2.5σ |
| 3 | `dataframe_to_csv` | sink | Write timestamp + metric + anomaly flag |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_synthetic_metrics_demo.sh | bash
cd synthetic-metrics-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/synthetic_metrics_anomalies.csv` — 721 hourly points (30 days +
the start-of-day-31 boundary) with `cpu_pct`, `is_anomaly`,
`anomaly_score`. Roughly 1-3% of points should fire as anomalies at
threshold 2.5σ.

## What this demo shows

- **First demo with no upstream ingestion** — `time_series_generator`
  is the source. Useful for testing downstream pipelines, generating
  load-test fixtures, or seeding a Dagster job graph for
  asset-materialization timing experiments.
- **`pattern_type` knob** — `trend`, `seasonal`, `random_walk`,
  `sine_wave`, `step_function`, `spike`, `complex`. Pick the shape;
  combine `noise_level` and `random_seed` for realism.
- **The right way to do synthetic data.** The SaaS-metrics + revenue-
  attribution demos use inline Python heredocs because they need
  Stripe-shaped CSVs that `time_series_generator` doesn't produce. For
  pure time-series, this is the registry-native pattern.
