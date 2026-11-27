# ShortCircuit (`ShortCircuitOperator`, `@task.short_circuit`)

**Native (preferred) when the short-circuit task is an Airflow task of its own: the task's asset is ALWAYS yielded and a skippable marker sits next to it in one `multi_asset`** (`short_circuit.py: short_circuit_task`, verified by running). Airflow ends the short-circuit task `success` and skips what is below; a bare optional-output asset (next paragraph) is not materialized when the condition is false, so the task itself reads `skipped`, which is wrong. The guarded tasks use `deps=["<marker>"]`; an `all_done` / `none_failed` task uses `deps=["<task asset>"]` and still runs (it cannot also depend on the skipped tasks: any skipped upstream skips it, so say that its ordering after them is lost).
```
<short_circuit_task>: replaced by asset `<task asset>` (success: always materialized; a skippable marker `<marker>` in the same multi_asset gates the tasks below)
<guarded_task>: replaced by asset `<key>` (skipped when the condition is false: it depends on the marker)
```

**Optional-output asset or op** (use only when the short-circuit task has no state of its own to report, e.g. folded into the first guarded asset; `short_circuit.py`, verified by running): a generator with `output_required=False` (`Out(is_required=False)` for an op inside a graph asset) that returns
before yielding when the condition is false; everything below is skipped (not materialized, STEP_SKIPPED) and the run succeeds. With this form the asset of the short-circuit task is the one not materialized (it reads `skipped`). Exactly: the function must be a GENERATOR (contain a `yield`) declared with `output_required=False` (the real parameter name on the installed `@dg.asset`; `Out(is_required=False)` for an op) and `return` before yielding. In a NON-generator a bare `return` AND `return None` both emit None as the output: the asset materializes and downstream runs (tested for both). With `ignore_downstream_trigger_rules=False` the tasks that must
run anyway (`all_done` / `none_failed`) need an input that is not the optional output (a `deps` on an always-materialized asset) or a plain-Python tail.

```
<short_circuit_task>: replaced by asset `<key>` (success: the condition part runs)
<guarded_task>: replaced by asset `<key>` (skipped when the condition is false: its output is not yielded)
```

**Fallback, plain Python: an early `return`** in the asset holding the guarded work, only when nothing below depends on skip semantics, the native form is not verified for the case, or it failed a gate:
```
<short_circuit_task>: folded into `<asset>`
<guarded_task>: folded into `<asset>`: skipped by the short circuit when the condition is false (not executed; no state of its own)
```
Decision record (skip/branch): native `preserved`; fallback `fallback to plain Python: <reason>`. Never mark the short-circuit task itself skipped.
