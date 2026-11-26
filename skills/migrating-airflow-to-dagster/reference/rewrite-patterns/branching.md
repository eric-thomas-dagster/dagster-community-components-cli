# Branching (BranchPythonOperator, `@task.branch`)

**Native forms first (`branching.py`, verified by running); plain Python in one asset (`branching_plain_fallback.py`) is the stated fallback.**

1. **Branches with different downstream assets, ops, retries, resources or checks**: `@multi_asset(outs={...: AssetOut(is_required=False)})` yielding only the chosen `Output(..., output_name=...)`, or an op
   with `Out(is_required=False)` per branch. The branches not taken are not materialized and every step below them is skipped; the run succeeds, so Airflow's `skipped` state IS expressed. `can_subset` is not needed.
2. **Branch then rejoin into one product**: a `@graph_asset` whose branch op has `Out(is_required=False)` per branch and whose join op takes the results as a fan-in `list` (`join([do_a(d.a), do_b(d.b)])`): it
   runs when AT LEAST ONE branch yielded: its list input holds only the outputs of the branches that yielded (verified: `['x-A']`, `['y-B']`, or both), and it is SKIPPED (not failed, the run still succeeds) when no branch yielded, as Airflow skips a none_failed_min_one_success join with no success upstream. A join written as `join(do_a(d.a), do_b(d.b))`, or a regular asset with two upstream arguments, is
   all_success: skipped when either branch is skipped, NOT none_failed_min_one_success. Separate top-level assets joined by `AutomationCondition.any_deps_updated()` run the join in a later run, not the same run:
   an approximation, say so.
**Join below a skipped upstream: which form works for which shape (each run and asserted).** Dagster skips a step when any upstream output it depends on was skipped; that is all_success and has no switch.
| Shape | Result | Use |
|---|---|---|
| join is a separate top-level asset, branch is a top-level optional-output asset, or an output of a multi_asset (input or `deps`), or a skipped graph_asset | join SKIPPED if ANY upstream skipped | not a none_failed / all_done join; fine for all_success |
| join is a `graph_asset` that takes the branch assets as inputs, wraps each in a pass-through op and fans in over a `list` (`join_graph`) | join RUNS with the branches that yielded; skipped when none | **the native form for a rejoin when branch and join are separate assets** |
| branch and join in the same `graph_asset` (`rejoined`) | join RUNS with the branches that yielded; skipped when none | native, when the branches need no asset of their own |
| always-yielded marker output next to the optional ones, join on the marker (`join_on_marker`) | join RUNS whatever was skipped (also when none yielded: all_done-like); what is below a branch output is skipped | when the join must run regardless |
| sentinel + metadata `airflow/state: skipped` on a materialized asset (`skip.py`) | join runs; the skipped state is metadata only | the honest fallback when a state must be visible and the join must run |
| `Nothing`-typed inputs or `deps` | join SKIPPED, same as an input | no help |
| separate top-level assets + `AutomationCondition.any_deps_updated()` | join requested in a LATER run | approximation |

3. **A list known only at run time**: `DynamicOut` + `.map()` (`mapped_tasks.py`).
4. **FALLBACK, plain Python in one asset** (`branching_plain_fallback.py`): only when (a) the branches produce the same output and differ only in logic, (b) the native form is not verified for this case, or
   (c) it failed a gate. The branch callable and the chosen branch's work live in one asset; the join is ordinary code after the `if`.

Product granularity still applies (one asset per data product, not per task) but is not a reason to fold a real branch into plain Python.

## Header lines (one line per Airflow task)

Native form: `<task>: replaced by asset `<key>`` (or `replaced by op `<name>` in asset `<key>`` for ops inside a graph asset); the harness compares the task's skipped/success state with Airflow's.
Fallback form:
```
<branch_task>: folded into `<asset>`
<taken_task>: folded into `<asset>`
<not_taken_task>: folded into `<asset>`: branch not taken (Airflow marks it skipped; here it is not executed and has no state of its own)
```
Decision record: native -> `- skip/branch: preserved` (state and effects). Fallback -> `- skip/branch: fallback to plain Python: <reason>` (effects preserved, Airflow's skipped state not expressed). Both are
counted in the evaluation report.

## What is lost (decision record)

In the fallback form: the skipped state of the not-taken branch; per-branch retries and visibility (one step); a failed branch restarts from the start of the asset. The native forms keep them.
