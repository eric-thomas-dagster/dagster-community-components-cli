# Skill-level evals

A small fixture set and pass criteria for the converter's output, run by `scripts/run_evals.py` (and by `scripts/tests/test_evals.py`).

| Case | What it checks |
|---|---|
| `clean_python_dag` | a plain scheduled DAG converts; the generated project loads (`dg check defs`, or dagster's own loader when `dg` is not installed); the generated pytest passes; no invented component type; no secret value |
| `clean_python_dag_task` | the same with the fallback `--granularity task` (one asset per task); `clean_python_dag` runs the default, product granularity |
| `fake_secrets_in_arguments` | operator arguments named `password` and `api_token` carry planted fake values: they must not appear in any generated file or in the report |

Criteria: `converted`, `definitions_load`, `generated_tests` (a stated skip passes, a failure does not), `no_invented_types` (every component `type:` resolves), `no_secret_values` (the planted strings and any credential-looking literal). Add a case by adding an IR to this folder and an entry to `cases.json`. The rewrite route's equivalent gates are in `scripts/accept_rewrite.py`.

```bash
python3 scripts/run_evals.py          # nonzero exit if any criterion fails
```

Limits: fixtures only (no Airflow), three cases, no operator beyond Python tasks: this guards the pipeline's structure and its refusal to leak, not its coverage (see `reference/conversion.md`).
