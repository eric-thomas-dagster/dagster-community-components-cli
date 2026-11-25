"""Run the converter route on every fixture model and gate it (dg check defs, expected assets/deps/schedules from the manifest), unit by unit; print a table.

    python run_converter_gates.py <workdir> [--python PATH]

Needs this skill's dev environment (dagster, dg, dagster-community-components). The model fixtures are the skill's own tests/fixtures and bundled sample.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from runmyjobs_to_dagster.convert import convert, write_project
from runmyjobs_to_dagster.extract_car import load_car
from runmyjobs_to_dagster.extract_rest import load_rest
from runmyjobs_to_dagster.loader import load_model
from runmyjobs_to_dagster.manifest import build_manifest
from runmyjobs_to_dagster.rewrite.introspect import dg_command, env_for, introspect
from runmyjobs_to_dagster.rewrite.spec import build_specs

SKILL = Path(__file__).resolve().parents[2]
F = SKILL / "tests/fixtures"
MODELS = {
    "sample": lambda: load_model(SKILL / "src/runmyjobs_to_dagster/samples/sample_model.yaml"),
    "linear_chain": lambda: load_model(F / "linear_chain.yaml"),
    "car": lambda: load_car(F / "car")[0],
    "car_dm": lambda: load_car(F / "car_dm")[0],
    "rest_json": lambda: load_rest(F / "rest_json")[0],
    "car_chains": lambda: load_car(F / "car_chains")[0],
    "car_isu": lambda: load_car(F / "car_isu")[0],
    "car_plan_to_produce": lambda: load_car(F / "car_plan_to_produce")[0],
}


def main(work: Path, python: str) -> list[dict]:
    rows = []
    for name, loader in MODELS.items():
        model = loader()
        specs, estate, _ = build_specs(model)
        result = convert(model, output="components", pkg="rmj_conv")
        proj = work / name
        write_project(result, proj, "rmj_conv")
        manifest = build_manifest(result)
        dg = dg_command(python)
        r = subprocess.run(dg + ["check", "defs"], cwd=proj, capture_output=True, text=True, env=env_for(proj))
        snap, err = introspect(proj, python, "rmj_conv")
        g2 = r.returncode == 0 and snap is not None
        assets = {a["key"]: set(a["parents"]) for a in (snap or {}).get("assets", [])}
        scheds = {s["name"]: s["cron"] for s in (snap or {}).get("schedules", [])}
        bad = {}
        for u in manifest["units"]:
            e, probs = u["expected"], []
            if "asset" in e and (e["asset"] not in assets or assets[e["asset"]] != set(e.get("deps", []))):
                probs.append(f"asset {e['asset']}")
            for s in e.get("schedules", []):
                if scheds.get(s["name"]) != s["cron"]:
                    probs.append(f"schedule {s['name']}")
            if probs:
                bad[u["id"]] = probs
        for unit, s in sorted(specs.items()):
            ids = set(s["manifest_units"])
            rows.append({"model": name, "unit": unit, "kind": s["kind"], "calls": len(s["calls"]), "disposition": s["converter"]["disposition"],
                         "converter_assets": len(s["converter"]["assets"]), "stubbed": len(s["converter"]["stubbed"]), "degraded": len(s["converter"]["degraded"]),
                         "gate_load": "pass" if g2 else "FAIL", "gate_structure": "pass" if not (ids & set(bad)) else "FAIL: " + "; ".join(sum((bad[i] for i in ids & set(bad)), []))})
    return rows


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    py = sys.argv[sys.argv.index("--python") + 1] if "--python" in sys.argv else sys.executable
    rows = main(out, str(Path(py).absolute()))
    (out / "converter_gates.json").write_text(json.dumps(rows, indent=1))
    print("| model | unit | kind | calls | converter disposition | assets | stubbed | degraded | load | structure |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['model']} | {r['unit']} | {r['kind']} | {r['calls']} | {r['disposition']} | {r['converter_assets']} | {r['stubbed']} | {r['degraded']} | {r['gate_load']} | {r['gate_structure']} |")
