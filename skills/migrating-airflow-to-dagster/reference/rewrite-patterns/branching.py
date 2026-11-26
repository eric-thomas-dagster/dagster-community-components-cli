"""NATIVE form of branching (PREFERRED): OPTIONAL OUTPUTS. The branches have distinct downstream assets / ops, retries, resources or checks, or the Airflow `skipped` state should be reproduced in the same run;
plain Python in one asset (`branching_plain_fallback.py`) is the FALLBACK for when the branches only differ in logic, when this form is not verified for the case, or when it fails a gate.

Verified on the installed Dagster (run by tests/test_rewrite_patterns.py):
  * an output with `is_required=False` that is not yielded is not materialized; every step below it is SKIPPED (STEP_SKIPPED); the run still SUCCEEDS.
  * `can_subset=True` is NOT needed (the same result with and without it, also when the job selects every asset); it is launch-time selection, not a runtime decision. Do not add it.
  * a downstream asset with two inputs from two optional branches needs BOTH (all_success): one skipped branch skips it. That is NOT Airflow's none_failed_min_one_success.
  * REJOIN in the same run: a `graph_asset` whose op takes the branch results as a fan-in `list` runs with whichever branch was yielded (`rejoined` below); with no branch yielded it is skipped,
    as Airflow skips a none_failed_min_one_success join when nothing succeeded.
  * REJOIN across runs: separate top-level assets and `AutomationCondition.any_deps_updated()` on the join request it after any one of them ran. That is a later run, not the same run.
"""
import dagster as dg

CHOSEN = {"v": "a"}                                                    # what the branch callable returns: "a", "b", "both" or "none" (the tests change it)


def picks(name: str) -> bool:
    return CHOSEN["v"] in (name, "both")


# --- (a) branches that produce different assets: multi_asset with optional outs --------------------------------------------------------------------
@dg.multi_asset(outs={"asset_a": dg.AssetOut(is_required=False), "asset_b": dg.AssetOut(is_required=False)}, name="branch_assets")
def branch_assets():
    if picks("a"):
        yield dg.Output("value-a", output_name="asset_a")
    if picks("b"):
        yield dg.Output("value-b", output_name="asset_b")


@dg.asset
def under_a(asset_a: str) -> str:
    return asset_a + "+"                                                # skipped (not run) when branch a was not taken


@dg.asset
def under_b(asset_b: str) -> str:
    return asset_b + "+"


@dg.asset
def needs_both(asset_a: str, asset_b: str) -> str:
    return "joined"                                                     # all_success: skipped when EITHER branch was skipped (not Airflow's none_failed_min_one_success)


# --- (b) branch then rejoin to ONE product, same run: graph_asset with a fan-in --------------------------------------------------------------------
@dg.op(out={"a": dg.Out(is_required=False), "b": dg.Out(is_required=False)})
def decide():
    if picks("a"):
        yield dg.Output("x", output_name="a")
    if picks("b"):
        yield dg.Output("y", output_name="b")


@dg.op
def do_a(v: str) -> str:
    return v + "-A"


@dg.op
def do_b(v: str) -> str:
    return v + "-B"


@dg.op
def join(results: list) -> list:
    return sorted(results)                                              # whichever branch ran


@dg.graph_asset
def rejoined() -> list:
    d = decide()
    return join([do_a(d.a), do_b(d.b)])                                 # the list form is the fan-in; `join(do_a(d.a), do_b(d.b))` would be all_success


# --- (c) rejoin across runs: separate assets + an automation condition ------------------------------------------------------------------------------
@dg.asset
def top_a() -> int:
    return 1


@dg.asset
def top_b() -> int:
    return 2


@dg.asset(deps=["top_a", "top_b"], automation_condition=dg.AutomationCondition.any_deps_updated())
def join_after_any() -> int:
    return 3


defs = dg.Definitions(assets=[branch_assets, under_a, under_b, needs_both, rejoined, top_a, top_b, join_after_any])
