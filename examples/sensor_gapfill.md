# IoT sensor gap-fill demo

cumulative metric on top.

synthetic_data_generator's `sparse_sensors` schema generates 3 sensors
x 14 days of hourly readings with ~25% rows dropped to simulate flaky
devices. ts_filler resamples each sensor to a regular hourly grid and
forward-fills the gaps; running_total then computes a cumulative
average across the cleaned series.

Pipeline (4 components, all autoloaded by `dg`):
    synthetic_data_generator → ts_filler → running_total → dataframe_to_csv

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sensor_gapfill_demo.sh | bash
cd sensor-gapfill-demo
uv run dg launch --assets '*'
```
