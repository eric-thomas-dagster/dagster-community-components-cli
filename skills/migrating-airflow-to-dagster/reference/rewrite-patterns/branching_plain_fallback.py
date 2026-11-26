"""FALLBACK pattern: Airflow BranchPythonOperator / @task.branch -> plain Python inside ONE asset. Use it only when (a) the branches produce the same output and differ only in logic, (b) the native optional-output form
(`branching.py`) is not verified to work for this case, or (c) it failed a gate; then the decision record says `fallback to plain Python: <reason>`.

Dagster fixes the run's shape before it starts; a branch is a decision made while the run is going, so it is an `if` inside the asset: the branch callable and the chosen branch's work live
together, and the branches that are not taken are simply not executed. Airflow's `skipped` state for a not-taken task has no Dagster counterpart; the header says so (see branching.md).
Everything after the branches (a join with trigger rule none_failed_min_one_success, or any downstream) is ordinary code after the `if`: it runs for whichever branch was taken.
"""
import dagster as dg


def pick_branch(config: dict) -> list[str]:
    """The branch callable, unchanged: returns the task id(s) to follow ([] follows none)."""
    return ["branch_a"] if config.get("use_a", True) else ["branch_b"]


def work_a() -> str:
    return "a-result"


def work_b() -> str:
    return "b-result"


@dg.asset
def branching_asset(context: dg.AssetExecutionContext) -> dict:
    chosen = pick_branch({"use_a": True})
    out: dict = {"chosen": chosen, "ran": []}
    if "branch_a" in chosen:
        out["branch_a"] = work_a()
        out["ran"].append("branch_a")
    if "branch_b" in chosen:
        out["branch_b"] = work_b()
        out["ran"].append("branch_b")
    context.log.info(f"branch taken: {chosen}; not taken: {sorted({'branch_a', 'branch_b'} - set(chosen))}")
    out["join"] = f"joined after {out['ran']}"                    # the join task: runs for whichever branch was taken
    return out


defs = dg.Definitions(assets=[branching_asset])
