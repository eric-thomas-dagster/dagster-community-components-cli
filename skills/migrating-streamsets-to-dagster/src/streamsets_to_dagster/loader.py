"""Read StreamSets pipeline exports into an Estate.

Accepted, mixed freely (files, folders, .zip archives from Control Hub or Data Collector):
  * a Data Collector export:  {"pipelineConfig": {...}, "pipelineRules": {...}, "libraryDefinitions": {...}}
  * a bare pipeline config:   {"pipelineId": ..., "stages": [...], ...}
  * a list of either, or a Control Hub export whose pipeline objects nest the config under "pipelineConfig"

Credentials never reach the model: runtime constants whose names look like credentials and any literal (non-expression) value of a
credential-looking stage property are dropped here; `${NAME}` references are kept because they only point at a constant.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from .model import Estate, Pipeline, Stage

_SECRET = re.compile(r"(passw|pwd|secret|token|apikey|api_key|accesskey|privatekey|credential|keystore|passphrase|sas|connectionstring|(^|[_.])key$)", re.I)
_EL_ONLY = re.compile(r"^\s*\$\{[^}]*\}\s*$")


def is_secret_name(name: str) -> bool:
    return bool(_SECRET.search(name or ""))


def _role(stage_name: str, cls: str) -> str:
    if cls.endswith("DSource") or cls.endswith("Origin") or "_origin_" in stage_name:
        return "origin"
    if cls.endswith("DProcessor") or cls.endswith("Processor") or "_processor_" in stage_name:
        return "processor"
    if cls.endswith("DTarget") or cls.endswith("Destination") or "_destination_" in stage_name or "_target_" in stage_name:
        return "destination"
    if cls.endswith("DExecutor") or cls.endswith("Executor") or "_executor_" in stage_name:
        return "executor"
    return "unknown"


def _conf(stage: dict) -> tuple[dict, int]:
    """Stage properties by name. Newer Data Collectors keep data-format settings in `services[].configuration`; those are merged in."""
    out, dropped = {}, 0
    entries = list(stage.get("configuration") or [])
    for svc in stage.get("services") or []:
        entries += [c for c in svc.get("configuration") or [] if c.get("name") != "displayFormats"]
    for c in entries:
        name, val = c.get("name", ""), c.get("value")
        if is_secret_name(name.rsplit(".", 1)[-1]) and isinstance(val, str) and val and not _EL_ONLY.match(val):
            out[name], dropped = "<redacted>", dropped + 1
        else:
            out[name] = val
    return out, dropped


def parse_pipeline(cfg: dict, source: str, product_hint: str = "") -> Pipeline:
    top = {c.get("name"): c.get("value") for c in cfg.get("configuration") or []}
    consts, secrets = {}, []
    for kv in top.get("constants") or []:
        if not isinstance(kv, dict):
            continue
        k, v = str(kv.get("key", "")), kv.get("value")
        if is_secret_name(k):
            secrets.append(k)
            consts[k] = ""
        else:
            consts[k] = "" if v is None else str(v)
    stages = []
    for s in cfg.get("stages") or []:
        sn = s.get("stageName", "")
        cls = sn.rsplit("_", 1)[-1] if "_" in sn else sn
        conf, n = _conf(s)
        stages.append(Stage(
            instance=s.get("instanceName", ""), cls=cls, stage_name=sn, library=s.get("library", ""), role=_role(sn, cls),
            inputs=list(s.get("inputLanes") or []), outputs=list(s.get("outputLanes") or []), events=list(s.get("eventLanes") or []),
            conf=conf, on_error=conf.get("stageOnRecordError") or "", redacted=n,
            preconditions=[p for p in (conf.get("stageRecordPreconditions") or []) if isinstance(p, str)]))
    mode = str(top.get("executionMode") or "")
    libs = " ".join(s.library for s in stages)
    product = product_hint or ("transformer" if "spark" in libs.lower() and "datacollector" not in libs.lower() else "datacollector")

    def short(key: str) -> str:
        v = top.get(key)
        parts = str(v).split("::") if v else []
        return parts[1].rsplit("_", 1)[-1] if len(parts) > 1 else ""
    title = cfg.get("title") or cfg.get("pipelineId") or Path(source).stem
    return Pipeline(
        id=str(cfg.get("pipelineId") or title), title=title, description=cfg.get("description") or "", product=product,
        execution_mode=mode, constants=consts, secret_constants=secrets, stages=stages,
        error_stage=short("badRecordsHandling"), start_event_stage=short("startEventStage"), stop_event_stage=short("stopEventStage"),
        source=source, notes=[])


def _walk(o, source: str, est: Estate) -> None:
    if isinstance(o, list):
        for x in o:
            _walk(x, source, est)
        return
    if not isinstance(o, dict):
        return
    cfg = o.get("pipelineConfig")
    if isinstance(cfg, dict) and isinstance(cfg.get("stages"), list):
        _add(parse_pipeline(cfg, source), est)
    elif isinstance(o.get("stages"), list) and ("pipelineId" in o or "title" in o or "configuration" in o):
        _add(parse_pipeline(o, source), est)
    else:
        for k in ("pipelineEnvelope", "pipelines", "pipeline"):
            if k in o:
                _walk(o[k], source, est)


def _add(p: Pipeline, est: Estate) -> None:
    name = p.title
    if name in est.pipelines:
        n = 2
        while f"{name} ({n})" in est.pipelines:
            n += 1
        name = f"{name} ({n})"
        est.warnings.append(f"duplicate pipeline title {p.title!r} in {p.source}: kept as {name!r}")
        p.title = name
    est.pipelines[name] = p


def load(path: str | Path) -> Estate:
    est, root = Estate(), Path(path)

    def one(label: str, data: bytes):
        try:
            _walk(json.loads(data.decode("utf-8-sig")), label, est)
        except (ValueError, UnicodeDecodeError):
            est.warnings.append(f"{label}: not valid JSON, skipped")

    def file(p: Path, base: Path):
        label = str(p.relative_to(base)) if base != p else p.name
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for n in sorted(z.namelist()):
                    if n.lower().endswith(".json"):
                        one(f"{label}!{n}", z.read(n))
        elif p.suffix.lower() == ".json":
            one(label, p.read_bytes())

    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                file(p, root)
    else:
        file(root, root)
    return est
