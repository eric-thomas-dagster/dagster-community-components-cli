# Partition window parity: `partition_window_sensor` against Airflow's own mappers

Checks the keys the community `partition_window_sensor` expects (rollup) or produces (fan out) against what Airflow's `RollupMapper` and `FanOutMapper` return for the same key.

- `af_side.py` (Airflow 3.3+ environment): the upstream keys a daily and a monthly rollup needs, and the downstream keys a forward and a backward weekly fan-out produces, for four dates that include a short month, a leap year and a year boundary. Writes `af_out.json`.
- `dg_side.py` (environment with `dagster-community-components`): the sensor's own window functions for the same cases; prints any key set that differs.

Result (components 0.10.162): 12 of 12 cases identical as key sets. Not checked: the sensor's behavior over time (the cursor, `wait_policy: minimum_count`, a late upstream partition) and `window_type: segment`.
