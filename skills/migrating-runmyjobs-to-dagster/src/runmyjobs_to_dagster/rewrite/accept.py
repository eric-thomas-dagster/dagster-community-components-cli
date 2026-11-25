"""The acceptance gates for a REWRITTEN unit (the rewrite route), and the record of which route produced each manifest unit.

Gates (all must pass; a gate that cannot run FAILS, it is never skipped silently; a gate that does not apply says `not run: <why>` and is recorded that way):

  definitions_load     `dg check defs` in the project (the converter's gate 2). Where `dg` is not installed the definitions are loaded with dagster and the gate says so.
  matches_spec         `dg`-loaded definitions against the spec (the converter's gate 3, but behavioral because a rewrite picks its own asset keys): every call the header accounts for maps to an
                       existing asset; every earlier-to-later pair in the chain graph is kept (downstream, or folded into one asset); the unit's schedules equal the spec's (cron and time zone);
                       pools equal the queues' pools; retries equal the spec's.
  commands_preserved   each command or SQL text appears in the unit's code or YAML (whitespace and parameter references normalised), or its header line says `reimplemented`.
  completeness         every RunMyJobs object in the spec's accounting list is named in a header docstring with what happened to it (the converter's `status summary` equivalent, per unit).
  generated_tests      `tests/test_<unit>_rewrite.py` is written and run: definitions load, keys, dependencies, schedule, pools and retries from the header, and each asset that has no external
                       system is materialized (commands that only echo run for real, SQL against a local SQLite database through RMJ_CONN_<NAME>); anything else is reported as not materialized.
  structural_checks    asset checks exist where the facts make one derivable (a declared output that is a file path); command exit status is enforced by the asset failing.
  no_stub_left         the unit's code has no loud stub (RMJ-CAVEAT): a stub means the unit is not ported, so it cannot be complete.
  no_secret_values     no credential-looking literal in the unit's code, YAML or test.
  no_invented_apis     every import resolves, and every component `type:` is a real class (registry classes are checked against the registry, not just the lazy export).

With a manifest, every unit of the rewrite unit gets `route: rewrite` and the gate result. All gates passed: pending or deferred units are advanced with the evidence. Any gate failed:
units that are not already complete stay or become `deferred` with the failure, and the converter's output remains the fallback. A unit the converter already completed keeps
`route: converter` and gets a `rewrite_attempt` note. Nothing is marked complete without the gates.
"""
from __future__ import annotations

import ast
import json
import pprint
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .. import manifest as mf
from .introspect import (command_present, corpus, dg_command, docstrings, env_for, introspect, package_of, parse_accounting, unit_files)

SECRET_LITERAL = re.compile(r"""(?i)\b\w*(?:password|passwd|secret|token|api_?key|private_?key|credential)\w*\s*[:=]\s*(['"])(?!<)([^'"\n]{6,})\1""")
URL_CREDS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]{3,}@")
ENV_OK = re.compile(r"(?i)env_var|EnvVar|environ|getenv|os\.environ|\{\{\s*env|<redacted>|\$\{?\w")
SAFE_LINE = re.compile(r"^\s*(echo|printf|print\(|true\b|:\s*$|pass\b|exit 0|select\b|with\b|#|$)", re.I)
EXTERNAL = {"sap", "mail", "missing", "recursive", "other"}


def gate(name: str, ok: bool, detail: str, ran: bool = True) -> dict[str, Any]:
    return {"gate": name, "passed": bool(ok), "ran": ran, "detail": detail if ran else "not run: " + detail}


def _tail(text: str, n: int = 600) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and "Warning" not in ln and "warnings.warn" not in ln and not ln.lstrip().startswith(("class ", "return meta"))]
    return "\n".join(lines)[-n:]


# ---------------------------------------------------------------- context


class Ctx:
    def __init__(self, spec: dict, project: Path, python: str, pkg: str | None, run_commands: bool = False, sql_materialize: bool = True, unit_dir: Path | None = None):
        self.spec, self.project, self.python = spec, project.resolve(), str(Path(python).absolute())
        self.pkg = pkg or package_of(self.project)
        self.uid = spec["unit_id"]
        self.run_commands, self.sql_materialize = run_commands, sql_materialize
        self.files = unit_files(self.project, self.pkg, self.uid)
        if unit_dir:
            self.files = [p for p in sorted(unit_dir.rglob("*")) if p.is_file() and p.suffix in (".py", ".yaml", ".yml") and "__pycache__" not in p.parts]
        self.doc = docstrings(self.files)
        self.ids = [a["id"] for a in spec["accounting"]]
        self.acc = parse_accounting(self.doc, self.ids)
        self.snapshot: dict | None = None
        self.snapshot_error = ""

    def _sensor_names(self) -> set[str]:
        return {x["name"] for x in (self.snapshot or {}).get("sensors", [])}

    def call_assets(self) -> dict[str, list[str]]:
        """call id -> the asset keys the header says replaced it (a sensor named there is not an asset: see call_sensors)."""
        sensors = self._sensor_names()
        return {c["id"]: [k for k in self.acc[c["id"]]["assets"] if k not in sensors] for c in self.spec["calls"] if c["id"] in self.acc}

    def call_sensors(self) -> dict[str, list[str]]:
        sensors = self._sensor_names()
        return {c["id"]: [k for k in self.acc[c["id"]]["assets"] if k in sensors] for c in self.spec["calls"] if c["id"] in self.acc}

    def unit_assets(self) -> set[str]:
        return {k for ks in self.call_assets().values() for k in ks}


def _closure(parents: dict[str, list[str]], key: str) -> set[str]:
    seen: set[str] = set()
    stack = list(parents.get(key, []))
    while stack:
        k = stack.pop()
        if k not in seen:
            seen.add(k)
            stack.extend(parents.get(k, []))
    return seen


# ---------------------------------------------------------------- gates


def g_load(ctx: Ctx) -> dict:
    dg = dg_command(ctx.python)
    if dg:
        r = subprocess.run(dg + ["check", "defs"], cwd=ctx.project, capture_output=True, text=True, env=env_for(ctx.project), timeout=600)
        if r.returncode != 0:
            return gate("definitions_load", False, "dg check defs: " + _tail(r.stdout + r.stderr))
        how = "dg check defs"
    else:
        how = "`dg` is not installed: the definitions were loaded with dagster instead"
    snap, err = introspect(ctx.project, ctx.python, ctx.pkg)
    ctx.snapshot, ctx.snapshot_error = snap, err
    if snap is None:
        return gate("definitions_load", False, f"{how}; the definitions could not be introspected: {err}")
    return gate("definitions_load", True, f"{how}; {len(snap['assets'])} asset(s), {len(snap['schedules'])} schedule(s), {len(snap['jobs'])} job(s) in the project")


def g_matches(ctx: Ctx) -> dict:
    if ctx.snapshot is None:
        return gate("matches_spec", False, "the definitions did not load, so nothing can be compared")
    spec, snap = ctx.spec, ctx.snapshot
    assets = {a["key"]: a for a in snap["assets"]}
    parents = {k: a["parents"] for k, a in assets.items()}
    problems: list[str] = []
    call_to_assets: dict[str, list[str]] = {}
    sensor_calls: set[str] = set()
    sensors = ctx._sensor_names()
    for c in spec["calls"]:
        e = ctx.acc.get(c["id"])
        if not e:
            problems.append(f"call {c['id']} has no header line (see completeness)")
            continue
        if e["verb"] == "dropped":
            problems.append(f"call {c['id']} is dropped in the header: a call is behavior and cannot be dropped, only replaced, folded or merged")
            continue
        keys = e["assets"]
        missing = [k for k in keys if k not in assets and k not in sensors]
        if not keys:
            problems.append(f"call {c['id']}: the header line names no asset or sensor in backticks")
        if missing:
            problems.append(f"call {c['id']}: header names {missing}, which are neither assets nor sensors in the loaded definitions")
        call_to_assets[c["id"]] = [k for k in keys if k in assets]
        if keys and all(k in sensors for k in keys):
            sensor_calls.add(c["id"])
    ctx_assets = {k for ks in call_to_assets.values() for k in ks}
    skipped_pairs = 0
    for a, b in spec["expected"]["order_pairs"]:
        ka, kb = call_to_assets.get(a, []), call_to_assets.get(b, [])
        if a in sensor_calls or b in sensor_calls:
            skipped_pairs += 1                               # replaced by a sensor: it reacts to run status, it has no asset dependencies to compare
            continue
        if not ka or not kb:
            continue
        if set(ka) & set(kb):
            continue                                         # folded into one asset: the order is kept inside it
        if not any(x in _closure(parents, y) for x in ka for y in kb):
            problems.append(f"order lost: {b} (asset {kb}) is not downstream of {a} (asset {ka})")
    # schedules
    want = sorted((s["cron"], s["time_zone"]) for s in spec["expected"]["schedules"])
    job_assets = {j["name"]: set(j["assets"]) for j in snap["jobs"]}
    mine = [s for s in snap["schedules"] if job_assets.get(s["job"], set()) & ctx_assets] if ctx_assets else []
    got = sorted((s["cron"], s["timezone"] or "UTC") for s in mine)
    extra_ok = len(spec["expected"]["untranslatable_schedule_elements"])          # an element with no cron form may be replaced by a schedule function
    rest = list(got)
    for w in want:
        if w in rest:
            rest.remove(w)
    if len(rest) != extra_ok or any(w not in got for w in want):
        problems.append(f"schedules {got} != spec {want}" + (f" plus {extra_ok} for the element(s) with no cron form" if extra_ok else "") + (" (a schedule must target a job that selects this unit's assets)" if want and not got else ""))
    for s in mine:
        if not s["timezone"]:
            problems.append(f"schedule {s['name']} has no explicit execution_timezone")
    for sc in spec["schedules"]:
        if sc["end"] and sc["end"] not in ctx.doc:
            problems.append(f"schedule {sc['name']} ends {sc['end']}: the header must say how the end date is enforced")
        if sc["untranslatable"]:
            line = ctx.acc.get(sc["name"], {}).get("text", "")
            if not re.search(r"(?i)function|custom|approx|not enforced|dropped|cannot|no cron|in-asset|check", line):
                problems.append(f"schedule {sc['name']} has element(s) with no cron form {sc['untranslatable']}: the header line must say what replaces them")
    # pools
    pools = sorted({p for k in ctx_assets for p in assets[k]["pool"]})
    if pools != sorted(spec["expected"]["pools"]):
        problems.append(f"pools {pools} != spec {sorted(spec['expected']['pools'])}")
    # retries
    held: dict[str, int] = {}
    for cid, keys in call_to_assets.items():
        for k in keys:
            held[k] = max(held.get(k, 0), spec["expected"]["retries"].get(cid, 0))
    for k, r in sorted(held.items()):
        if r and assets[k]["retries"] != r:
            problems.append(f"asset {k} retries {assets[k]['retries']} != spec {r} (a restart handler's maximum, or Maximum Starts - 1)")
    return gate("matches_spec", not problems, "; ".join(problems[:8]) if problems else
                f"{len(call_to_assets)} call(s) mapped to {len(ctx_assets)} asset(s)" + (f" ({len(sensor_calls)} to a sensor)" if sensor_calls else "") + f"; {len(spec['expected']['order_pairs']) - skipped_pairs} order pair(s) kept" + (f", {skipped_pairs} not compared (involve a sensor)" if skipped_pairs else "") + f"; schedules {got or 'none'} and pools {pools or 'none'} equal the spec")


def g_commands(ctx: Ctx) -> dict:
    texts = corpus(ctx.files)
    cmds = ctx.spec["expected"]["commands"]
    missing, redacted = [], 0
    for c in cmds:
        e = ctx.acc.get(c["call"], {})
        if command_present(c, texts) or e.get("reimplemented"):
            continue
        missing.append(c["call"])
    redacted = sum(1 for c in ctx.spec["calls"] if c["command_redacted"])
    note = f"; {redacted} command(s) hold a redacted credential and are not compared" if redacted else ""
    return gate("commands_preserved", not missing, (f"command text of {missing} is neither in the code nor marked `reimplemented` in the header" if missing
                else f"{len(cmds)} command(s) present (or marked reimplemented){note}"))


def g_complete(ctx: Ctx) -> dict:
    absent = [i for i in ctx.ids if i not in ctx.acc]
    if not ctx.doc.strip():
        return gate("completeness", False, f"no header docstring found in {ctx.pkg}/defs/{ctx.uid}(/.py): it must account for {len(ctx.ids)} object(s)")
    return gate("completeness", not absent, ("every RunMyJobs object is accounted for in the header (" + str(len(ctx.ids)) + ")") if not absent else
                f"{len(absent)} of {len(ctx.ids)} object(s) are not accounted for (an `<id>: replaced by/folded into/merged into/dropped/reimplemented` line each): {absent[:8]}")


def _safe(c: dict, ctx: Ctx) -> tuple[bool, str]:
    t, cmd = c["type"], c["command"]
    if t in EXTERNAL:
        return False, f"external system or no definition ({t})"
    if t == "sql":
        return (ctx.sql_materialize, "SQL (run against SQLite)" if ctx.sql_materialize else "SQL materialization switched off")
    if ctx.run_commands:
        return True, ""
    if t == "redwoodscript":
        return True, ""                                         # reimplemented by the rewrite; its code is the agent's, run like any test
    if all(SAFE_LINE.match(ln) for ln in cmd.splitlines()):
        return True, ""
    return False, "the command is not on the echo/print allowlist, so it is not run unless --run-commands is given"


def contract(ctx: Ctx) -> dict:
    spec = ctx.spec
    call_assets = ctx.call_assets()
    call_sensors = ctx.call_sensors()
    by_asset: dict[str, list[str]] = {}
    for cid, keys in call_assets.items():
        for k in keys:
            by_asset.setdefault(k, []).append(cid)
    calls = {c["id"]: c for c in spec["calls"]}
    mat = {}
    for k, cids in sorted(by_asset.items()):
        verdicts = [_safe(calls[c], ctx) for c in cids]
        bad = [why for ok, why in verdicts if not ok]
        mat[k] = {"calls": cids, "run": not bad, "why_not": "; ".join(sorted(set(bad)))}
    return {"unit": spec["unit"], "unit_id": ctx.uid, "pkg": ctx.pkg, "call_assets": call_assets, "call_sensors": call_sensors, "order_pairs": spec["expected"]["order_pairs"],
            "schedules": spec["expected"]["schedules"], "extra_schedules": len(spec["expected"]["untranslatable_schedule_elements"]), "pools": spec["expected"]["pools"], "retries": spec["expected"]["retries"], "materialize": mat,
            "connections": sorted({c["connection"] for c in spec["calls"] if c["connection"]}), "structural_checks": spec["expected"]["structural_checks"]}


TEST_TEMPLATE = '''"""Generated by `runmyjobs-to-dagster accept` for the rewrite of RunMyJobs unit `{unit}`. A behavioral contract, not a key-for-key one: the rewrite chose its own asset keys, and the
header docstring of its code says which asset replaced which RunMyJobs call. Nothing here is a fake pass: an asset with an external system is reported as skipped, with the reason."""
import os
import sys
from pathlib import Path

import dagster as dg
import pytest

CONTRACT = {contract}
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture(scope="module")
def defs():
    return dg.load_from_defs_folder(path_within_project=SRC / CONTRACT["pkg"])


@pytest.fixture(scope="module")
def graph(defs):
    return defs.resolve_asset_graph()


def _key(s):
    return dg.AssetKey.from_user_string(s)


def _unit_keys():
    return sorted({{k for ks in CONTRACT["call_assets"].values() for k in ks}})


def _up(graph, key):
    seen, stack = set(), list(graph.get(_key(key)).parent_keys)
    while stack:
        k = stack.pop()
        if k not in seen:
            seen.add(k)
            stack.extend(graph.get(k).parent_keys)
    return {{k.to_user_string() for k in seen}}


def test_every_call_maps_to_an_existing_asset_or_sensor(graph, defs):
    have = {{k.to_user_string() for k in graph.get_all_asset_keys()}}
    for cid, keys in CONTRACT["call_assets"].items():
        assert keys or CONTRACT["call_sensors"].get(cid), cid
        assert set(keys) <= have, (cid, sorted(set(keys) - have))
    sensors = {{s.name for s in defs.sensors or []}}
    for cid, keys in CONTRACT["call_sensors"].items():
        assert set(keys) <= sensors, (cid, sorted(set(keys) - sensors))


def test_the_chain_order_is_kept(graph):
    for a, b in CONTRACT["order_pairs"]:
        ka, kb = CONTRACT["call_assets"][a], CONTRACT["call_assets"][b]
        if CONTRACT["call_sensors"].get(a) or CONTRACT["call_sensors"].get(b):
            continue                                         # replaced by a sensor: no asset dependency to compare
        if set(ka) & set(kb):
            continue
        assert any(x in _up(graph, y) for x in ka for y in kb), f"{{b}} must run after {{a}}"


def test_schedules_match_the_source(defs):
    unit = set(_unit_keys())
    covered = {{}}
    for j in defs.jobs or []:
        covered[j.name] = {{k.to_user_string() for k in defs.resolve_job_def(j.name).asset_layer.selected_asset_keys}}
    got = sorted((s.cron_schedule, s.execution_timezone or "UTC") for s in defs.schedules or [] if covered.get(s.job_name, set()) & unit)
    want = sorted((s["cron"], s["time_zone"]) for s in CONTRACT["schedules"])
    rest = list(got)
    for w in want:
        assert w in rest, (w, got)
        rest.remove(w)
    assert len(rest) == CONTRACT["extra_schedules"], (rest, "elements with no cron form: %d" % CONTRACT["extra_schedules"])
    assert all(tz for _, tz in rest)


def test_pools_match_the_queues(defs):
    pools = set()
    for a in defs.assets or []:
        if isinstance(a, dg.AssetsDefinition) and {{k.to_user_string() for k in a.keys}} & set(_unit_keys()):
            for op in a.node_def.iterate_op_defs():
                if op.pool:
                    pools.add(op.pool)
    assert sorted(pools) == sorted(CONTRACT["pools"])


def test_retries_match_the_source(defs):
    held = {{}}
    for cid, keys in CONTRACT["call_assets"].items():
        for k in keys:
            held[k] = max(held.get(k, 0), CONTRACT["retries"].get(cid, 0))
    for a in defs.assets or []:
        if isinstance(a, dg.AssetsDefinition):
            for k in a.keys:
                want = held.get(k.to_user_string())
                if want:
                    got = max((o.retry_policy.max_retries for o in a.node_def.iterate_op_defs() if o.retry_policy), default=0)
                    assert got == want, (k.to_user_string(), got, want)


def test_structural_checks_exist_where_derivable(defs):
    need = CONTRACT["structural_checks"]
    have = [ck for c in defs.asset_checks or [] for ck in c.check_keys] + [ck for a in defs.assets or [] if isinstance(a, dg.AssetsDefinition) for ck in (a.check_keys or [])]
    assert len(have) >= len(need), f"{{len(have)}} asset check(s), the facts support {{len(need)}}: {{need}}"


@pytest.mark.parametrize("key", sorted(CONTRACT["materialize"]))
def test_materialize_each_asset_without_external_systems(defs, key, tmp_path, monkeypatch):
    m = CONTRACT["materialize"][key]
    if not m["run"]:
        pytest.skip("not materialized: " + m["why_not"])
    for name in CONTRACT["connections"]:
        monkeypatch.setenv("RMJ_CONN_" + name.upper(), "sqlite:///" + str(tmp_path / "gate.db"))
    assets = [a for a in defs.assets or [] if isinstance(a, dg.AssetsDefinition)]
    checks = list(defs.asset_checks or [])
    sel = dg.AssetSelection.assets(_key(key)) | dg.AssetSelection.checks_for_assets(_key(key))
    res = dg.materialize(assets + checks, selection=sel, resources=defs.resources or {{}}, raise_on_error=False)
    errs = [str(e.event_specific_data.error)[-300:] for e in res.get_step_failure_events()]
    assert res.success, errs
    evals = [e.event_specific_data for e in res.all_events if e.event_type_value == "ASSET_CHECK_EVALUATION"]
    assert all(ev.passed for ev in evals), [(ev.check_name, ev.metadata) for ev in evals if not ev.passed]
'''


def g_tests(ctx: Ctx) -> dict:
    if ctx.snapshot is None:
        return gate("generated_tests", False, "the definitions did not load")
    con = contract(ctx)
    tdir = ctx.project / "tests"
    tdir.mkdir(exist_ok=True)
    f = tdir / f"test_{ctx.uid}_rewrite.py"
    f.write_text(TEST_TEMPLATE.format(unit=ctx.spec["unit"], contract=pprint.pformat(con, width=140, sort_dicts=True)))
    r = subprocess.run([ctx.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", "-rs", str(f)], cwd=ctx.project, capture_output=True, text=True, env=env_for(ctx.project), timeout=900)
    out = r.stdout
    summ = [ln for ln in out.strip().splitlines() if re.search(r"\d+ (passed|failed|skipped|error)", ln)][-1:] or [_tail(r.stderr, 200)]
    skipped = re.findall(r"SKIPPED \[\d+\] [^:]+:\d+: (not materialized: .*)", out)
    ran = [k for k, m in con["materialize"].items() if m["run"]]
    detail = f"{f.name}: {summ[0].strip('= ')}; materialized {len(ran)} of {len(con['materialize'])} asset(s)"
    if ctx.spec["run_window"] or any(c["run_window"] for c in ctx.spec["calls"]):
        detail += " [the unit has a run window: outside it the components skip, so a materialization at this time proves it loads and exits cleanly, not that the command ran]"
    if skipped:
        detail += " (not materialized: " + "; ".join(sorted(set(s.replace('not materialized: ', '') for s in skipped))) + ")"
    if r.returncode != 0:
        fails = re.findall(r"^FAILED (\S+)(?: - (.*))?$", out, re.M)
        detail += "; " + "; ".join(f"{n.split('::')[-1]}{(': ' + m[:200]) if m else ''}" for n, m in fails[:4])
    return gate("generated_tests", r.returncode == 0, detail)


def g_structural(ctx: Ctx) -> dict:
    need = ctx.spec["expected"]["structural_checks"]
    if not need:
        return gate("structural_checks", True, "the model carries no output location that a structural check could use (declared outputs are table or dataset names); command exit status is enforced by the asset failing, "
                    "and no business check is invented", ran=False)
    have = (ctx.snapshot or {}).get("checks", [])
    return gate("structural_checks", len(have) >= len(need), f"{len(have)} asset check(s) in the project; the facts support {len(need)} (a file output exists and is not empty): {[n['output'] for n in need]}")


def g_stubs(ctx: Ctx) -> dict:
    hits = []
    for f in ctx.files:
        if f.suffix == ".py":
            try:
                tree = ast.parse(f.read_text())
            except SyntaxError as e:
                return gate("no_stub_left", False, f"{f.name} does not parse: {e.msg}")
            doc_nodes = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body
                         and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "RMJ-CAVEAT" in node.value and id(node) not in doc_nodes:
                    hits.append(f"{f.name}:{node.lineno}")
                elif isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "rmj_caveat":
                    hits.append(f"{f.name}:{node.lineno}")
        elif f.suffix in (".yaml", ".yml") and "RMJ-CAVEAT" in f.read_text():
            hits.append(f.name)
    return gate("no_stub_left", not hits, "no loud stub (RMJ-CAVEAT) in the unit's code" if not hits else f"a loud stub remains at {', '.join(hits[:5])}: the step is not ported, so the unit cannot be complete")


def g_secrets(ctx: Ctx) -> dict:
    texts = [(f.name, f.read_text()) for f in ctx.files if f.is_file()]
    t = ctx.project / "tests" / f"test_{ctx.uid}_rewrite.py"
    if t.exists():
        texts.append((t.name, t.read_text()))
    hits = []
    for name, text in texts:
        for n, ln in enumerate(text.splitlines(), 1):
            if (SECRET_LITERAL.search(ln) or URL_CREDS.search(ln)) and not ENV_OK.search(ln):
                hits.append(f"{name}:{n} a credential-looking literal")
    return gate("no_secret_values", not hits, "none found in " + str(len(texts)) + " file(s)" if not hits else "; ".join(hits[:5]))


_API_SCRIPT = r'''
import ast, importlib, json, re, sys
from pathlib import Path
files = [Path(p) for p in sys.argv[1:]]
bad = []
for f in files:
    if f.suffix == ".py":
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError as e:
            bad.append(f"{f.name}: syntax error {e.msg}"); continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                try:
                    mod = importlib.import_module(node.module)
                except Exception as e:
                    bad.append(f"import {node.module} ({type(e).__name__})"); continue
                for a in node.names:
                    if a.name != "*" and not hasattr(mod, a.name):
                        try:
                            importlib.import_module(f"{node.module}.{a.name}")
                        except Exception:
                            bad.append(f"{node.module}.{a.name} does not exist")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    try:
                        importlib.import_module(a.name)
                    except Exception as e:
                        bad.append(f"import {a.name} ({type(e).__name__})")
    elif f.suffix in (".yaml", ".yml"):
        for m in re.finditer(r"^type:\s*([\w.]+)\s*$", f.read_text(), re.M):
            mod_name, _, cls = m.group(1).rpartition(".")
            try:
                mod = importlib.import_module(mod_name)
                if mod_name == "dagster_community_components":
                    if not hasattr(mod, cls):
                        bad.append(f"component type {m.group(1)} is not in the registry package")
                    else:
                        mod._load_real_class(cls)
                elif not hasattr(mod, cls):
                    bad.append(f"component type {m.group(1)} does not exist")
            except Exception as e:
                bad.append(f"component type {m.group(1)} ({type(e).__name__}: {str(e)[:80]})")
print("RMJ_APIS=" + json.dumps(bad))
'''


def g_apis(ctx: Ctx) -> dict:
    r = subprocess.run([ctx.python, "-c", _API_SCRIPT, *map(str, ctx.files)], cwd=ctx.project, capture_output=True, text=True, env=env_for(ctx.project))
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RMJ_APIS=")), None)
    if line is None:
        return gate("no_invented_apis", False, "could not check: " + _tail(r.stderr, 300))
    bad = json.loads(line[len("RMJ_APIS="):])
    return gate("no_invented_apis", not bad, "every import resolves and every component type is a real class" if not bad else "; ".join(bad[:6]))


def run_gates(spec: dict, project: Path, python: str = sys.executable, pkg: str | None = None, run_commands: bool = False, sql_materialize: bool = True, unit_dir: Path | None = None) -> list[dict]:
    ctx = Ctx(spec, project, python, pkg, run_commands, sql_materialize, unit_dir)
    if not ctx.files:
        return [gate("definitions_load", False, f"no source found for unit {ctx.uid}: expected src/{ctx.pkg}/defs/{ctx.uid}/ or src/{ctx.pkg}/defs/{ctx.uid}.py (or pass --unit-dir)")]
    out = [g_load(ctx), g_matches(ctx), g_commands(ctx), g_complete(ctx), g_tests(ctx), g_structural(ctx), g_stubs(ctx), g_secrets(ctx), g_apis(ctx)]
    return out


# ---------------------------------------------------------------- manifest record


def annotate_manifest(manifest: dict, specs: dict[str, dict]) -> None:
    """Stamp each manifest unit with the rewrite unit it belongs to (queue units: every unit that uses the queue) and the default route."""
    for u in manifest["units"]:
        u.setdefault("route", "converter")
        owners = [s["unit"] for s in specs.values() if u["id"] in s["manifest_units"]]
        queue_owners = [s["unit"] for s in specs.values() if u["id"] in s.get("queue_manifest_units", [])]
        if owners:
            u["rewrite_unit"] = owners[0]
        if queue_owners:
            u["rewrite_units"] = sorted(queue_owners)


def record(manifest: dict, unit: str, results: list[dict], python_note: str = "") -> dict[str, int]:
    ok = all(r["passed"] for r in results)
    summary = {"passed": ok, "gates": {r["gate"]: ("pass" if r["passed"] and r["ran"] else "not run" if r["passed"] else "FAIL") for r in results}}
    fails = [f"{r['gate']}: {r['detail']}" for r in results if not r["passed"]]
    stats = {"advanced": 0, "deferred": 0, "kept_converter": 0, "waiting": 0}
    units = manifest["units"]
    passed = set(manifest.setdefault("rewrite_passed", []))
    if ok:
        passed.add(unit)
    else:
        passed.discard(unit)
    manifest["rewrite_passed"] = sorted(passed)

    def converter_done(name: str) -> bool:
        mine = [x for x in units if x.get("rewrite_unit") == name]
        return bool(mine) and all(x["status"] == "complete" and x.get("route", "converter") == "converter" for x in mine)

    for u in units:
        shared = u.get("rewrite_units")
        if u.get("rewrite_unit") != unit and not (shared and unit in shared):
            continue
        if shared and u.get("rewrite_unit") != unit:                       # a queue shared by several units: complete only when every unit that uses it is done
            u.setdefault("gates_by_unit", {})[unit] = summary
            if ok and all(o in passed or converter_done(o) for o in shared) and u["status"] != "complete":
                u["route"] = "rewrite"
                mf.advance(manifest, u["id"], "rewrite route: every unit using this queue passed its gates (" + ", ".join(shared) + "); pool and limit are in the header")
                stats["advanced"] += 1
            elif ok and u["status"] != "complete":
                stats["waiting"] += 1
            continue
        if u["status"] == "complete" and u.get("route", "converter") == "converter":
            u["rewrite_attempt"] = {**summary, "failures": fails}
            stats["kept_converter"] += 1
            continue
        u["route"] = "rewrite"
        u["gates"] = summary
        if ok:
            mf.advance(manifest, u["id"], "rewrite route: all gates passed (" + ", ".join(r["gate"] for r in results) + ")")
            stats["advanced"] += 1
        else:
            mf.defer(manifest, u["id"], "rewrite failed its gates (the converter output remains the fallback): " + "; ".join(fails)[:800])
            stats["deferred"] += 1
    return stats
