# Sensors with timeout, soft_fail, mode

Airflow: poke every `poke_interval` until true; after `timeout` the sensor FAILS, or is SKIPPED with `soft_fail=True`; `mode='reschedule'` frees the worker between pokes.

**Native (anything but a short wait): never `time.sleep` in an asset.** The wait is a Dagster sensor that evaluates the condition and emits `RunRequest` (`condition_sensor` in `sensors.py`). For an async external
job in the middle of a pipeline: asset A submits and returns the job id, a sensor polls completion and requests the run holding asset B (`submit_job`, `job_done_sensor`, `after_job`); Dagster Pipes is the other
native route for a job that reports back while it runs. The wait is then outside every run: there is no timeout state, `reschedule` is the closest of Airflow's modes, and it is said so in the decision record
(`sensor modes`). Reuse the shape the converter already emits for Airflow sensors.

**Fallback (short waits whose follow-on work must be in the same run): `poll_until(check, timeout, interval)` inside the asset** (`wait_then_work`; `<sensor>: folded into `<asset>`: poke loop`). On timeout raise
`dg.Failure` (Airflow: failed); with `soft_fail` record `airflow/state: skipped` and return the SKIPPED sentinel (`skip.py`). It holds the worker like `mode='poke'`; record `fallback to plain Python: <reason>`.

Tested: the sensors return `SkipReason` / `RunRequest` correctly, the poll loop (fake clock), timeout failure, soft_fail skip, and that the sensor returns `SkipReason` / `RunRequest` correctly. Not run against a real external condition.
