"""A small client for a local Data Collector's REST API, enough to preview a pipeline on known records and read each stage's output.

Used by tests/test_sdc_parity.py (skipped unless SDC_PARITY_URL points at a running Data Collector, e.g. streamsets/datacollector:3.18.1, which needs no activation).
"""
import json
import time
import uuid

import requests


class Sdc:
    def __init__(self, url="http://localhost:18632", auth=("admin", "admin")):
        """auth is admin/admin for a standalone engine; an engine run from a Control Hub deployment (which is how 6.x is activated) takes the Control Hub API credentials from
        SDC_PARITY_COMPONENT_ID and SDC_PARITY_TOKEN instead (never put them in a file)."""
        import os
        self.b = url.rstrip("/") + "/rest/v1"
        self.s = requests.Session()
        self.s.headers.update({"X-Requested-By": "sdc"})
        if os.environ.get("SDC_PARITY_TOKEN"):
            self.s.headers.update({"X-SS-App-Component-Id": os.environ["SDC_PARITY_COMPONENT_ID"], "X-SS-App-Auth-Token": os.environ["SDC_PARITY_TOKEN"]})
        else:
            self.s.auth = auth
        self._defs = None

    def defs(self):
        if self._defs is None:
            self._defs = {x["name"].split("_")[-1]: x for x in self.s.get(f"{self.b}/definitions").json()["stages"]}
        return self._defs

    def svc_defs(self):
        if not hasattr(self, "_svc"):
            self._svc = {x["provides"]: x for x in self.s.get(f"{self.b}/definitions").json()["services"]}
        return self._svc

    def stage(self, cls, instance, conf=None, inputs=(), outputs=(), events=(), service_conf=None):
        """A stage instance with every property at its default, then `conf` on top (names as in a pipeline export)."""
        sd = self.defs()[cls]
        cfg = {c["name"]: c.get("defaultValue") for c in sd["configDefinitions"]}
        for k in (conf or {}):
            if k not in cfg:
                raise KeyError(f"{cls} has no property {k!r}; it has {sorted(cfg)}")
        cfg.update(conf or {})
        services = []
        for sv in sd.get("services") or []:
            d = self.svc_defs()[sv["service"]]
            sc = {c["name"]: c.get("defaultValue") for c in d["configDefinitions"]}
            sc.update(sv.get("configuration") or {})
            sc.update(service_conf or {})
            services.append({"service": sv["service"], "serviceVersion": d["version"], "configuration": [{"name": k, "value": v} for k, v in sc.items()]})
        return {"instanceName": instance, "library": sd["library"], "stageName": sd["name"], "stageVersion": str(sd["version"]),
                "configuration": [{"name": k, "value": v} for k, v in cfg.items()], "uiInfo": {"label": instance, "xPos": 60, "yPos": 50, "stageType": sd["type"]},
                "inputLanes": list(inputs), "outputLanes": list(outputs), "eventLanes": list(events), "services": services}

    def preview(self, stages, batch_size=100, timeout=60000):
        """Create a throw-away pipeline from the stage list, preview one batch, return {instanceName: {"output": {lane: [records]}, "errors": [...], "events": [...]}}."""
        pid = "parity_" + uuid.uuid4().hex[:8]
        r = self.s.put(f"{self.b}/pipeline/{pid}", params={"pipelineType": "STANDALONE", "description": "parity"})
        r.raise_for_status()
        cfg = r.json()
        cfg["stages"] = stages
        cfg["metadata"] = cfg.get("metadata") or {"labels": []}        # 6.x previews fail with a NullPointerException when the pipeline metadata is null
        cfg["errorStage"] = self.stage("ToErrorNullDTarget", "ErrorStage")
        cfg["errorStage"]["uiInfo"]["stageType"] = "TARGET"
        r = self.s.post(f"{self.b}/pipeline/{pid}", json=cfg)
        r.raise_for_status()
        saved = r.json()
        if saved.get("issues", {}).get("issueCount"):
            raise RuntimeError("pipeline has issues: " + json.dumps(saved["issues"])[:800])
        r = self.s.post(f"{self.b}/pipeline/{pid}/preview", params={"batchSize": batch_size, "batches": 1, "skipTargets": "false", "skipLifecycleEvents": "true", "timeout": timeout},
                        json=[])
        r.raise_for_status()
        prev = r.json()["previewerId"]
        t0 = time.time()
        while True:
            st = self.s.get(f"{self.b}/pipeline/{pid}/preview/{prev}/status").json()
            if st["status"] in ("FINISHED", "INVALID", "RUN_ERROR", "START_ERROR", "TIMED_OUT", "CANCELLED", "VALIDATION_ERROR"):
                break
            if time.time() - t0 > 90:
                raise TimeoutError("preview did not finish")
            time.sleep(0.4)
        res = self.s.get(f"{self.b}/pipeline/{pid}/preview/{prev}").json()
        self.s.delete(f"{self.b}/pipeline/{pid}")
        if res.get("status") != "FINISHED":
            raise RuntimeError(f"preview {res.get('status')}: {json.dumps(res.get('issues'))[:600]}")
        out = {}
        for sout in res["batchesOutput"][0]:
            out[sout["instanceName"]] = {
                "output": {lane: [plain(rec) for rec in recs] for lane, recs in sout.get("output", {}).items()},
                "errors": [plain(rec) for rec in sout.get("errorRecords", [])],
                "events": [plain(rec) for rec in sout.get("eventRecords", [])]}
        return out


_INT = {"BYTE", "SHORT", "INTEGER", "LONG", "DATE", "DATETIME", "TIME"}
_FLT = {"FLOAT", "DOUBLE", "DECIMAL"}


def _items(v):
    """(name, field) pairs: a MAP is a dict, a LIST_MAP an ordered list of fields whose sqpath ends in the name."""
    if isinstance(v, dict):
        return list(v.items())
    return [(f["sqpath"].rsplit("/", 1)[-1].strip("'\""), f) for f in (v or [])]


def _val(f):
    """SDC's typed JSON field to (plain value, type name or nested types)."""
    if f is None:
        return None
    t, v = f.get("type"), f.get("value")
    if t in ("MAP", "LIST_MAP"):
        return {k: _val(x) for k, x in _items(v)}
    if t == "LIST":
        return [_val(x) for x in (v or [])]
    if v is None:
        return None
    if t in _INT:
        return int(v)
    if t in _FLT:
        return float(v)
    return v


def _types(f):
    if f is None:
        return None
    t, v = f.get("type"), f.get("value")
    if t in ("MAP", "LIST_MAP"):
        return {k: _types(x) for k, x in _items(v)}
    if t == "LIST":
        return [_types(x) for x in (v or [])]
    return t


def plain(rec):
    """{"value": plain value, "types": SDC type of every field, "attributes": header attributes, "error": message}."""
    h = rec.get("header", {})
    return {"value": _val(rec.get("value")), "types": _types(rec.get("value")), "attributes": dict(h.get("values") or {}), "error": h.get("errorMessage")}
