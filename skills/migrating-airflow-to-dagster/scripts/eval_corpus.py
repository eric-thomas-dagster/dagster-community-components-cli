#!/usr/bin/env python3
"""Build the evaluation corpus: public Airflow example repos -> IR -> converter result -> pattern, externals, eligibility, written as one deterministic manifest.

    python3 eval_corpus.py build  --work DIR [--repos evals/corpus_repos.json] [--venv-2 PY] [--venv-3 PY] [--no-clone] [--jobs N]
    python3 eval_corpus.py summary --manifest corpus/manifest.json

What `build` does, per repo in the repo list (all public; nothing private):
  1. clones it (shallow) into DIR/repos/<name> (skipped with --no-clone or when already there);
  2. extracts the IR with `airflow_ir.py` in each Airflow venv (2.x and 3.x; whichever imports the DAG file wins, the repo's `prefer` first), which EXECUTES the DAG files:
     run it only in a scratch machine, as the extractor itself warns;
  3. enumerates the DAGs the static scanner (`inventory.py`) sees, so a DAG whose file did not import is listed as "not extracted" with the import error, not lost;
  4. runs the converter (`convert_ir.py`) on each IR and classifies the result: clean / degraded / stubbed / deferred;
  5. classifies the DAG's pattern (python, bash, sql, transfer, sensor, branching, mapping, dbt, containers), the external systems it needs, and whether each has a local stand-in
     the behaviour harness provides (filesystem, SQLite, HTTP and SMTP recorders, moto for S3), which makes the DAG eligible for behaviour comparison.

The manifest is a table (one row per DAG, sorted), deterministic for the same clones, with no timestamps and no secret values. Intermediate files stay in DIR.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
PATTERN_ORDER = ["dbt", "containers", "transfer", "sql", "sensor", "branching", "mapping", "bash", "python"]
STRATA = ["clean", "degraded", "stubbed", "deferred"]

# external systems with a local stand-in in the behaviour harness
STANDINS = {"filesystem", "sqlite", "http", "smtp", "s3"}

PROVIDER_SYSTEM = [  # (module prefix, system)
    ("airflow.providers.http", "http"), ("airflow.providers.smtp", "smtp"), ("airflow.providers.sqlite", "sqlite"),
    ("airflow.providers.docker", "docker"), ("airflow.providers.cncf.kubernetes", "kubernetes"), ("airflow.providers.ssh", "ssh"), ("airflow.providers.sftp", "sftp"),
    ("airflow.providers.snowflake", "snowflake"), ("airflow.providers.postgres", "postgres"), ("airflow.providers.mysql", "mysql"),
    ("airflow.providers.google", "gcp"), ("airflow.providers.microsoft", "azure"), ("airflow.providers.databricks", "databricks"), ("airflow.providers.slack", "slack"),
    ("airflow.providers.apache.spark", "spark"), ("airflow.providers.apache.kafka", "kafka"), ("airflow.providers.openai", "openai"), ("airflow.providers.pinecone", "pinecone"),
    ("airflow.providers.weaviate", "weaviate"), ("airflow.providers.fivetran", "fivetran"), ("airflow.providers.airbyte", "airbyte"), ("airflow.providers.dbt", "dbt-cloud"),
    ("airflow.providers.mongo", "mongodb"), ("airflow.providers.redis", "redis"), ("airflow.providers.oracle", "oracle"), ("airflow.providers.jdbc", "jdbc"),
    ("airflow.providers.common.ai", "llm-api"), ("airflow.providers.anthropic", "llm-api"), ("airflow.providers.edge3", "edge-worker"), ("airflow.providers.arangodb", "arangodb"),
    ("airflow.providers.clickhousedb", "clickhouse"), ("airflow.providers.informatica", "informatica"), ("cosmos", "dbt-warehouse"),
]
SOURCE_SYSTEM = [  # (regex over callable source, system)
    (r"\brequests\.(get|post|put|delete|patch|request|Session)|urllib\.request|urlopen\(|HttpHook", "http"),
    (r"\bhttpx\b|aiohttp", "http-async"),
    (r"smtplib|send_email\(", "smtp"),
    (r"\bboto3\.(client|resource)\(\s*['\"](?!s3['\"])|\bboto3\.client\(\s*service_name\s*=\s*['\"](?!s3)", "aws"),
    (r"\bboto3\b|S3Hook", "s3"),
    (r"psycopg2|pymysql|snowflake\.connector|SnowflakeHook|PostgresHook|MySqlHook|BigQuery|pymongo|\bredis\b|kafka|cx_Oracle|oracledb|pyodbc|MsSqlHook|DbApiHook|\.get_hook\(", "database"),
    (r"sqlalchemy\.create_engine|create_engine\(", "database"),
    (r"sqlite3|SqliteHook", "sqlite"),
    (r"\bopenai\b|\banthropic\b|cohere|pinecone|weaviate|transformers|torch\b|mlflow|wandb|langchain", "ml-api"),
    (r"\bslack_sdk|SlackWebhook|WebClient\(", "slack"),
    (r"\bparamiko|SSHHook|SFTPHook|ftplib", "ssh"),
    (r"docker\.|DockerOperator|kubernetes\.client", "containers"),
]
NONDET_SOURCE = re.compile(r"datetime\.(now|utcnow|today)|time\.time\(|\brandom\.|uuid\.uuid|np\.random|\bsecrets\.|pendulum\.now|\.now\(")
SQL_OPS = re.compile(r"Sql|SQL|Postgres|MySql|Snowflake|BigQuery|Redshift|Athena|Trino|Presto|Oracle|Mssql|Teradata|Vertica|Hive", re.I)
CONTAINER_OPS = re.compile(r"Docker|Kubernetes|KPO|Ecs|Eks|Batch|Container|Spark|Dataproc|Lambda|Pod|Dataflow|Beam", re.I)
BRANCH_OPS = re.compile(r"Branch|ShortCircuit")


def sh(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_repo_list(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["repos"]


def discover_dag_folders(root: Path, spec: dict[str, Any]) -> list[Path]:
    if spec.get("dag_folders"):
        return [root / f for f in spec["dag_folders"] if (root / f).is_dir()]
    found = sorted((p for p in root.rglob("dags") if p.is_dir() and not any(x in p.parts for x in (".venv", "node_modules", ".git", "tests", "site-packages"))), key=lambda p: (len(p.parts), str(p)))
    top: list[Path] = []
    for p in found:
        if not any(t in p.parents for t in top):
            top.append(p)
    return top


def repo_commit(root: Path) -> str:
    r = sh(["git", "-C", str(root), "rev-parse", "--short=10", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def clone(spec: dict[str, Any], work: Path) -> Path:
    dest = work / "repos" / spec["name"]
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = sh(["git", "clone", "-q", "--depth", "1", f"https://github.com/{spec['github']}", str(dest)])
        if r.returncode != 0:
            print(f"clone failed for {spec['github']}: {r.stderr.strip()[-200:]}", file=sys.stderr)
    return dest


def extract_ir(py: str, root: Path, folders: list[Path], out: Path) -> dict[str, Any]:
    """Run airflow_ir.py in the given interpreter. Returns the IR (or an empty one with the error)."""
    if out.exists():
        return json.loads(out.read_text())
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "AIRFLOW__CORE__LOAD_EXAMPLES": "False"}
    cmd = [py, "-W", "ignore", str(HERE / "airflow_ir.py"), *map(str, folders), "--out", str(out), "--pythonpath", str(root)]
    try:
        r = sh(cmd, env=env, timeout=1800, cwd=str(root))
    except subprocess.TimeoutExpired:
        return {"dags": [], "import_errors": {"*": "IR extraction timed out after 1800 s"}, "airflow_version": None}
    if not out.exists():
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
        return {"dags": [], "import_errors": {"*": f"IR extraction failed: {tail[0][:200]}"}, "airflow_version": None}
    return json.loads(out.read_text())


def convert(ir_path: Path, out_dir: Path) -> dict[str, Any]:
    """Run the converter on an IR; returns its report (per-DAG status and caveats). The generated modules stay in out_dir/src/pkg/defs."""
    rep = out_dir / "report.json"
    if not rep.exists():
        (out_dir / "src" / "pkg" / "defs").mkdir(parents=True, exist_ok=True)
        sh([sys.executable, str(HERE / "convert_ir.py"), str(ir_path), "--out-dir", str(out_dir / "src" / "pkg" / "defs"), "--report", str(rep)], timeout=1800)
    return json.loads(rep.read_text()) if rep.exists() else {"dags": {}}


def static_inventory(root: Path, folders: list[Path], out: Path) -> dict[str, dict[str, Any]]:
    """DAG id -> {file, task_ids} as the static scanner (never imports code) sees them, over the DAG folders only (file paths are relative to the repo root)."""
    res: dict[str, dict[str, Any]] = {}
    for i, folder in enumerate(folders):
        o = out.with_name(f"{out.stem}.{i}.json")
        if not o.exists():
            sh([sys.executable, str(HERE / "inventory.py"), str(folder), "--out", str(o)], timeout=900)
        if not o.exists():
            continue
        for u in json.loads(o.read_text())["units"]:
            if u.get("kind") == "dag" and u.get("dag_id"):
                res[u["dag_id"]] = {"file": os.path.relpath(folder / u["file"].split("/", 1)[-1], root), "task_ids": u.get("task_ids", [])}
    return res


def conn_ids(task: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def walk(v: Any, k: str = "") -> None:
        if isinstance(v, dict):
            for kk, vv in v.items():
                walk(vv, kk)
        elif isinstance(v, str) and k.endswith("conn_id"):
            out.append(v)
    walk(task.get("args") or {})
    return out


def callable_source(task: dict[str, Any]) -> str:
    parts = [str((task.get("callable") or {}).get("source", ""))]
    parts += [str(v.get("source", "")) for v in (task.get("callable_arg_info") or {}).values() if isinstance(v, dict)]
    return "\n".join(parts)


def task_systems(task: dict[str, Any]) -> set[str]:
    mod = task.get("module") or ""
    typ = task.get("type") or ""
    s: set[str] = set()
    cids = conn_ids(task)
    for pref, name in PROVIDER_SYSTEM:
        if mod.startswith(pref):
            s.add(name)
    if mod.startswith("airflow.providers.amazon"):
        s.add("s3" if re.search(r"S3", typ) and not re.search(r"Glue|Athena|Redshift|Ecs|Eks|Batch|Lambda|Sagemaker|Emr|Dms|Rds|Sns|Sqs|Step|Cloud|Appflow|Quick", typ) else "aws")
    if mod.startswith(("airflow.providers.common.sql", "airflow.operators.sql", "airflow.providers.standard.operators.sql")):
        s.add("sqlite" if cids and all("sqlite" in c.lower() for c in cids) else "database")
    if "EmailOperator" in typ:
        s.add("smtp")
    if typ in ("TriggerDagRunOperator", "ExternalTaskSensor", "ExternalTaskMarker") or "trigger_dagrun" in mod:
        s.add("other-dag")
    if "HttpSensor" in typ or "HttpOperator" in typ or "SimpleHttp" in typ:
        s.add("http")
    src = callable_source(task)
    for rx, name in SOURCE_SYSTEM:
        if re.search(rx, src):
            s.add(name)
    if cids and not s and not mod.startswith(("airflow.operators", "airflow.sensors")):
        s.add("database")
    return s


def classify_patterns(dag: dict[str, Any]) -> list[str]:
    pats: set[str] = set()
    for t in dag["tasks"]:
        typ, mod = t.get("type") or "", t.get("module") or ""
        if mod.startswith("cosmos") or "Dbt" in typ:
            pats.add("dbt")
        if CONTAINER_OPS.search(typ) and not typ.startswith("_Python"):
            pats.add("containers")
        if re.search(r"(?:To|From)[A-Z]\w*Operator$|Transfer|Copy|Sync", typ) and not re.search(r"Dag|Task", typ):
            pats.add("transfer")
        if SQL_OPS.search(typ) and "Operator" in typ and not CONTAINER_OPS.search(typ):
            pats.add("sql")
        if t.get("sensor") or "Sensor" in typ:
            pats.add("sensor")
        if BRANCH_OPS.search(typ):
            pats.add("branching")
        if t.get("is_mapped") or t.get("expand"):
            pats.add("mapping")
        if "Bash" in typ:
            pats.add("bash")
        if "Python" in typ or mod.startswith("airflow.decorators"):
            pats.add("python")
    if dag.get("cosmos_groups"):
        pats.add("dbt")
    return [p for p in PATTERN_ORDER if p in pats] or ["python"]


def dag_systems(dag: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    systems: set[str] = set()
    nondet = False
    for t in dag["tasks"]:
        systems |= task_systems(t)
        nondet = nondet or bool(NONDET_SOURCE.search(callable_source(t)))
    return [{"system": s, "standin": s in STANDINS} for s in sorted(systems)], nondet


def converter_stratum(rep: dict[str, Any] | None) -> tuple[str, list[str], list[str], str | None]:
    """clean / degraded / stubbed / deferred from the converter's report entry."""
    if rep is None:
        return "unconverted", [], [], "no converter report"
    if rep.get("status") != "converted":
        why = rep.get("reason") or "; ".join(rep.get("reasons", []) or []) or rep.get("status")
        return "deferred", [], [], str(why)[:300]
    cav = rep.get("caveats", [])
    stubs = sorted({c.get("task") or "(dag)" for c in cav if c.get("kind") == "stub"})
    degr = sorted({c.get("task") or "(dag)" for c in cav if c.get("kind") == "degraded"})
    if stubs:
        return "stubbed", stubs, degr, None
    if degr:
        return "degraded", [], degr, None
    return "clean", [], [], None


def run_job(job: tuple) -> tuple:
    spec, ver, py, root, folders, work = job
    irp = work / "irs" / f"{spec['name']}.{ver}.json"
    ir = extract_ir(py, root, folders, irp)
    conv = convert(irp, work / "conv" / f"{spec['name']}.{ver}") if ir.get("dags") else {"dags": {}}
    return spec["name"], ver, ir, conv


def build(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    for d in ("repos", "irs", "conv", "corpus"):
        (work / d).mkdir(parents=True, exist_ok=True)
    pys = {"2": a.venv_2 or str(work / "venvs/af210/bin/python"), "3": a.venv_3 or str(work / "venvs/af340/bin/python")}
    specs = load_repo_list(a.repos)
    roots: dict[str, Path] = {s["name"]: (work / "repos" / s["name"]) if a.no_clone else clone(s, work) for s in specs}
    jobs = []
    for s in specs:
        root = roots[s["name"]]
        if not root.exists():
            continue
        folders = discover_dag_folders(root, s)
        s["_folders"] = folders
        if folders:
            jobs += [(s, ver, pys[ver], root, folders, work) for ver in ("2", "3")]
    print(f"extracting {len(jobs)} (repo, airflow) combinations with {a.jobs} workers ...", flush=True)
    results: dict[tuple[str, str], tuple[dict, dict]] = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        for name, ver, ir, conv in ex.map(run_job, jobs):
            results[(name, ver)] = (ir, conv)
            print(f"  {name} airflow-{ver}: {len(ir.get('dags', []))} DAGs, {len(ir.get('import_errors', {}))} import problem(s)", flush=True)
    rows: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    for s in specs:
        name = s["name"]
        root = roots[name]
        if not root.exists():
            continue
        commit = repo_commit(root)
        inv = static_inventory(root, s.get("_folders", []), work / "irs" / f"{name}.inventory.json")
        prefer = s.get("prefer", "2")
        order = [prefer] + [v for v in ("2", "3") if v != prefer]
        chosen: dict[str, tuple[str, dict, dict | None]] = {}
        in_versions: dict[str, list[str]] = collections.defaultdict(list)
        for ver in order:
            ir, conv = results.get((name, ver), ({"dags": []}, {"dags": {}}))
            if ir.get("airflow_version"):
                versions[ver] = ir["airflow_version"]
            for d in ir.get("dags", []):
                in_versions[d["dag_id"]].append(ver)
                chosen.setdefault(d["dag_id"], (ver, d, conv.get("dags", {}).get(d["dag_id"])))
        errors = {v: results.get((name, v), ({}, {}))[0].get("import_errors", {}) for v in ("2", "3")}
        for did in sorted(set(chosen) | set(inv)):
            row: dict[str, Any] = {"key": f"{name}::{did}", "repo": name, "github": s["github"], "commit": commit, "dag_id": did}
            if did in chosen:
                ver, d, rep = chosen[did]
                stratum, stubs, degr, why = converter_stratum(rep)
                systems, nondet = dag_systems(d)
                pats = classify_patterns(d)
                bad = [x["system"] for x in systems if not x["standin"]]
                row.update({
                    "extracted": True, "extract_error": None, "airflow": ver, "extracted_in": sorted(in_versions[did]), "file": d["file"],
                    "tasks": len(d["tasks"]), "task_ids": sorted(t["task_id"] for t in d["tasks"]),
                    "schedule_kind": (d.get("timetable") or {}).get("kind"), "catchup": d.get("catchup"),
                    "converter": stratum, "converter_reason": why, "converter_stubbed": stubs, "converter_degraded": degr,
                    "pattern": pats[0], "patterns": pats, "externals": systems, "needs_external": bool(systems), "nondeterministic_source": nondet,
                    "eligible": not bad, "ineligible_reason": None if not bad else "needs " + ", ".join(bad) + " (no local stand-in)",
                })
            else:
                f = inv[did]["file"]
                base = os.path.basename(f)
                err = None
                for v in ("2", "3"):
                    for path, msg in errors[v].items():
                        if os.path.basename(path.split(" (")[0]) == base:
                            err = err or f"airflow {v}: {msg}"
                row.update({"extracted": False, "extract_error": err or "the file defines the DAG statically but Airflow did not produce it (conditional, factory or runtime-dependent definition)",
                            "airflow": None, "extracted_in": [], "file": f, "tasks": len(inv[did]["task_ids"]), "task_ids": sorted(inv[did]["task_ids"]), "schedule_kind": None, "catchup": None,
                            "converter": "unconverted", "converter_reason": "not extracted", "converter_stubbed": [], "converter_degraded": [], "pattern": None, "patterns": [], "externals": [],
                            "needs_external": None, "nondeterministic_source": None, "eligible": False, "ineligible_reason": "not extracted"})
            rows.append(row)
    rows.sort(key=lambda r: (r["repo"], r["dag_id"]))
    manifest = {"schema": 1, "note": "public Airflow example DAGs, not an estate; no secret values; deterministic for the same clone commits", "airflow_versions": versions,
                "repos": {s["name"]: {"github": s["github"], "commit": repo_commit(roots[s["name"]]), "dag_folders": [str(p.relative_to(roots[s["name"]])) for p in s.get("_folders", [])]} for s in specs if roots[s["name"]].exists()},
                "dags": rows}
    out = work / "corpus" / "manifest.json"
    out.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return summary(out)


def summary(path: Path) -> int:
    m = json.loads(Path(path).read_text())
    rows = m["dags"]
    ext = [r for r in rows if r["extracted"]]
    elig = [r for r in rows if r.get("eligible")]
    print(f"DAGs known: {len(rows)} (static scan or Airflow); extracted: {len(ext)}; not extracted: {len(rows) - len(ext)}; eligible for behaviour comparison: {len(elig)}")
    print("converter result (extracted):", dict(collections.Counter(r["converter"] for r in ext)))
    print("converter result (eligible): ", dict(collections.Counter(r["converter"] for r in elig)))
    print("pattern (eligible):          ", dict(collections.Counter(r["pattern"] for r in elig)))
    print("repo (eligible):             ", dict(collections.Counter(r["repo"] for r in elig).most_common()))
    why = collections.Counter((r["extract_error"] or "").split(":")[-2][:50] if (r["extract_error"] or "").count(":") > 1 else (r["extract_error"] or "")[:50] for r in rows if not r["extracted"])
    print("not extracted, by error:     ", dict(why.most_common(8)))
    print("ineligible, by external:     ", dict(collections.Counter(x["system"] for r in ext if not r["eligible"] for x in r["externals"] if not x["standin"]).most_common(12)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--repos", type=Path, default=SKILL / "evals" / "corpus_repos.json")
    b.add_argument("--venv-2")
    b.add_argument("--venv-3")
    b.add_argument("--no-clone", action="store_true")
    b.add_argument("--jobs", type=int, default=6)
    s = sub.add_parser("summary")
    s.add_argument("--manifest", type=Path, required=True)
    a = ap.parse_args(argv)
    return build(a) if a.cmd == "build" else summary(a.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
