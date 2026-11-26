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


# --- optional-output form (Airflow's skipped state reproduced in the same run; use when assets below depend on the skip semantics) -----------------------------------
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


defs = dg.Definitions(assets=[guarded, short_circuit_asset, below_short_circuit, guarded_graph])
