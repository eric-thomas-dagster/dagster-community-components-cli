# A task that skips itself (AirflowSkipException, exit code 99, `skip_on_exit_code`)

**Native: its own asset with an optional output** (a generator that returns before yielding: not materialized, everything below skipped; `short_circuit.py` shows the shape), or `airflow/state` metadata when the skip must stay visible as a materialization. **Fallback: inside the asset, `if <skip condition>: return` before the work** (a folded task: `<task>: folded into `<asset>`: skipped when <condition>`; record `fallback to plain Python: <reason>`). Airflow's BashOperator exits skip
on code 99 (default `skip_on_exit_code`) and fail on any other non-zero code: reproduce both (`skip.py`).

When the skipping task is its OWN asset and others consume it, `skip.py` shows the closest honest form: materialize with metadata `airflow/state: skipped`, return the SKIPPED sentinel, let an
`all_success` consumer propagate it and an `all_done` / `none_failed` consumer ignore it. Do not yield nothing from an ordinary asset (that is a failure: a required output is missing).

Header sentence for that form: `<task>: replaced by asset `<key>`; Airflow's skipped state is recorded as metadata airflow/state=skipped (Dagster has no skipped asset state)`.
Decision record: `skip: preserved as metadata, not as a Dagster state` (or `not preserved: ...` when folded).
