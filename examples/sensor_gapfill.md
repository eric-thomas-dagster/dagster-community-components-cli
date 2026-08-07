# IoT sensor gap-fill demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

cumulative metric on top.

synthetic_data_generator's `sparse_sensors` schema generates 3 sensors
x 14 days of hourly readings with ~25% rows dropped to simulate flaky
devices. ts_filler resamples each sensor to a regular hourly grid and
forward-fills the gaps; running_total then computes a cumulative
average across the cleaned series.

Pipeline (4 components, all autoloaded by `dg`):
    synthetic_data_generator → ts_filler → running_total → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate synthetic data |
| 2 | `ts_filler` | transformation | Fill time-series gaps |
| 3 | `running_total` | transformation | Cumulative aggregate |
| 4 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sensor_gapfill_demo.sh | bash
cd sensor-gapfill-demo
uv run dg launch --assets '*'
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
