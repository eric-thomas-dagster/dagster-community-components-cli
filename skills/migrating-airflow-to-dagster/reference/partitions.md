# Partitioned timetables (Airflow 3.2 and later)

`CronPartitionTimetable` makes each scheduled run work on one time partition. `PartitionedAssetTimetable` starts a DAG when partitions of upstream assets arrive, and a partition mapper says which downstream partition each upstream partition belongs to. The converter reads Airflow's own structured form of the timetable and the mappers (`encode_timetable`), so nothing is scraped from source text.

## What converts

| Airflow | Dagster | Notes |
|---|---|---|
| `CronPartitionTimetable("0 * * * *")` (hourly, daily, weekly and monthly cadences; no `run_offset`) | The DAG's assets are partitioned with Dagster's hourly, daily, weekly or monthly partitions, and a schedule at the cron time requests the partition whose window contains the tick | A run at 10:15 works on hour 10, as in Airflow |
| `PartitionedAssetTimetable` with `IdentityMapper` or `StartOfHour/Day/Week/MonthMapper` | The assets use the partitions the mapper names, and update when the upstream partition does (an automation condition per partition; several upstream assets are combined with the per-asset AND used for asset-triggered DAGs) | `StartOfHourMapper` over producers that fire at `:00`, `:15` and `:30` lines them up on the hour |
| `RollupMapper` with `DayWindow`, `WeekWindow` or `MonthWindow`, `WaitForAll` or `MinimumCount(n)` | A registry `partition_window_sensor` (`mode: rollup`) requests one run for the rolled-up partition once the window is complete | Day windows need hourly upstream partitions, week and month windows daily ones |
| `FanOutMapper` with `DayWindow` or `WeekWindow`, forward or backward | A `partition_window_sensor` (`mode: fan_out`) requests one run per downstream partition when an upstream partition materializes | The upstream key must already be the start of the window |
| `dag_run.partition_key`, `dag_run.partition_date`, `context["partition_key"]` | The Dagster partition key (Dagster's format) and the start of the partition window | |

## Differences to state

- **Keys are Dagster's own.** Hourly keys look like `2026-03-10-10:00`, the others `2026-03-10`, not the `key_format` of the Airflow timetable or the mapper's `output_format`. Code that parses or compares keys must change.
- **Partitions have a start date.** Airflow's have none; the converter uses `--partition-start` (default `2025-01-01`) and says so in each module's note.
- **Partition mappers become Dagster partition relationships**, not per-event mapping: an hourly asset consumed through `StartOfDayMapper` becomes a daily asset that updates whenever one of its hours does.
- The sensor-driven DAGs (rollup, fan out) depend on the `dagster-community-components` package, which the report lists in the requirements.

## What is not converted yet

`RollupMapper` and `FanOutMapper` over categorical segments (`SegmentWindow`, `FixedKeyMapper`), `ProductMapper`, `AllowedKeyMapper`, a mapper per upstream asset, `HourWindow`, `QuarterWindow`, `YearWindow` and `StartOfYear/Quarter` mappers, and `PartitionedAtRuntime` (a task that decides its own partition keys with `outlet_events[...].add_partitions`). Each is deferred with a named reason.

## How it is checked

`scripts/tests/test_convert_ir.py` converts producers and consumers and checks: the producer's tick requests the right partition; a consumer over three producers runs only after all three materialized the hour; a weekly cron starts on its weekday; `dag_run.partition_key` is the Dagster partition; a daily rollup waits for all 24 hours and then requests one run for the day; a weekly fan-out requests the seven days. The sensor's window keys were also compared with Airflow's own mappers (`examples/runtime_conversion/partition_window_parity/`, 12 of 12 identical).
