"""Compare Airflow's and Dagster's parity results.

  python3 compare.py <results dir>

<results dir> holds af_<repo>.json and dg_<repo>.json and reports.json (repo name -> the converter's report.json). A DAG is
"identical" when the overall outcome and every converted task's outcome match, and every returned value that both sides recorded
matches. Mapped tasks: Airflow records one instance, Dagster the collected list, so the value passes if Airflow's appears in it.
"""
import collections, glob, json, os, sys

results = sys.argv[1]
reports = json.load(open(f"{results}/reports.json"))
tot = collections.Counter(); problems = []
for af_path in sorted(glob.glob(f"{results}/af_*.json")):
    repo = os.path.basename(af_path)[3:-5]; dg_path = f"{results}/dg_{repo}.json"
    if not os.path.exists(dg_path):
        continue
    A = json.load(open(af_path)); D = json.load(open(dg_path)); rep = json.load(open(reports[repo]))["dags"]
    c = collections.Counter()
    for did in A:
        a, d = A[did], D.get(did, {"error": "missing in dagster run"})
        if "error" in a or "error" in d:
            c["no comparison (error on a side)"] += 1; problems.append((repo, did, f"airflow: {a.get('error', 'ok')} | dagster: {d.get('error', 'ok')}")); continue
        issues = []
        if ("success" in a["state"].lower()) != (d["state"] == "success"):
            issues.append(f"overall: airflow {a['state']} vs dagster {d['state']}")
        for tid in rep[did]["task_keys"]:
            at, dt = a["tasks"].get(tid), d["tasks"].get(tid)
            if at is None or dt is None:
                issues.append(f"{tid}: missing"); continue
            a_s = "success" in at["state"].lower(); d_s = dt["state"] == "success"
            if a_s != d_s:
                issues.append(f"{tid}: airflow {at['state']} vs dagster {dt['state']}")
            elif dt["value"] != "None" and not at["value"].startswith("'xcom") and at["value"] != "'xcom-unavailable-3x'" and at["value"] != dt["value"] \
                    and not (dt["value"].startswith("[") and at["value"] in dt["value"]):
                issues.append(f"{tid}: value airflow {at['value'][:60]} vs dagster {dt['value'][:60]}")
        if not issues:
            c["identical outcome"] += 1
        else:
            c["differs"] += 1; problems.append((repo, did, "; ".join(issues)[:300]))
    tot.update(c); print(f"{repo:34} {dict(c)}")
print("\nTOTAL", dict(tot))
for p in problems:
    print("  ", p[0][:18], p[1][:36], "::", p[2])
