"""The sandbox every execution runs in: the same working directory layout, connections, local stand-in services and recorders for all three routes (original DAG in Airflow, converter output, rewrite).

    from sandbox import Sandbox
    sb = Sandbox(root, connections={...}, variables={...}); sb.install()        # BEFORE importing airflow or dagster
    ... run the DAG or the Dagster job ...
    observed = sb.snapshot()                                                     # what the run left behind and what it sent

What is stood in (all in-process, no network, no listening port):
  files       a scratch working directory (`<root>/cwd`) and a scratch HOME; the snapshot lists every file (relative path, sha256, size)
  sqlite      any `*.db`, `*.sqlite`, `*.sqlite3` file under `<root>/data`, the cwd or HOME: the snapshot lists every table's rows (sorted, hashed)
  http        `requests.Session.send` and `urllib.request` are replaced by a recorder that answers 200 `{"ok": true}`; the snapshot lists method, url, body, content-type (never an Authorization header)
  smtp        `smtplib.SMTP` and `SMTP_SSL` are replaced by a recorder; the snapshot lists sender, recipients, subject, body
  s3          moto's `mock_aws` is started when moto is installed; the snapshot lists every bucket, key and content hash
  time        `time.sleep` can be capped (off by default: a cap stops execution timeouts and sensor timeouts from firing, so it changes what a DAG does); both sides get the same cap
Connections are exported as `AIRFLOW_CONN_<ID>` and Variables as `AIRFLOW_VAR_<NAME>`, which is what Airflow reads and what the converter and the rewrite brief say to read.
Nothing here holds a real credential; the AWS values are the placeholders moto requires.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", "logs", ".venv", "dagster_home", "airflow_home", ".dagster"}
EXCLUDE_SUFFIX = {".pyc", ".pyo"}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _text(b: Any) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    if isinstance(b, (bytes, bytearray)):
        try:
            return bytes(b).decode("utf-8")
        except UnicodeDecodeError:
            return "sha256:" + _sha(bytes(b))
    if hasattr(b, "read"):
        return ""
    return str(b)


class Sandbox:
    def __init__(self, root: Path, connections: dict[str, str] | None = None, variables: dict[str, str] | None = None, sleep_cap: float | None = None, moto: bool = True) -> None:
        self.root = Path(root).resolve()
        self.cwd = self.root / "cwd"
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.tmp = self.root / "tmp"
        self.connections = connections or {}
        self.variables = variables or {}
        self.sleep_cap = sleep_cap
        self.moto_on = moto
        self.http: list[dict[str, Any]] = []
        self.smtp: list[dict[str, Any]] = []
        self._moto = None
        self.notes: list[str] = []

    # ---- install -------------------------------------------------------------------------------------------------------------------------
    def install(self) -> "Sandbox":
        for d in (self.cwd, self.home, self.data, self.tmp, self.root / "airflow_home", self.root / "dagster_home"):
            d.mkdir(parents=True, exist_ok=True)
        env = os.environ
        env.update({"HOME": str(self.home), "TMPDIR": str(self.tmp), "TZ": "UTC", "PYTHONHASHSEED": "0", "AIRFLOW_HOME": str(self.root / "airflow_home"), "DAGSTER_HOME": str(self.root / "dagster_home"),
                    "AIRFLOW__CORE__LOAD_EXAMPLES": "False", "AWS_ACCESS_KEY_ID": "eval-placeholder", "AWS_SECRET_ACCESS_KEY": "eval-placeholder",
                    "AWS_DEFAULT_REGION": "us-east-1", "AWS_SESSION_TOKEN": "eval-placeholder", "NO_PROXY": "*", "DO_NOT_TRACK": "1", "SCARF_ANALYTICS": "false"})
        for k in [k for k in env if k.startswith(("AIRFLOW_CONN_", "AIRFLOW_VAR_"))]:
            del env[k]
        defaults = {"sqlite_default": f"sqlite:///{self.data / 'app.db'}", "http_default": "http://eval-http.test", "smtp_default": "smtp://eval-smtp.test:25", "aws_default": "aws://"}
        for cid, uri in {**defaults, **self.connections}.items():
            env["AIRFLOW_CONN_" + cid.upper()] = uri
        for name, v in self.variables.items():
            env["AIRFLOW_VAR_" + name.upper()] = v
        tempfile.tempdir = str(self.tmp)
        os.chdir(self.cwd)
        self._patch_sleep()
        self._patch_tmpdirs()
        self._patch_http()
        self._patch_smtp()
        self._start_moto()
        return self

    def _patch_tmpdirs(self) -> None:
        """Airflow's BashOperator runs the command in a temporary directory (`airflowtmp*`) that is deleted afterwards, so a file it writes with a relative path is gone when the task ends.
        Copy such a directory to `<root>/ephemeral` before it is deleted so the effect is observable; the snapshot lists those files as if they were in the working directory (noted)."""
        sb = self
        orig = tempfile.TemporaryDirectory.cleanup

        def cleanup(td: Any, *a: Any, **k: Any) -> Any:
            try:
                if os.path.basename(td.name).startswith("airflowtmp") and os.path.isdir(td.name):
                    shutil.copytree(td.name, sb.root / "ephemeral", dirs_exist_ok=True, ignore_dangling_symlinks=True)
            except Exception:  # noqa: BLE001
                pass
            return orig(td, *a, **k)
        tempfile.TemporaryDirectory.cleanup = cleanup                                # type: ignore[method-assign]

    def _patch_sleep(self) -> None:
        if self.sleep_cap is None:
            return
        real, cap = time.sleep, self.sleep_cap
        time.sleep = lambda s: real(min(float(s), cap))                              # type: ignore[assignment]

    def _patch_http(self) -> None:
        sb = self
        try:
            import requests
            from requests.models import Response

            def send(session: Any, request: Any, **kw: Any) -> Any:
                body = request.body
                sb.http.append({"method": request.method, "url": request.url, "body": _text(body), "content_type": request.headers.get("Content-Type", "")})
                r = Response()
                r.status_code = 200
                r._content = b'{"ok": true}'
                r.headers["Content-Type"] = "application/json"
                r.url = request.url
                r.request = request
                r.encoding = "utf-8"
                return r
            requests.Session.send = send                                             # type: ignore[method-assign]
        except ImportError:
            self.notes.append("requests not installed: HTTP via requests not recorded")
        import urllib.request as ur

        def open_(opener: Any, fullurl: Any, data: Any = None, timeout: Any = None) -> Any:
            url = fullurl if isinstance(fullurl, str) else fullurl.full_url
            method = "GET" if data is None and not hasattr(fullurl, "data") else "POST"
            if not isinstance(fullurl, str):
                method = fullurl.get_method()
                data = data if data is not None else fullurl.data
            sb.http.append({"method": method, "url": url, "body": _text(data), "content_type": "" if isinstance(fullurl, str) else fullurl.headers.get("Content-type", "")})

            class R(io.BytesIO):
                status = 200
                headers = {"Content-Type": "application/json"}

                def getcode(self) -> int:
                    return 200

                def geturl(self) -> str:
                    return url

                def info(self) -> Any:
                    return self.headers
            return R(b'{"ok": true}')
        ur.OpenerDirector.open = open_                                               # type: ignore[method-assign]

    def _patch_smtp(self) -> None:
        import email
        import smtplib
        sb = self

        class Rec:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def __enter__(self) -> "Rec":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def _noop(self, *a: Any, **k: Any) -> tuple[int, bytes]:
                return 250, b"ok"
            ehlo = helo = starttls = login = set_debuglevel = noop = quit = close = connect = _noop

            def sendmail(self, frm: str, to: Any, msg: Any, *a: Any, **k: Any) -> dict:
                m = email.message_from_string(msg) if isinstance(msg, str) else email.message_from_bytes(bytes(msg))
                body = ""
                for part in (m.walk() if m.is_multipart() else [m]):
                    if part.get_content_maintype() == "text":
                        body += (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                sb.smtp.append({"from": frm, "to": sorted([to] if isinstance(to, str) else list(to)), "subject": str(m.get("Subject", "")), "body": body.strip(), "attachments": sorted(p.get_filename() for p in m.walk() if p.get_filename())})
                return {}

            def send_message(self, msg: Any, from_addr: Any = None, to_addrs: Any = None, *a: Any, **k: Any) -> dict:
                to = to_addrs or [x.strip() for h in ("To", "Cc", "Bcc") for x in str(msg.get(h, "")).split(",") if x.strip()]
                return self.sendmail(from_addr or str(msg.get("From", "")), to, msg.as_string())
        smtplib.SMTP = Rec                                                           # type: ignore[misc,assignment]
        smtplib.SMTP_SSL = Rec                                                       # type: ignore[misc,assignment]

    def _start_moto(self) -> None:
        if not self.moto_on:
            return
        try:
            from moto import mock_aws
        except ImportError:
            self.notes.append("moto not installed: S3 not stood in")
            return
        self._moto = mock_aws()
        self._moto.start()

    # ---- snapshot ------------------------------------------------------------------------------------------------------------------------
    def _files(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for base, label in ((self.cwd, "cwd"), (self.home, "home"), (self.data, "data"), (self.root / "ephemeral", "cwd")):
            for p in sorted(base.rglob("*")):
                rel = p.relative_to(base)
                if p.is_dir() or any(x in EXCLUDE_DIRS for x in rel.parts) or p.suffix in EXCLUDE_SUFFIX or str(rel).startswith((".cache", ".config", ".local")):
                    continue
                try:
                    b = p.read_bytes()
                except OSError:
                    continue
                if label == "cwd" and base.name == "ephemeral" and f"cwd/{rel}" in out:
                    continue
                out[f"{label}/{rel}"] = {"via_task_tmpdir": base.name == "ephemeral", "sha": _sha(b), "size": len(b), "head": b[:200].decode("utf-8", "replace") if len(b) < 4096 and b.isascii() else None}
        return out

    def _sqlite(self, files: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for rel in files:
            if not rel.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            base = {"cwd": self.cwd, "home": self.home, "data": self.data}[rel.split("/", 1)[0]]
            path = base / rel.split("/", 1)[1]
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
                for t in tables:
                    rows = [list(map(repr, r)) for r in con.execute(f'select * from "{t}"')]
                    rows.sort()
                    out[f"{rel}:{t}"] = {"count": len(rows), "sha": _sha(json.dumps(rows).encode()), "rows": rows[:50]}
                con.close()
            except sqlite3.Error as e:
                out[f"{rel}:(unreadable)"] = {"count": -1, "sha": "error", "rows": [str(e)]}
        return out

    def _s3(self) -> dict[str, Any]:
        if self._moto is None:
            return {}
        out: dict[str, Any] = {}
        try:
            import boto3
            c = boto3.client("s3", region_name="us-east-1")
            for b in c.list_buckets().get("Buckets", []):
                keys = []
                for page in c.get_paginator("list_objects_v2").paginate(Bucket=b["Name"]):
                    keys += [o["Key"] for o in page.get("Contents", [])]
                if not keys:
                    out[f"{b['Name']}/"] = {"sha": "empty-bucket", "size": 0}
                for k in keys:
                    body = c.get_object(Bucket=b["Name"], Key=k)["Body"].read()
                    out[f"{b['Name']}/{k}"] = {"sha": _sha(body), "size": len(body)}
        except Exception as e:  # noqa: BLE001
            out["(snapshot error)"] = {"sha": type(e).__name__ + ": " + str(e)[:100], "size": -1}
        return out

    def snapshot(self) -> dict[str, Any]:
        files = self._files()
        def canon(r: dict[str, Any]) -> str:
            return re.sub(r"\s+", " ", json.dumps(r, sort_keys=True))
        http = sorted(self.http, key=canon)
        smtp = sorted(self.smtp, key=canon)
        sq = self._sqlite(files)
        files = {k: v for k, v in files.items() if not k.endswith((".db", ".sqlite", ".sqlite3", "-journal", "-wal", "-shm"))}      # a database file is compared by its rows, not its bytes
        return {"files": files, "sqlite": sq, "http": http, "smtp": smtp, "s3": self._s3(), "notes": self.notes}

    def stop(self) -> None:
        if self._moto is not None:
            try:
                self._moto.stop()
            except Exception:  # noqa: BLE001
                pass
