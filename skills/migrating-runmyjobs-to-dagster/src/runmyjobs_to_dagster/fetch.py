"""Pull definitions from a live RunMyJobs server through the built-in REST interface.

Documented (2025.4 docs): <base>/api-rest/list/{type}?check=export, .../object/{type}/{Partition.Name}
(XML), Basic auth. UNVERIFIED against a live server: the shape of the list response (parsed
tolerantly below) and whether SubmitFrame/JobGroup are listable. On RunMyJobs SaaS the built-in
interface is only reachable from inside RunMyJobs; use an exported CAR instead.
"""
from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from defusedxml import ElementTree as ET

DEFAULT_TYPES = ("JobDefinition", "Queue", "TimeWindow", "SubmitFrame", "JobGroup")


def _get(url: str, user: str, password: str, opener=urllib.request.urlopen) -> bytes:
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()})
    with opener(req, timeout=60) as r:
        return r.read()


def parse_list(data: bytes) -> list[str]:
    """Business keys (Partition.Name) found in a list response, tolerant of shape."""
    keys: list[str] = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return keys
    for el in root.iter():
        part, name = el.get("partition") or el.get("Partition"), el.get("name") or el.get("Name")
        if part and name:
            keys.append(f"{part}.{name}")
        elif el.get("path"):
            keys.append(el.get("path"))
        elif not len(el) and el.text and re.fullmatch(r"[A-Za-z0-9_]+\.[A-Za-z0-9_.\-]+", el.text.strip()):
            keys.append(el.text.strip())
    return list(dict.fromkeys(keys))


def fetch(base: str, user: str, password: str, out_dir: Path, types=DEFAULT_TYPES,
          search: str = "", opener=urllib.request.urlopen) -> list[str]:
    """Write one XML file per object into out_dir (loadable with load_car). Returns warnings."""
    base = base.rstrip("/")
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    for t in types:
        q = {"check": "export"}
        if search:
            q["search"] = search
        try:
            listing = _get(f"{base}/api-rest/list/{t}?{urllib.parse.urlencode(q)}", user, password, opener)
        except urllib.error.HTTPError as e:
            warnings.append(f"{t}: list failed with HTTP {e.code}")
            continue
        keys = parse_list(listing)
        if not keys:
            warnings.append(f"{t}: no objects recognised in list response (first bytes: {listing[:120]!r})")
        for k in keys:
            try:
                data = _get(f"{base}/api-rest/object/{t}/{urllib.parse.quote(k)}", user, password, opener)
            except urllib.error.HTTPError as e:
                warnings.append(f"{t} {k}: HTTP {e.code}")
                continue
            (out_dir / f"{t}__{re.sub(r'[^A-Za-z0-9_.-]', '_', k)}.xml").write_bytes(data)
    return warnings


def fetch_ext(base: str, out_dir: Path, user: str = "", password: str = "", api_key: str = "",
              types=("JobDefinition", "Queue", "EventDefinition"), flt: str = "",
              opener=urllib.request.urlopen) -> list[str]:
    """Inbound REST Extension (swagger v1.1.0.0): GET <base>/<Type>?limit=500 returns {"results": [...]}.
    base is .../api-extension/External/REDWOOD/Redwood_RestService/rest/v1. Auth: X-API-KEY, else Basic.
    Writes <Type>.json per type. UNVERIFIED against a live server."""
    base = base.rstrip("/")
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
    else:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    warnings: list[str] = []
    for t in types:
        q = {"limit": 500, **({"filter": flt} if flt else {})}
        req = urllib.request.Request(f"{base}/{t}?{urllib.parse.urlencode(q)}", headers=headers)
        try:
            with opener(req, timeout=60) as r:
                (out_dir / f"{t}.json").write_bytes(r.read())
        except urllib.error.HTTPError as e:
            warnings.append(f"{t}: HTTP {e.code}")
    return warnings


def fetch_dbt_jobs(account_id: str, token: str, out_file: Path, host: str = "cloud.getdbt.com", opener=urllib.request.urlopen) -> int:
    """List the account's dbt Cloud jobs (GET /api/v2/accounts/{id}/jobs/, paged) and write [{id, name, ...}] to out_file.
    The converter turns a dbt Cloud job id into the mirrored asset's key from this list. UNVERIFIED against a live account."""
    import json
    jobs: list[dict] = []
    offset = 0
    while True:
        req = urllib.request.Request(f"https://{host}/api/v2/accounts/{account_id}/jobs/?limit=100&offset={offset}",
                                     headers={"Authorization": f"Token {token}", "Accept": "application/json"})
        with opener(req, timeout=60) as r:
            body = json.loads(r.read())
        page = body.get("data") or []
        jobs.extend({k: j.get(k) for k in ("id", "name", "project_id", "environment_id", "job_type", "state") if k in j} for j in page)
        total = ((body.get("extra") or {}).get("pagination") or {}).get("total_count")
        offset += len(page)
        if not page or (total is not None and offset >= total) or (total is None and len(page) < 100):
            break
    out_file.write_text(json.dumps(jobs, indent=2) + "\n")
    return len(jobs)
