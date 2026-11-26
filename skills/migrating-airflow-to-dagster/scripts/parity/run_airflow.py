"""Run ONE original DAG in real Airflow (`dag.test()`) inside the shared sandbox and record outcomes, XCom values, errors and what it left behind.

    python run_airflow.py CONFIG.json            (CONFIG: dag_file, dag_id, repo_root, sandbox {root, connections, variables, sleep_cap}, out, timeout_s)

Runs in the Airflow interpreter, which usually has no dagster: it imports only the sandbox and Airflow. Extends `parity_af.py` with every XCom key (not only `return_value`), the state of each
mapped instance, the error text of failed tasks, and the sandbox snapshot (files, SQLite rows, HTTP requests, mails, S3 objects).
"""
import json
import logging
import os
import signal
import sys
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
cfg = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, str(Path(__file__).parent))
from sandbox import Sandbox  # noqa: E402

sb = Sandbox(Path(cfg["sandbox"]["root"]), cfg["sandbox"].get("connections"), cfg["sandbox"].get("variables"), cfg["sandbox"].get("sleep_cap")).install()
repo = cfg["repo_root"]
sys.path.insert(0, repo)
for extra in cfg.get("pythonpath", []):
    sys.path.insert(0, extra)


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.items = []

    def emit(self, record):
        try:
            txt = record.getMessage()
            if record.exc_info:
                txt += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
            self.items.append(txt[-1800:])
        except Exception:
            pass


errs = _Errors()
logging.getLogger().addHandler(errs)


def canon(v):
    try:
        return json.dumps(v, sort_keys=True, default=lambda o: repr(o))
    except Exception:
        return repr(v)


res = {"dag_id": cfg["dag_id"], "airflow_version": None, "state": None, "tasks": {}, "error": None, "errors": [], "observed": None}
import airflow  # noqa: E402

res["airflow_version"] = airflow.__version__
AF2 = airflow.__version__.startswith("2.")
try:
    from airflow.utils.db import initdb
    initdb()
    if AF2:
        from airflow.models.dagbag import DagBag
    else:
        try:
            from airflow.dag_processing.dagbag import DagBag
        except ImportError:
            from airflow.models.dagbag import DagBag
    try:
        bag = DagBag(dag_folder=cfg["dag_file"], include_examples=False, safe_mode=False)
    except TypeError:
        bag = DagBag(dag_folder=cfg["dag_file"], safe_mode=False)
    dag = bag.dags.get(cfg["dag_id"])
    if dag is None:
        res["error"] = "dag not in DagBag: " + "; ".join(f"{k}: {v.strip().splitlines()[-1]}" for k, v in bag.import_errors.items())[:400]
    elif not AF2:
        try:
            from airflow.dag_processing.dagbag import sync_bag_to_db
        except ImportError:
            try:
                from airflow.models.dagbag import sync_bag_to_db
            except ImportError:
                def sync_bag_to_db(bag, bundle, version):
                    bag.sync_to_db(bundle_name=bundle, bundle_version=version)
        from airflow.dag_processing.bundles.manager import DagBundlesManager
        DagBundlesManager().sync_bundles_to_db()
        for k in [k for k in bag.dags if k != cfg["dag_id"]]:
            del bag.dags[k]
        sync_bag_to_db(bag, "dags-folder", None)
except BaseException as e:  # noqa: BLE001
    res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    dag = None


class TO(Exception):
    pass


def alarm(*_):
    raise TO()


if dag is not None:
    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(int(cfg.get("timeout_s", 300)))
    try:
        dr = dag.test()
        if dr is None:
            from airflow.models import DagRun
            dr = sorted(DagRun.find(dag_id=cfg["dag_id"]), key=lambda r: r.start_date or r.execution_date)[-1]
        try:
            from airflow.models import DagRun
            fresh_dr = DagRun.find(dag_id=cfg["dag_id"], run_id=dr.run_id)
            if fresh_dr:
                dr = fresh_dr[0]
        except BaseException:  # noqa: BLE001
            pass
        res["state"] = str(dr.state)
        xc = {}
        try:
            if AF2:
                from airflow.models.xcom import XCom
                from airflow.utils.session import create_session
                with create_session() as s:
                    for r in s.query(XCom).filter(XCom.dag_id == cfg["dag_id"], XCom.run_id == dr.run_id).all():
                        xc.setdefault(r.task_id, {}).setdefault(r.key, {})[r.map_index] = canon(r.value)
            else:
                from airflow.models.xcom import XComModel
                from airflow.utils.session import create_session
                with create_session() as s:
                    for r in s.scalars(XComModel.get_many(run_id=dr.run_id, dag_ids=cfg["dag_id"], key=None)).all():
                        xc.setdefault(r.task_id, {}).setdefault(r.key, {})[r.map_index] = canon(XComModel.deserialize_value(r))
        except BaseException as e:  # noqa: BLE001
            res["errors"].append(f"xcom read failed: {type(e).__name__}: {str(e)[:200]}")
        for ti in dr.get_task_instances():
            t = res["tasks"].setdefault(ti.task_id, {"states": {}, "xcom": {}, "tries": 0})
            t["states"][str(ti.map_index)] = str(ti.state)
            t["tries"] = max(t["tries"], int(getattr(ti, "try_number", 0) or 0))
        for tid, t in res["tasks"].items():
            sts = set(t["states"].values())
            t["state"] = "failed" if "failed" in sts else ("upstream_failed" if "upstream_failed" in sts else ("skipped" if sts == {"skipped"} else ("success" if sts <= {"success", "skipped"} and "success" in sts else sorted(sts)[0])))
            t["xcom"] = {k: [m[i] for i in sorted(m)] if len(m) > 1 or list(m) != [-1] else m[-1] for k, m in xc.get(tid, {}).items()}
    except TO:
        res["error"] = f"timeout {cfg.get('timeout_s', 300)}s"
    except BaseException as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        signal.alarm(0)
if res["tasks"]:
    bad = [t for t, v in res["tasks"].items() if v["state"] in ("failed", "upstream_failed")]
    res["computed_state"] = "failed" if bad else "success"
    if res["state"] and not any(w in res["state"].lower() for w in ("success", "failed")):
        res["errors"].append(f"harness note: the run state was {res['state']}: using the task states")
        res["state"] = res["computed_state"]
res["errors"] += errs.items[:20]
try:                                                                                 # task logs: Airflow writes a failing task's traceback there, not to the root logger
    for lf in sorted(Path(os.environ["AIRFLOW_HOME"], "logs").rglob("*.log"))[:60]:
        txt = lf.read_text(errors="replace")
        if "Traceback" in txt or " ERROR " in txt or "ERROR -" in txt:
            res["errors"].append(f"[{lf.parent.name}/{lf.name}] " + txt[-1500:])
except Exception:  # noqa: BLE001
    pass
try:
    res["observed"] = sb.snapshot()
finally:
    sb.stop()
Path(cfg["out"]).write_text(json.dumps(res, indent=1, default=str))
