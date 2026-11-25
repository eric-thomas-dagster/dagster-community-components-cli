"""Per-pipeline questions the converter cannot answer from an export, kept in the manifest so `report` reproduces them."""
from __future__ import annotations

import re


def build_questions(est, res) -> dict:
    q: dict[str, list[str]] = {}
    for title, p in est.pipelines.items():
        meta = res.meta.get("pipelines", {}).get(title)
        if not meta:
            continue
        qs = []
        if meta["streaming"]:
            qs.append("This pipeline starts from a continuous origin (" + ", ".join(meta["streaming"]) + "). A Dagster run is a batch: "
                      "(a) keep it running in StreamSets and observe it with the registry StreamSetsWorkspaceComponent (tier 1), "
                      "(b) replace it with a micro-batch asset on a schedule, or (c) move the source to a streaming platform. Which?")
        else:
            qs.append("What starts this pipeline today (a Control Hub schedule, an external scheduler, a manual start) and how often? "
                      "The export holds no schedule, so no Dagster schedule was generated.")
        if meta["secrets"]:
            qs.append("Credential parameters were removed from the export: set " + ", ".join(f"SDC_SECRET_{k}" for k in meta["secrets"])
                      + " where the code location runs (or tell me the secret store).")
        if any(s.cls in ("JdbcDTarget", "TableJdbcDSource", "JdbcQueryDExecutor") for s in p.stages):
            qs.append("JDBC stages derive a SQLAlchemy URL from the JDBC URL (MySQL, MariaDB, PostgreSQL, Redshift). For any other database set "
                      "SDC_SQLALCHEMY_URL_<STAGE> to the full SQLAlchemy URL.")
        if any(re.search(r"crypt|hash|mask", s.cls, re.I) for s in p.stages):
            qs.append("This pipeline encrypts, decrypts, hashes or masks fields. Converted Encrypt and Decrypt Fields stages read and write StreamSets-compatible ciphertext (user-supplied keys "
                      "were checked against Data Collector 6.4.1; AWS KMS was not). Tell me where the key lives (the export drops it: SDC_SECRET_<STAGE>_KEY, or a KMS key ARN with AWS credentials), "
                      "whether date or time fields are encrypted (not supported yet), and whether old hashes must still match new ones (hashing matches Data Collector for in-place and target-field hashes).")
        if any(s.cls == "GeolocationDProcessor" for s in p.stages):
            qs.append("Geolocation uses a MaxMind GeoIP2/GeoLite2 database. Does the customer hold a MaxMind account and license key (GeoLite2 downloads require one), and who refreshes the .mmdb file? "
                      "The converted code reads a file path (or SDC_GEOIP_DB); it does not download or update the database.")
        if meta["stubs"]:
            qs.append("Stages that were not translated: " + "; ".join(f"{i} ({w.split(';')[0].split('.')[0]})" for i, w in meta["stubs"]) + ". Which of these must be rebuilt by hand, and which are retired?")
        q[title] = qs
    return q


def render_questions(q: dict) -> list[str]:
    if not q:
        return []
    lines = ["", "## Needs your input", ""]
    for title, items in q.items():
        lines.append(f"### {title}")
        lines += [f"- [ ] {x}" for x in items]
        lines.append("")
    return lines
