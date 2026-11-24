"""Recognise process definitions that call dbt Cloud or HVR, from their command text.

HEURISTIC and UNVERIFIED: no real RunMyJobs definition of these steps has been seen. The patterns
cover the usual ways a script would do it (dbt Cloud REST `.../jobs/<id>/run/`, a `dbt cloud` CLI or
job_id variable; `hvrrefresh -c <channel>`). A match is reported as "needs review", never silently trusted.
"""
from __future__ import annotations

import re

_DBT_URL = re.compile(r"/jobs/(\d+)/run", re.I)
_DBT_VAR = re.compile(r"\bjob[_-]?id\b\W{0,6}(\d{4,})", re.I)
_HVR = re.compile(r"\bhvr\w*refresh\b|\bhvrrefresh\b|\bhvr_refresh\b", re.I)
_HVR_CHANNEL = re.compile(r"(?:-c|--channel)[ =]+([\w.\-]+)")


def _param(params: dict, *patterns: str):
    for k, v in (params or {}).items():
        if any(re.search(pt, k, re.I) for pt in patterns) and str(v or "").strip():
            return str(v).strip()
    return None


def detect(command: str, name: str = "", params: dict | None = None) -> dict | None:
    """By the called definition's NAME first (a typical convention: JD_dbt_Cloud_Job_Trigger, JD_HVR_Refresh_CMD,
    JD_LDP_Refresh_CMD), taking the job id or channel from the call's parameters; then by command text."""
    if re.search(r"dbt[_ ]?cloud[_ ]?job[_ ]?trigger", name, re.I):
        jid = _param(params, r"job[_-]?id", r"^job$")
        return {"kind": "dbt_cloud", "job_id": int(jid) if jid and jid.isdigit() else None, "by": "name"}
    if re.search(r"(hvr|ldp)[_ ]?refresh", name, re.I):
        return {"kind": "hvr_refresh", "channel": _param(params, r"channel"), "by": "name"}
    if not command:
        return None
    if re.search(r"dbt[ _.-]?cloud|getdbt\.com|dbt\.com/api", command, re.I) or _DBT_URL.search(command):
        m = _DBT_URL.search(command) or _DBT_VAR.search(command)
        return {"kind": "dbt_cloud", "job_id": int(m.group(1)) if m else None}
    if _HVR.search(command):
        m = _HVR_CHANNEL.search(command)
        return {"kind": "hvr_refresh", "channel": m.group(1) if m else None}
    return None


def mirrored_asset_key(job_name: str) -> str:
    """The asset key EnrichedDbtCloudWorkspaceComponent gives a mirrored dbt Cloud job (its `_sanitize_job_name`, copied from registry 0.10.173)."""
    out = re.sub(r"[^A-Za-z0-9_]+", "_", (job_name or "").strip())
    if not out or not (out[0].isalpha() or out[0] == "_"):
        out = f"job_{out}" if out else "job"
    return out


def load_dbt_jobs(path) -> dict[int, str]:
    """{job id: job name} from fetch-dbt-jobs output (a list of {id, name}) or a plain {id: name} mapping."""
    import json
    data = json.loads(open(path).read())
    if isinstance(data, dict):
        data = data.get("data", [{"id": k, "name": v} for k, v in data.items()])
    return {int(j["id"]): str(j.get("name") or f"job_{j['id']}") for j in data if j.get("id") is not None}


def load_dbt_model_jobs(path) -> set[int]:
    """Job ids marked `"models": true` in the job list: those jobs also get per-model assets (the rest are one asset per job)."""
    import json
    data = json.loads(open(path).read())
    if isinstance(data, dict):
        data = data.get("data", [])
    return {int(j["id"]) for j in data if isinstance(j, dict) and j.get("models") and j.get("id") is not None}
