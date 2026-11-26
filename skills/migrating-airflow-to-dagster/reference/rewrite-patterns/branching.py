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


# --- (b2) JOIN BELOW A SKIPPED UPSTREAM, by shape (each run and asserted in tests/test_rewrite_patterns.py) ------------------------------------------------------
# The branch outputs above (`asset_a`, `asset_b`) are TOP-LEVEL assets (outputs of a multi_asset). A separate top-level asset below them, whether it takes them as inputs (`needs_both`) or as deps, is SKIPPED
# when ANY of them is skipped (Dagster's all_success): that cannot be turned into all_done / none_failed_min_one_success. What works instead:
#   1. make the JOIN itself a graph_asset that takes the branch assets as graph inputs and wraps each in a pass-through op; the fan-in `list` then holds only the branches that yielded (`join_graph`).
#      Verified with a top-level skipped input: the pass-through op of the skipped input is skipped, the fan-in join still runs. The branch and the join stay separate assets.
#   2. branch and join in the SAME graph_asset (`rejoined`, above).
#   3. an always-yielded MARKER output next to the optional ones, and the join depends on the marker (`join_on_marker`): the join runs whatever was skipped (all_done-like: it also runs when NO branch yielded).
#   4. a sentinel + metadata `airflow/state: skipped` on a materialized asset (skip.py): the join takes it as an ordinary input and runs; the skipped state is only metadata.
#   5. `Nothing`-typed op inputs or `deps` do NOT help: the join is skipped just the same (verified).
@dg.op
def passthru(x):
    return x


@dg.op
def join_results(results: list) -> list:
    return sorted(results)


@dg.graph_asset
def join_graph(asset_a: str, asset_b: str) -> list:
    return join_results([passthru(asset_a), passthru(asset_b)])           # runs with whichever branch asset was materialized; skipped when none was


@dg.multi_asset(specs=[dg.AssetSpec("marker_a", skippable=True), dg.AssetSpec("marker_b", skippable=True), dg.AssetSpec("branches_done")], name="marker_branches")
def marker_branches():
    if picks("a"):
        yield dg.MaterializeResult(asset_key="marker_a")
    if picks("b"):
        yield dg.MaterializeResult(asset_key="marker_b")
    yield dg.MaterializeResult(asset_key="branches_done")                  # always yielded


@dg.asset(deps=["branches_done"])
def join_on_marker() -> str:
    return "ran whichever branch was skipped"


@dg.asset(deps=["marker_b"])
def below_marker_b() -> str:
    return "skipped when marker_b was not yielded"


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


defs = dg.Definitions(assets=[branch_assets, under_a, under_b, needs_both, rejoined, join_graph, marker_branches, join_on_marker, below_marker_b, top_a, top_b, join_after_any])
