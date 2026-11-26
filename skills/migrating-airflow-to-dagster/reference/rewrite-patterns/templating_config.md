# Templates, params, Variables, connections

Use the converter's translation set (ds, ds_nodash, ts, logical_date, data_interval_start/end, ds_add, ds_format) and `templating_config.py` (all of it executed): `{{ ds }}` / `data_interval_*` -> `partition_key` /
`partition_time_window` of a time-partitioned asset (add a `DailyPartitionsDefinition` when the interval matters; an unpartitioned asset has only the run time, which is NOT the data interval: say so);
`{{ params.x }}` -> a `dg.Config` field; `{{ var.value.x }}` -> an environment variable by name (`EnvVar`); connections -> a `ConfigurableResource` with `EnvVar` fields injected as an argument, mockable in tests.
Header: `<task>: replaced by asset `<key>`` plus which translation you used. Decision record (templating / connections): `preserved` or `not preserved: <why>` (for example `the interval is the run time`).
Limits: partitioned assets change how the job is launched (one partition per run); a Jinja expression the table does not cover is `not preserved`, not guessed.
