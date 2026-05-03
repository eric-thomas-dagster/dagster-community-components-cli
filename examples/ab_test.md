# A/B test stats demo


synthetic_data_generator (schema_type: ab_experiment) produces 5000
control/treatment exposure rows where treatment converts 30% better.
ab_test_analysis runs a stat test and reports lift, p-value, sample
size, etc. Output is one row per variant.

Pipeline (3 components, all autoloaded by `dg`):
    synthetic_data_generator → ab_test_analysis → dataframe_to_csv

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ab_test_demo.sh | bash
cd ab-test-demo
uv run dg launch --assets '*'
```
