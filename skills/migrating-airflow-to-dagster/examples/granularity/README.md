# Worked example: task granularity versus product granularity

`s_basic.task.py` and `s_basic.product.py` are the converter's output for the same Airflow DAG (`examples/airflow_release_matrix/suite/s_basic.py`: a Bash task `a` that echoes, then a Python task `b`), made with `--granularity task` and `--granularity product`.

| | task (default) | product (opt-in) |
|---|---|---|
| assets | 2 (`s_basic/a`, `s_basic/b`) | 1 (`s_basic/b`) |
| job selection | both keys | `s_basic/b` |
| `a` | its own asset, with `deps` from `b` | `_v_a = run_shell(...)  # folded from Airflow task 'a'`, plain Python at the top of `b`'s asset |
| restart | `a` and `b` are separate steps | the asset restarts from the beginning: `a` runs again with `b` |
| report | `task_keys` has one key per task | `folded: {a: b}`, `task_keys` maps `a` to `s_basic/b`, and `granularity.decisions` says why: `a` is a step ("its value feeds only b and nothing persists it or needs its own restart point"), `b` is a product ("no downstream task consumes it") |

`diff s_basic.task.py s_basic.product.py` is the whole change.

Measured on the 11 quick DAGs of `examples/airflow_release_matrix/suite` (the other 12 converted DAGs include long sensor waits and were left out), with the parity harness (`scripts/parity/parity_af.py` on Airflow 2.10.5, `parity_dg.py` and `compare.py` for each granularity): **both granularities give 10 identical outcomes and 1 difference, the same one** (`s_execution_date` prints a timestamp, which cannot match). Three of the 11 DAGs had a step folded (`s_basic`, `s_taskflow`, `s_legacy_imports`). That is too little to make product the default: the corpus was not re-run, and only 3 folds were exercised, so product stays opt-in.
