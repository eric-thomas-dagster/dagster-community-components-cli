"""Run `conditional_asset` (dagster-community-components 0.10.194) on Dagster and print one JSON line of what it materializes.

Not a test file: `test_rewrite_resource_first.py` runs it with the interpreter that has dagster-community-components 0.10.194 installed (the registry venv), skips when there is none, and asserts the
claims `reference/rewrite-patterns/branching.md` and `short_circuit.md` make about the decision assets. Everything is offline."""
import json
import logging
import pathlib
import sys
import tempfile
import textwrap

import dagster as dg
from dagster_community_components import ConditionalAssetComponent as C

logging.disable(logging.CRITICAL)
d = pathlib.Path(tempfile.mkdtemp())
sys.path.insert(0, str(d))
(d / "routing_probe.py").write_text(textwrap.dedent('''
    def choose():
        return "a"
    def falsy():
        return False
    def truthy():
        return True
'''))


def run(*comps):
    defs = dg.Definitions.merge(*(C(**kw).build_defs(None) for kw in comps))
    r = dg.materialize(list(defs.assets), raise_on_error=False)
    meta = {"/".join(e.asset_key.path): {k: getattr(v, "value", v) for k, v in e.materialization.metadata.items()} for e in r.get_asset_materialization_events()}
    return {"success": r.success, "materialized": sorted(meta), "metadata": meta}


out = {
    "branch": run(dict(mode="branch", callable_path="routing_probe:choose", branches={"a": "x/a", "b": "x/b"})),
    "short_false": run(dict(mode="short_circuit", callable_path="routing_probe:falsy", asset_key="g/gate")),
    "short_true": run(dict(mode="short_circuit", callable_path="routing_probe:truthy", asset_key="g/gate")),
    "two_branch_explicit": run(dict(mode="branch", callable_path="routing_probe:choose", branches={"a": "x/a", "b": "x/b"}, decision_asset_key="x/decision1"),
                               dict(mode="branch", callable_path="routing_probe:choose", branches={"a": "x/c", "b": "x/d"}, decision_asset_key="x/decision2")),
}
try:
    run(dict(mode="branch", callable_path="routing_probe:choose", branches={"a": "x/a", "b": "x/b"}), dict(mode="branch", callable_path="routing_probe:choose", branches={"a": "x/c", "b": "x/d"}))
    out["two_branch_default"] = {"error": None}
except Exception as e:                                                      # noqa: BLE001
    out["two_branch_default"] = {"error": f"{type(e).__name__}: {e}"[:200]}
print(json.dumps(out))
