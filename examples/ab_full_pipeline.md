# A/B Full Pipeline demo


An end-to-end experimentation pipeline:
  1. eligible_users (synthetic) → ab_treatments (deterministic split)
  2. exposure_events (synthetic) → ab_test_analysis  (significance verdict)
                                 → ab_trend          (daily conv-rate trend)
                                 → ab_controls       (sizing for next experiment)

Pipeline (10 components, all autoloaded by `dg`):

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ab_full_pipeline_demo.sh | bash
cd ab-full-pipeline-demo
uv run dg launch --assets '*'
```
