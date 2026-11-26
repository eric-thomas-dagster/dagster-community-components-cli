"""Run a Dagster project's DAG in the same sandbox the evaluation harness uses, and print what happened. For the rewrite worker's own checks.

    python tools/run_in_sandbox.py --project out/<project> --dag-id <dag_id> [--module <pkg>.defs.<module>] [--connections-from spec.json]

Runs in the interpreter you start it with (use the project's: `out/<project>/.venv/bin/python`). The sandbox is a scratch directory used as the working directory, with local stand-ins:
HTTP requests (answered 200 `{"ok": true}`, and recorded), SMTP (recorded), S3 (moto), SQLite files, and real `time.sleep`. Connections come from `AIRFLOW_CONN_<ID>` and Variables from
`AIRFLOW_VAR_<NAME>` environment variables, exactly as the harness sets them; the defaults and any connection the spec names are exported for you. This is NOT the acceptance test: it shows
what your code did (each asset's outcome and value, the files, rows, requests and mails it produced) so you can check it against the spec.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--dag-id", required=True)
    ap.add_argument("--module")
    ap.add_argument("--spec", type=Path, help="the spec.json in your folder: its connection names are exported")
    ap.add_argument("--source", type=Path, help="your folder's source/ directory: its files are copied into the sandbox's working directory at the same paths, as the measurement does")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    conns = {}
    if a.spec and a.spec.exists():
        for c in json.loads(a.spec.read_text()).get("connections", []):
            cid = c if isinstance(c, str) else c.get("conn_id") or c.get("id")
            if cid:
                conns[cid] = "sqlite:///{root}/data/app.db" if "sqlite" in cid.lower() else f"generic://eval-host.test/{cid}"
    root = Path(tempfile.mkdtemp(prefix="sandbox_"))
    conns = {k: v.replace("{root}", str(root)) for k, v in conns.items()}
    cfg = {"kind": "rewrite", "dag_id": a.dag_id, "out": str(root / "result.json"), "timeout_s": a.timeout, "sandbox": {"root": str(root), "connections": conns, "variables": {}, "seed": {str(f.relative_to(a.source)): str(f) for f in sorted(a.source.rglob("*")) if f.is_file()} if a.source else {}},
           "rewrite": {"project": str(a.project.resolve()), "module": a.module}}
    (root / "config.json").write_text(json.dumps(cfg))
    r = subprocess.run([sys.executable, str(HERE / "run_dagster.py"), str(root / "config.json")], capture_output=True, text=True)
    if not (root / "result.json").exists():
        print(r.stdout[-2000:], r.stderr[-2000:])
        return 1
    res = json.loads((root / "result.json").read_text())
    print(f"loaded: {res.get('load')}; run state: {res.get('state')}; error: {res.get('error')}")
    for n in res.get("notes", []):
        print("note:", n)
    for k, v in res.get("assets", {}).items():
        print(f"asset {k}: {v['state']}" + (f" value={str(v.get('value'))[:100]}" if v.get("value") else "") + (f" errors={v['errors']}" if v.get("errors") else ""))
    for c in res.get("checks", []):
        print("check", c)
    st = res.get("structure", {})
    print("jobs:", st.get("jobs"), "schedules:", st.get("schedules"), "sensors:", st.get("sensors"))
    o = res.get("observed") or {}
    print("files:", sorted(o.get("files", {})))
    print("sqlite:", {k: v["count"] for k, v in o.get("sqlite", {}).items()})
    print("http requests:", [f"{x['method']} {x['url']}" for x in o.get("http", [])])
    print("mails:", [x["subject"] for x in o.get("smtp", [])])
    print("s3 objects:", sorted(o.get("s3", {})))
    print(f"(sandbox kept at {root})")
    return 0 if res.get("state") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
