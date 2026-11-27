"""The rewrite route's pattern library: which patterns a DAG's spec shows, the text injected into its prompt, and the decision-record format the gate checks.

    detect(spec)            -> ordered pattern names the spec shows (`branching`, `short_circuit`, `skip`, `trigger_rules`, `mapped_tasks`, `sensors`, `depends_on_past`)
    render(names)           -> markdown for the prompt: each pattern's `.md` and its working `.py` from `reference/rewrite-patterns/` (every .py is executed by tests/test_rewrite_patterns.py)
    flagged(spec)           -> the non-obvious behaviours the spec flags (each needs a line in the header's decision record)
    parse_record(docstring) -> {behaviour: {"verdict": "preserved" | "not preserved", "reason": str}} from the header's `Decision record:` section
    check_record(spec, doc) -> (ok, detail, declared)   (`declared` has the behaviours preserved, not preserved with reasons, and fallback to plain Python with reasons)

Decision record format (in the module docstring, after the per-task accounting):

    Decision record:
    - skip/branch: preserved | not preserved: <why> | fallback to plain Python: <why>
    - trigger rules: ...
    - retries: ...      - pools: ...      - sensor modes: ...      - depends_on_past: ...      - failure semantics: ...      - mapped tasks: ...      - cross-DAG triggers: ...      - xcom: ...      - task groups: ...
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PATTERN_DIRS = [HERE.parent / "reference" / "rewrite-patterns", HERE / "rewrite-patterns"]      # the repository layout, then a flat copy next to the script
SKIP_SRC = re.compile(r"AirflowSkipException|skip_on_exit_code|exit\s+99|\.skip_if\(|\.run_if\(")
ORDER = ["branching", "short_circuit", "skip", "trigger_rules", "mapped_tasks", "sensors", "depends_on_past", "cross_dag", "pools", "xcom", "task_groups", "templating_config", "venv_pipes", "timeouts_sla", "latest_only"]

# behaviour -> (regex that recognises its line in the record, what flags it in the spec)
BEHAVIOURS = {
    "skip/branch": re.compile(r"skip|branch|short[\s_-]*circuit", re.I),
    "trigger rules": re.compile(r"trigger[\s_-]*rule", re.I),
    "retries": re.compile(r"retr", re.I),
    "pools": re.compile(r"pool|concurren", re.I),
    "sensor modes": re.compile(r"sensor", re.I),
    "depends_on_past": re.compile(r"depends[\s_-]*on[\s_-]*past", re.I),
    "failure semantics": re.compile(r"failure|fail", re.I),
    "mapped tasks": re.compile(r"mapp|expand", re.I),
    "cross-DAG triggers": re.compile(r"cross|trigger[\s_-]*dag|external|asset[\s_-]*trigger", re.I),
    "xcom": re.compile(r"xcom|value flow", re.I),
    "task groups": re.compile(r"task[\s_-]*group|group", re.I),
    "templating / connections": re.compile(r"templat|jinja|connection|params|variable", re.I),
    "virtualenv": re.compile(r"venv|virtualenv|interpreter|pipes", re.I),
    "timeouts/sla": re.compile(r"timeout|sla\b", re.I),
    "latest only": re.compile(r"latest", re.I),
}


VENV_OP = re.compile(r"Virtualenv|ExternalPython|PythonExternal|DockerOperator|_DockerDecorated|KubernetesPod|_KubernetesDecorated")      # virtualenv, external_python (incl. the decorated class names), docker / kubernetes operators


def _src(t: dict[str, Any]) -> str:
    c = t.get("callable") or {}
    return (c.get("source") or "") + " " + " ".join(str(v) for v in (t.get("arguments") or {}).values())


def detect(spec: dict[str, Any]) -> list[str]:
    tasks = spec.get("tasks") or []
    found = set()
    for t in tasks:
        op = t.get("operator") or ""
        if "Branch" in op:
            found.add("branching")
        if "ShortCircuit" in op:
            found.add("short_circuit")
        if SKIP_SRC.search(_src(t)):
            found.add("skip")
        if (t.get("trigger_rule") or "all_success") != "all_success" or t.get("is_setup") or t.get("is_teardown"):
            found.add("trigger_rules")
        if t.get("is_mapped") or t.get("expand"):
            found.add("mapped_tasks")
        if t.get("sensor") or "Sensor" in op:
            found.add("sensors")
        if t.get("depends_on_past") or t.get("wait_for_downstream"):
            found.add("depends_on_past")
        if "TriggerDagRun" in op or "ExternalTask" in op or "Trigger" in op and "Dag" in op:
            found.add("cross_dag")
        if t.get("pool") not in (None, "default_pool") or t.get("max_active_tis_per_dag"):
            found.add("pools")
        if t.get("reads_value_from"):
            found.add("xcom")
        if t.get("group"):
            found.add("task_groups")
        if re.search(r"\{\{|params\.|var\.(value|json)|Variable\.get|context\[.(ds|data_interval_start|logical_date)", str(t.get("arguments")) + " " + str((t.get("callable") or {}).get("source") or "")) or t.get("connections"):
            found.add("templating_config")
        if VENV_OP.search(op):
            found.add("venv_pipes")
        if t.get("execution_timeout_s") or t.get("sla_s") or any("sla" in c.lower() for c in (t.get("callbacks") or [])):
            found.add("timeouts_sla")
        if "LatestOnly" in op:
            found.add("latest_only")
        if re.search(r"deferrable['\"]?\s*:\s*True", str(t.get("arguments"))):
            found.add("sensors")
    if (spec.get("schedule") or {}).get("assets"):
        found.add("cross_dag")
    return [n for n in ORDER if n in found]


def flagged(spec: dict[str, Any]) -> list[str]:
    tasks = spec.get("tasks") or []
    pats = set(detect(spec))
    out = []
    if pats & {"branching", "short_circuit", "skip"}:
        out.append("skip/branch")
    if "trigger_rules" in pats:
        out.append("trigger rules")
    if any((t.get("retries") or 0) > 0 for t in tasks):
        out.append("retries")
    if any(t.get("pool") not in (None, "default_pool") for t in tasks):
        out.append("pools")
    if "sensors" in pats:
        out.append("sensor modes")
    if "depends_on_past" in pats:
        out.append("depends_on_past")
    if "mapped_tasks" in pats:
        out.append("mapped tasks")
    if "cross_dag" in pats:
        out.append("cross-DAG triggers")
    if "xcom" in pats:
        out.append("xcom")
    if "task_groups" in pats:
        out.append("task groups")
    if "templating_config" in pats:
        out.append("templating / connections")
    if "venv_pipes" in pats:
        out.append("virtualenv")
    if "timeouts_sla" in pats:
        out.append("timeouts/sla")
    if "latest_only" in pats:
        out.append("latest only")
    if any(any(re.search(r"fail|retry|sla", c, re.I) for c in (t.get("callbacks") or [])) or t.get("execution_timeout_s") for t in tasks) or "trigger_rules" in pats or "sensors" in pats:
        out.append("failure semantics")
    return out


def index() -> dict[str, Any]:
    """The machine-readable index (`reference/rewrite-patterns/index.json`): feature -> pattern id -> files -> verified status -> what Dagster can and cannot express. The converter reads this too."""
    import json
    return json.loads(_read("index", ".json"))


def _read(name: str, ext: str) -> str:
    for d in PATTERN_DIRS:
        p = d / f"{name}{ext}"
        if p.exists():
            return p.read_text()
    raise FileNotFoundError(f"rewrite pattern {name}{ext}")


# Rows of the "what Dagster can express" table that matter per pattern (the first four rows, the plain states, are always kept)
ROW_FOR = {
    "branching": r"branch|join|skip", "short_circuit": r"short\s*circuit|branch|join", "skip": r"skip|sentinel", "trigger_rules": r"all_done|one_failed|all_failed|upstream_failed|failure",
    "mapped_tasks": r"mapped|dynamic", "sensors": r"sensor|wait|deferred|queued|poll", "depends_on_past": r"depends_on_past", "cross_dag": r"TriggerDagRun|ExternalTask|asset trigger",
    "pools": r"pool", "xcom": r"xcom", "task_groups": r"taskgroup", "templating_config": r"$^", "venv_pipes": r"$^", "timeouts_sla": r"$^", "latest_only": r"$^",
}
SKIP_FAMILY = {"branching", "short_circuit", "skip", "mapped_tasks"}


def _state_table(names: list[str]) -> str:
    """The README's representation table (what Dagster can and cannot express), cut to the rows these patterns need; the 'choice rule' bullets are dropped (the prompt's principle paragraph says them once)."""
    try:
        body = _read("README", ".md").split("## What Dagster can and cannot express")[1].split("## Index")[0]
    except (FileNotFoundError, IndexError):
        return ""
    lines = body.splitlines()
    head, rows, tail, in_table = [], [], [], False
    for ln in lines:
        if ln.startswith("|"):
            in_table = True
            rows.append(ln)
        elif in_table:
            tail.append(ln)
    want = re.compile("|".join(f"(?:{ROW_FOR[n]})" for n in names if n in ROW_FOR) or "$^", re.I)
    keep = rows[:6] + [r for r in rows[6:] if want.search(r)]          # header, separator, success, failed, up_for_retry, upstream_failed
    return "\n".join(["## What Dagster can and cannot express (rows for the constructs above; the full table is in the pattern library)", ""] + keep + tail).rstrip()


def render(names: list[str]) -> str:
    """The prompt text for the detected patterns ('' when none). The code is the tested code, verbatim."""
    if not names:
        return ""
    if ("branching" in names or "short_circuit" in names) and "skip" not in names:
        names = [n for n in ORDER if n in names or n == "skip"]       # their text points at skip.py
    out = ["### Patterns this DAG needs (verified Dagster code)", "",
           "Each pattern shows the NATIVE Dagster form first and plain Python inside one asset as the stated fallback; the code was executed on the installed Dagster and its skip/state behaviour asserted. Use the "
           "native form, change the names, keep the semantics; fall back to plain Python only for the reasons the pattern lists, and then say `fallback to plain Python: <reason>` in the decision record. The table says what Dagster cannot express: say so in the header and the decision record.", ""]
    tbl = _state_table(names)
    if tbl:
        out += [tbl, ""]
    for n in names:
        out += [_read(n, ".md").rstrip(), "", f"```python  # {n}.py (tested)", _read(n, ".py").rstrip(), "```", ""]
        if n == "branching":
            out += ["The FALLBACK form (`branching_plain_fallback.py`, tested; not pasted here: it is an `if/else` calling the chosen branch's code in ONE asset, the join ordinary code after it) is for the three cases above only, said in the decision record.", ""]
    return "\n".join(out)


def parse_record(doc: str) -> dict[str, dict[str, str]]:
    m = re.search(r"^[ \t]*Decision record\s*:?[ \t]*\n(.*?)(?=\n[ \t]*\n[ \t]*\S|\Z)", doc, re.I | re.S | re.M)
    if not m:
        return {}
    rec: dict[str, dict[str, str]] = {}
    for ln in m.group(1).splitlines():
        mm = re.match(r"\s*[-*]\s*([^:]+?)\s*:\s*(not preserved|preserved|fallback to plain python)\b\s*:?\s*(.*)$", ln, re.I)
        if not mm:
            continue
        label, verdict, reason = mm.group(1), mm.group(2).lower(), mm.group(3).strip()
        verdict = "fallback" if verdict.startswith("fallback") else verdict
        for b, rx in BEHAVIOURS.items():
            if rx.search(label) and b not in rec:
                rec[b] = {"verdict": verdict, "reason": reason, "label": label}
                break
    return rec


def check_record(spec: dict[str, Any], doc: str) -> tuple[bool, str, dict[str, Any]]:
    need = flagged(spec)
    rec = parse_record(doc)
    missing = [b for b in need if b not in rec]
    noreason = [b for b in need if b in rec and rec[b]["verdict"] in ("not preserved", "fallback") and len(rec[b]["reason"]) < 4]
    declared = {"flagged": need, "preserved": sorted(b for b, v in rec.items() if v["verdict"] == "preserved"), "not_preserved": {b: v["reason"] for b, v in rec.items() if v["verdict"] == "not preserved"},
                "fallback": {b: v["reason"] for b, v in rec.items() if v["verdict"] == "fallback"}}
    if not rec and not need:
        return True, "no non-obvious behaviour is flagged by the spec and no record was needed", declared
    if missing or noreason:
        bits = []
        if not rec:
            bits.append("the header has no `Decision record:` section")
        if missing:
            bits.append(f"no `- <behaviour>: preserved | not preserved: <why>` line for: {missing}")
        if noreason:
            bits.append(f"`not preserved` / `fallback to plain Python` without a reason for: {noreason}")
        return False, "; ".join(bits), declared
    n, fb = len(declared["not_preserved"]), len(declared["fallback"])
    return True, f"decision record covers {need or 'nothing flagged'}; {n} behaviour(s) declared not preserved" + (f" ({', '.join(declared['not_preserved'])})" if n else "") + f"; {fb} fallback(s) to plain Python" + (f" ({', '.join(declared['fallback'])})" if fb else ""), declared
