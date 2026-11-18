"""My af_gate (runtime.py) against the registry's af_guard, over every combination of up to 3 upstream states."""
import itertools, sys, types
ns = {}
exec(open(sys.argv[1]).read(), ns)
from dagster_community_components.assets.infrastructure.conditional_asset import component as C
Ctx = types.SimpleNamespace(add_output_metadata=lambda m: None)
rules = ["all_success", "all_failed", "all_done", "one_success", "one_failed", "none_failed", "none_failed_min_one_success", "none_skipped"]
mine_inputs = {"success": 1, "skipped": ns["Skipped"](), "failed": ns["Failed"]("x"), "upstream_failed": ns["UpstreamFailed"]()}
bad = 0; total = 0
for rule in rules:
    for n in (1, 2, 3):
        for states in itertools.product(["success", "skipped", "failed", "upstream_failed"], repeat=n):
            total += 1
            out = ns["af_gate"](Ctx, rule, [mine_inputs[s] for s in states], "t")
            mine = "run" if out is None else ("skipped" if isinstance(out, ns["Skipped"]) else "upstream_failed")
            fn = C.af_guard(rule=rule)(lambda *a: "ran")
            res = fn(*[C.StepOutcome(s, value=1) for s in states])
            theirs = "run" if res.state == "success" else res.state
            if (mine == "run") != (theirs == "run"):
                bad += 1
                if bad <= 14: print(f"{rule:30} {states!s:55} mine={mine:16} registry={theirs}")
print(total, "combinations,", bad, "differ in whether the step runs")
