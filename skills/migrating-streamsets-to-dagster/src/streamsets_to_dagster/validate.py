"""Validation gates for a generated project.

Gate 1: generated Python compiles.
Gate 2: `dg list defs --json` succeeds (definitions load).
Gate 3: loaded assets, jobs and schedules match the manifest.
Gate 4 (`execute`, opt-in): every asset runs once with SDC_DRY_RUN=1: reads and writes are logged, not performed; a stub must fail with SDC-CAVEAT and nothing else may fail.
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
        for want in exp.get("assets", []):
            if want["asset"] not in assets:
                problems.append(f"asset {want['asset']!r} missing")
            elif assets[want["asset"]] != set(want["deps"]):
                problems.append(f"asset {want['asset']!r} deps {sorted(assets[want['asset']])} != expected {sorted(want['deps'])}")
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
skip = set(json.loads(Path("sink_components.json").read_text())) if Path("sink_components.json").exists() else set()
for spec in defs.resolve_all_asset_specs():
    key = spec.key
    if "/".join(key.path) in skip:
        out["/".join(key.path)] = {"ok": True, "error": "", "cls": "", "skipped": "registry component that sends data: not run in dry mode"}
        continue
    pd = spec.partitions_def
    pk = None
    if pd is not None:
        keys = pd.get_partition_keys()
        pk = keys[-1] if keys else None
    r = job.execute_in_process(asset_selection=[key], partition_key=pk, raise_on_error=False)
    err, cls = "", ""
    if not r.success:
        for e in r.all_events:
            d = e.event_specific_data
            if e.is_failure and getattr(d, "error", None):
                er = d.error
                while getattr(er, "cause", None):
                    er = er.cause
                err, cls = (er.message or er.cls_name or "").strip(), er.cls_name or ""
                break
    out["/".join(key.path)] = {"ok": r.success, "error": err, "cls": cls}
print("SDC_EXEC_RESULT=" + json.dumps(out))
"""


@dataclass
class ExecResult:
    ran: dict[str, dict] = field(default_factory=dict)       # asset -> {ok, error}
    problem: str = ""

    @property
    def stubs(self) -> list[str]:
        return [a for a, r in self.ran.items() if not r["ok"] and "SDC-CAVEAT" in r["error"]]

    @property
    def unexpected(self) -> dict[str, str]:
        return {a: r["error"] or "failed" for a, r in self.ran.items()
                if not r["ok"] and "SDC-CAVEAT" not in r["error"]}

    @property
    def ok(self) -> bool:
        return not self.problem and not self.unexpected


def execute(project: Path) -> ExecResult:
    """Gate 4: run every asset once in dry-run mode."""
    res = ExecResult()
    proc = subprocess.run(["uv", "run", "python", "-c", _EXEC_SCRIPT], cwd=project, capture_output=True, text=True,
                          env={**__import__("os").environ, "SDC_DRY_RUN": "1"})
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("SDC_EXEC_RESULT=")), None)
    if proc.returncode != 0 or line is None:
        res.problem = f"gate 4 could not run: {(proc.stderr or proc.stdout)[-1500:]}"
        return res
    res.ran = json.loads(line.split("=", 1)[1])
    return res
