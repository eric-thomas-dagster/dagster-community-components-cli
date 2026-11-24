"""Validation gates for a generated project.

Gate 1: generated Python compiles.
Gate 2: `dg list defs --json` succeeds (definitions load).
Gate 3: the loaded definitions match the manifest's expectations (assets, deps, jobs, schedules).
Gate 4 (`execute`, opt-in): every asset runs once with RMJ_DRY_RUN=1: commands and SQL are rendered and logged but not executed, scripts
are compiled, retries/pools/dependencies are exercised by Dagster itself. A stub must fail with RMJ-CAVEAT and nothing else may fail.
Both outputs support it: the python runtime and the generated `components.py` (subclasses of the registry shell and SQL components) honor
RMJ_DRY_RUN. A components project whose YAML points at the plain registry shell or SQL components would run the real commands, so it is refused.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    gate1: bool = True
    gate2: bool = True
    errors: list[str] = field(default_factory=list)
    passed_units: list[str] = field(default_factory=list)
    failed_units: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.gate1 and self.gate2 and not self.failed_units


def _load_defs(project: Path) -> tuple[dict | None, str]:
    proc = subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd=project,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout)[-2000:]
    start = proc.stdout.find("{")
    try:
        return json.loads(proc.stdout[start:]), ""
    except json.JSONDecodeError as e:
        return None, f"could not parse dg output: {e}"


def validate(project: Path, manifest: dict) -> GateResult:
    res = GateResult()

    for py in sorted((project / "src").rglob("*.py")):
        try:
            compile(py.read_text(), str(py), "exec")
        except SyntaxError as e:
            res.gate1 = False
            res.errors.append(f"gate 1: {py}: {e}")
    if not res.gate1:
        return res

    defs, err = _load_defs(project)
    if defs is None:
        res.gate2 = False
        res.errors.append(f"gate 2: definitions failed to load: {err}")
        return res

    assets = {a["asset_key"]: set(a["dependency_keys"]) for a in defs["assets"]}
    jobs = {j["name"] for j in defs["jobs"]}
    scheds = {s["name"]: s["cron_schedule"] for s in defs["schedules"]}
    sensors = {s["name"] for s in defs.get("sensors", [])}

    for u in manifest["units"]:
        exp, problems = u["expected"], []
        if "asset" in exp:
            if exp["asset"] not in assets:
                problems.append(f"asset {exp['asset']!r} missing")
            elif assets[exp["asset"]] != set(exp.get("deps", [])):
                problems.append(f"asset {exp['asset']!r} deps {sorted(assets[exp['asset']])} != expected {sorted(exp.get('deps', []))}")
        if "sensor" in exp and exp["sensor"] not in sensors:
            problems.append(f"sensor {exp['sensor']!r} missing")
        if "job" in exp and exp["job"] not in jobs:
            problems.append(f"job {exp['job']!r} missing")
        for s in exp.get("schedules", []):
            if s["name"] not in scheds:
                problems.append(f"schedule {s['name']!r} missing")
            elif scheds[s["name"]] != s["cron"]:
                problems.append(f"schedule {s['name']!r} cron {scheds[s['name']]!r} != expected {s['cron']!r}")
        if problems:
            res.failed_units[u["id"]] = problems
        elif exp:
            res.passed_units.append(u["id"])
    return res


_EXEC_SCRIPT = r"""
import json, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import dagster as dg
pkg_dir = next(p for p in Path("src").iterdir() if (p / "defs").is_dir())
defs = dg.load_from_defs_folder(path_within_project=pkg_dir)
job = defs.get_implicit_global_asset_job_def()
out = {}
for spec in defs.resolve_all_asset_specs():
    key = spec.key
    r = job.execute_in_process(asset_selection=[key], raise_on_error=False)
    err = ""
    if not r.success:
        for e in r.all_events:
            d = e.event_specific_data
            if e.is_failure and getattr(d, "error", None):
                er = d.error
                while getattr(er, "cause", None):
                    er = er.cause
                err = (er.message or er.cls_name or "").strip()
                break
    out["/".join(key.path)] = {"ok": r.success, "error": err}
print("RMJ_EXEC_RESULT=" + json.dumps(out))
"""


@dataclass
class ExecResult:
    ran: dict[str, dict] = field(default_factory=dict)       # asset -> {ok, error}
    problem: str = ""

    @property
    def stubs(self) -> list[str]:
        return [a for a, r in self.ran.items() if not r["ok"] and "RMJ-CAVEAT" in r["error"]]

    @property
    def unexpected(self) -> dict[str, str]:
        return {a: r["error"] or "failed" for a, r in self.ran.items() if not r["ok"] and "RMJ-CAVEAT" not in r["error"]}

    @property
    def ok(self) -> bool:
        return not self.problem and not self.unexpected


def execute(project: Path) -> ExecResult:
    """Gate 4: run every asset once in dry-run mode."""
    res = ExecResult()
    import re
    plain = [str(f.relative_to(project)) for f in (project / "src").rglob("defs.yaml")
             if re.search(r"type:\s*dagster_community_components\.(ShellCommandAssetComponent|SqlAssetComponent|TaskChainAssetComponent|EnrichedDbtCloudWorkspaceComponent|EmailSendAssetComponent|SapJobTriggerAssetComponent)\b", f.read_text())]
    if plain:
        res.problem = (f"gate 4 refused: {', '.join(plain)} use the plain registry shell, SQL or task chain component, which would run the real commands. "
                       "Point them at the project's Rmj* subclasses (src/<pkg>/components.py), which honor the dry run")
        return res
    proc = subprocess.run(["uv", "run", "python", "-c", _EXEC_SCRIPT], cwd=project, capture_output=True, text=True,
                          env={**__import__("os").environ, "RMJ_DRY_RUN": "1"})
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RMJ_EXEC_RESULT=")), None)
    if proc.returncode != 0 or line is None:
        res.problem = f"gate 4 could not run: {(proc.stderr or proc.stdout)[-1500:]}"
        return res
    res.ran = json.loads(line.split("=", 1)[1])
    return res
