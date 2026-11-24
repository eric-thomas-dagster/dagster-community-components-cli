"""Read a folder of ADF JSON into a Factory.

Two layouts, mixed freely in one folder:
  * git-integration files: one JSON per object (pipeline/, trigger/, dataset/, linkedService/, dataflow/)
  * ARM export (ARMTemplateForFactory.json): resources[] of type Microsoft.DataFactory/factories/<kind>

Secrets never reach the model: SecureString values, Key Vault secret references' values and any key that looks like a credential are
dropped here (the Key Vault secret *name* is kept so the report can say what to set).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Dataset, Factory, LinkedService, Pipeline, Trigger, parse_activities

_SECRET_KEY = re.compile(r"(password|pwd|secret|accountkey|sastoken|sasuri|connectionstring|serviceprincipalkey|apikey|api_key|credential|encryptedcredential)", re.I)
_PARAM = re.compile(r"^\[parameters\('([^']+)'\)\]$")


def redact(o, counter: list[int]):
    """Copy of o with secrets removed; counter[0] counts the drops."""
    if isinstance(o, dict):
        t = o.get("type")
        if t == "SecureString":
            counter[0] += 1
            return {"redacted": "SecureString"}
        if t == "AzureKeyVaultSecret":
            counter[0] += 1
            return {"redacted": "AzureKeyVaultSecret", "secretName": o.get("secretName"), "store": (o.get("store") or {}).get("referenceName")}
        out = {}
        for k, v in o.items():
            if _SECRET_KEY.search(k) and isinstance(v, str) and v:
                counter[0] += 1
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, counter)
        return out
    if isinstance(o, list):
        return [redact(v, counter) for v in o]
    return o


class _Arm:
    """Resolve [parameters('x')] strings from the template's own default values."""

    def __init__(self, template: dict, factory: Factory, source: str):
        self.params = {k: (v or {}).get("defaultValue") for k, v in (template.get("parameters") or {}).items()}
        self.has_default = {k for k, v in (template.get("parameters") or {}).items() if isinstance(v, dict) and "defaultValue" in v}
        self.factory, self.source, self.unresolved = factory, source, set()

    def resolve(self, o):
        if isinstance(o, str):
            m = _PARAM.match(o)
            if m:
                p = m.group(1)
                if p in self.has_default:
                    return self.resolve(self.params[p])
                self.unresolved.add(p)
                return o
            if o.startswith("[") and o.endswith("]") and not o.startswith("[["):
                self.unresolved.add(o)
            return o
        if isinstance(o, dict):
            return {k: self.resolve(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.resolve(v) for v in o]
        return o


def _object_name(raw) -> str:
    raw = str(raw or "")
    m = re.search(r"'/([^']+)'\)\]$", raw)
    if m:
        return m.group(1)
    return raw.rsplit("/", 1)[-1]


def _add_pipeline(f: Factory, name: str, props: dict, source: str) -> None:
    notes = []
    params, variables = props.get("parameters"), props.get("variables")
    if isinstance(params, str):
        notes.append(f"pipeline parameters are the deploy-time value {params} and could not be resolved")
        params = {}
    if isinstance(variables, str):
        notes.append(f"pipeline variables are the deploy-time value {variables} and could not be resolved")
        variables = {}
    acts = props.get("activities")
    if isinstance(acts, str):
        notes.append(f"activities are the deploy-time value {acts} and could not be resolved")
    folder = (props.get("folder") or {}).get("name", "") if isinstance(props.get("folder"), dict) else ""
    f.pipelines[name] = Pipeline(
        name=name, parameters=params or {}, variables=variables or {}, activities=parse_activities(acts),
        folder=folder, description=props.get("description", "") or "", concurrency=props.get("concurrency"), notes=notes)


def _add_trigger(f: Factory, name: str, props: dict) -> None:
    refs = []
    for p in (props.get("pipelines") or ([props["pipeline"]] if isinstance(props.get("pipeline"), dict) else [])):
        if isinstance(p, dict):
            ref = (p.get("pipelineReference") or {}).get("referenceName")
            if ref:
                refs.append({"name": ref, "parameters": p.get("parameters") or {}})
    f.triggers[name] = Trigger(name=name, type=props.get("type", ""), state=props.get("runtimeState", ""),
                               pipelines=refs, props=props.get("typeProperties") or {})


def _add_linked_service(f: Factory, name: str, props: dict) -> None:
    n = [0]
    tp = redact(props.get("typeProperties") if isinstance(props.get("typeProperties"), dict) else {}, n)
    via = (props.get("connectVia") or {}).get("referenceName", "") if isinstance(props.get("connectVia"), dict) else ""
    f.linked_services[name] = LinkedService(name=name, type=props.get("type", ""), props=tp, connect_via=via, redacted=n[0])


def _add_dataset(f: Factory, name: str, props: dict) -> None:
    ls = (props.get("linkedServiceName") or {}).get("referenceName", "") if isinstance(props.get("linkedServiceName"), dict) else ""
    n = [0]
    tp = redact(props.get("typeProperties") if isinstance(props.get("typeProperties"), dict) else {}, n)
    f.datasets[name] = Dataset(name=name, type=props.get("type", ""), linked_service=ls, props=tp, parameters=props.get("parameters") or {})


def _native(f: Factory, d: dict, hint: str, source: str) -> bool:
    props, name = d.get("properties"), d.get("name")
    if not isinstance(props, dict) or not name:
        return False
    ptype = str(props.get("type", ""))
    if hint == "pipeline" or ("activities" in props and not hint):
        _add_pipeline(f, name, props, source)
    elif hint == "trigger" or ptype.endswith("Trigger"):
        _add_trigger(f, name, props)
    elif hint == "dataflow" or ptype in ("MappingDataFlow", "WranglingDataFlow", "Flowlet"):
        f.data_flows[name] = ptype
        f.data_flow_defs[name] = props.get("typeProperties") or {}
    elif hint == "dataset" or (not hint and "linkedServiceName" in props and "activities" not in props):
        _add_dataset(f, name, props)
    elif hint == "linkedservice" or (not hint and "typeProperties" in props):
        _add_linked_service(f, name, props)
    else:
        return False
    return True


def load(path: str | Path) -> Factory:
    root = Path(path)
    files = [root] if root.is_file() else sorted(root.rglob("*.json"))
    f = Factory()
    for p in files:
        if p.name == "manifest.json" or "node_modules" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            f.warnings.append(f"{p}: not readable as JSON ({e})")
            continue
        if not isinstance(d, dict):
            continue
        if isinstance(d.get("resources"), list):
            arm = _Arm(d, f, str(p))
            for r in d["resources"]:
                if not isinstance(r, dict):
                    continue
                kind = str(r.get("type", "")).rsplit("/", 1)[-1].lower()
                name, props = _object_name(r.get("name")), arm.resolve(r.get("properties") or {})
                if not isinstance(props, dict):
                    continue
                if kind == "pipelines":
                    _add_pipeline(f, name, props, str(p))
                elif kind == "triggers":
                    _add_trigger(f, name, props)
                elif kind == "datasets":
                    _add_dataset(f, name, props)
                elif kind == "linkedservices":
                    _add_linked_service(f, name, props)
                elif kind == "dataflows":
                    f.data_flows[name] = str(props.get("type", ""))
                    f.data_flow_defs[name] = props.get("typeProperties") or {}
            if arm.unresolved:
                f.warnings.append(f"{p.name}: {len(arm.unresolved)} deploy-time value(s) have no default in the template and were left as written: {', '.join(sorted(arm.unresolved)[:8])}")
            continue
        hint = {"pipeline": "pipeline", "trigger": "trigger", "dataset": "dataset", "linkedservice": "linkedservice", "dataflow": "dataflow"}.get(p.parent.name.lower(), "")
        _native(f, d, hint, str(p))
    return f
