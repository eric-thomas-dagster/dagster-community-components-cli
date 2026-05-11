# A/B Full Pipeline demo

An end-to-end experimentation pipeline:
  1. eligible_users (synthetic) → ab_treatments (deterministic split)
  2. exposure_events (synthetic) → ab_test_analysis  (significance verdict)
                                 → ab_trend          (daily conv-rate trend)
                                 → ab_controls       (sizing for next experiment)

Pipeline (10 components, all autoloaded by `dg`):

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | Generate synthetic data |
| 2 | [`ab_treatments`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_treatments) | analytics | Assign variants by hash |
| 3 | [`ab_test_analysis`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_test_analysis) | analytics | Stat-test verdict |
| 4 | [`ab_trend`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_trend) | analytics | Daily conversion trend |
| 5 | [`ab_controls`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_controls) | analytics | Sample size for next experiment |
| 6 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ab_full_pipeline_demo.sh | bash
cd ab-full-pipeline-demo
uv run dg launch --assets '*'
```
