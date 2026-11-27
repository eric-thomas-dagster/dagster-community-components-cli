"""Pattern: ShortCircuitOperator / @task.short_circuit. NATIVE (preferred): an optional-output asset or op, below. FALLBACK: an early `return` inside the asset that holds the downstream work (plain Python), when nothing below depends on skip semantics, the native form is not verified for the case, or it failed a gate.

Airflow: the short-circuit task itself ends `success`; its downstream tasks end `skipped` (with ignore_downstream_trigger_rules=True all of them; with False only the direct ones and the
trigger rules of the rest still apply). Dagster: the condition and the work it guards live in one asset; when the condition is falsy the function returns before the work. Tasks that
follow the short circuit and share the asset are `folded into <asset>: skipped by the short circuit`. A downstream task that must run anyway (trigger rule all_done / none_failed) goes
AFTER the guarded work in the same function, outside the `if`.
"""
import dagster as dg


def condition() -> bool:
    return False


@dg.asset
def guarded(context: dg.AssetExecutionContext) -> dict:
    out: dict = {"short_circuit": "success", "downstream_ran": []}     # the short-circuit task itself succeeded
    if condition():
        out["downstream_ran"] += ["task_a", "task_b"]                   # the guarded downstream work
    else:
        context.log.info("short circuit: downstream tasks task_a, task_b are not executed")
    out["downstream_ran"].append("all_done_task")                       # trigger_rule all_done (or ignore_downstream_trigger_rules=False): still runs
    return out


# --- DECISION + MARKER form (PREFERRED native form when the short-circuit task is its own Airflow task): the task ENDS `success`, only what is below it is skipped ------------------
# A bare optional-output asset (next section) is not materialized when the condition is false, so the short-circuit task itself reads as `skipped` (Airflow: `success`). Keep the task's asset
# ALWAYS yielded and put a skippable marker next to it in the same multi_asset; the guarded tasks depend on the MARKER. Verified (tests/test_rewrite_patterns.py): condition false -> `sc_task`
# materialized, `sc_go` not, `below_marker` STEP_SKIPPED; the run succeeds; an `all_done` / `none_failed` task that depends on `sc_task` (not on the marker) still runs.
SC = {"v": False}


@dg.multi_asset(specs=[dg.AssetSpec("sc_task"), dg.AssetSpec("sc_go", skippable=True, deps=["sc_task"])], name="short_circuit_task")
def short_circuit_task():
    yield dg.MaterializeResult(asset_key="sc_task")                    # the short-circuit task itself: always success
    if SC["v"]:
        yield dg.MaterializeResult(asset_key="sc_go")                  # not yielded when the condition is false: everything that depends on sc_go is skipped


@dg.asset(deps=["sc_go"])
def below_marker() -> None:
    """a guarded downstream task: skipped when the condition is false"""


@dg.asset(deps=["sc_task"])
def all_done_after_short_circuit() -> None:
    """trigger_rule all_done / none_failed: depends on the task, not the marker, so it runs whatever the condition (its ordering after the skipped tasks cannot be an edge: a skipped upstream skips it)"""


# --- optional-output form (Airflow's skipped state reproduced in the same run; use ONLY when the short-circuit task has no state of its own to report, e.g. it is folded into the first guarded asset) -----------------------------------
# Verified on the installed Dagster: `@dg.asset(output_required=False)` that RETURNS before yielding (a generator) is not materialized and every asset below it is skipped; the run succeeds.
# A bare `return` with no `yield` in a non-generator function does NOT skip: the None return value counts as the output and the downstream assets run.
CONDITION = {"v": False}


@dg.asset(output_required=False)
def short_circuit_asset():
    if not CONDITION["v"]:
        return                                                          # nothing yielded: not materialized, the assets below are skipped
    yield dg.Output("go")


@dg.asset
def below_short_circuit(short_circuit_asset: str) -> str:
    return short_circuit_asset + "!"


@dg.op(out=dg.Out(is_required=False))
def short_circuit_op():
    if not CONDITION["v"]:
        return
    yield dg.Output("go")


@dg.op
def guarded_op(x: str) -> str:
    return x


@dg.graph_asset
def guarded_graph() -> str:
    return guarded_op(short_circuit_op())                               # skipped, with its asset, when the condition is false


defs = dg.Definitions(assets=[guarded, short_circuit_asset, below_short_circuit, guarded_graph, short_circuit_task, below_marker, all_done_after_short_circuit])
