"""Airflow schedule=[a, b] (AND) vs Dagster conditions. Which Dagster condition fires only after BOTH upstreams updated?"""
import warnings; warnings.filterwarnings("ignore")
import dagster as dg

AC = dg.AutomationCondition
AND = (AC.asset_matches(dg.AssetKey("a"), AC.newly_updated()).since_last_handled()
       & AC.asset_matches(dg.AssetKey("b"), AC.newly_updated()).since_last_handled()
       & ~AC.any_deps_missing() & ~AC.any_deps_in_progress() & ~AC.in_progress())

@dg.asset
def a(): return 1
@dg.asset
def b(): return 2
@dg.asset(deps=[a, b], automation_condition=AND)
def c_and(): return 3
@dg.asset(deps=[a, b], automation_condition=AC.eager())
def c_eager(): return 3

def run(label, cond_asset):
    defs = dg.Definitions(assets=[a, b, cond_asset])
    inst = dg.DagsterInstance.ephemeral()
    cur = None; out = []
    def tick(why):
        nonlocal cur
        r = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=cur); cur = r.cursor
        out.append((why, r.get_num_requested(cond_asset.key)))
    tick("nothing materialized")
    dg.materialize([a], instance=inst); tick("a updated")
    dg.materialize([b], instance=inst); tick("a and b updated (first time)")
    dg.materialize([cond_asset], instance=inst, selection=None) if False else dg.materialize([a, b, cond_asset], instance=inst) if False else None
    dg.materialize([cond_asset], instance=inst); tick("consumer ran")
    dg.materialize([a], instance=inst); tick("a updated again")
    dg.materialize([b], instance=inst); tick("b updated again (now both since the last run)")
    print(f"{label:8}", out)

run("AND", c_and); run("eager", c_eager)
