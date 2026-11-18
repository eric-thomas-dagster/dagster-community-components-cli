# Mechanical conversion from the runtime IR

`converted_2_9/` is the output of the pipeline for the public repo `astronomer/2-9-example-dags` (8 of its 18 DAGs convert; the other 10 are deferred with a reason each in `report.json`). It loads in a Dagster project created with `create-dagster`; add `dagster-aws` and `dagster-snowflake` to the project if it uses resources.

```
# in an environment with the same Airflow version and providers as the project (this EXECUTES the DAG files: sandbox only)
python3 scripts/airflow_ir.py <dags folder> --out ir.json --pythonpath <project root>
python3 scripts/convert_ir.py ir.json --out-dir <dagster project>/src/<pkg>/defs --report report.json
python3 scripts/apply_conversion.py manifest.json ir.json report.json      # plans for converted DAGs, deferrals for the rest
python3 scripts/validate_defs.py <dagster project> --manifest manifest.json
```

`parity/` is the harness that decides whether converted DAGs behave like the originals: `parity_af.py` runs each DAG with Airflow's own `dag.test()` in a sandbox and records every task's state and returned value; `parity_dg.py` materializes the converted assets; `compare_parity_all.py` compares them. `shim_*.py` checks the `S3Hook` compatibility shim against Airflow's real hook with a recording fake boto client; `and_trigger.py` checks the multi-asset AND trigger with `dg.evaluate_automation_conditions`. The scripts use the directory layout of the session that produced them; adjust the paths.

The parity harness found a converter bug the load check could not: Airflow 2's decorated mapped tasks are `DecoratedMappedOperator`, a subclass of `MappedOperator`, so the IR missed that they were mapped and the converter produced a task that failed at run time. See `reference/conversion.md`.
