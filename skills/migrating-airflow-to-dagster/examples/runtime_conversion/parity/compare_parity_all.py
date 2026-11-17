import json, glob, os, collections, sys
tot = collections.Counter(); problems = []
for af_path in sorted(glob.glob("parity_out/af_*.json")):
    repo = os.path.basename(af_path)[3:-5]; dg_path = f"parity_out/dg_{repo}.json"
    if not os.path.exists(dg_path): continue
    A = json.load(open(af_path)); D = json.load(open(dg_path)); rep = json.load(open(f"conv/{repo}/report.json"))["dags"]
    c = collections.Counter()
    for did in A:
        a, d = A[did], D.get(did, {"error": "missing in dagster run"})
        if "error" in a or "error" in d:
            c["no comparison (error on a side)"] += 1; problems.append((repo, did, f"airflow: {a.get('error','ok')} | dagster: {d.get('error','ok')}")); continue
        a_ok = "success" in a["state"].lower(); d_ok = d["state"] == "success"
        issues = []
        if a_ok != d_ok: issues.append(f"overall: airflow {a['state']} vs dagster {d['state']}")
        for tid in rep[did]["task_keys"]:
            at, dt = a["tasks"].get(tid), d["tasks"].get(tid)
            if at is None or dt is None: issues.append(f"{tid}: missing"); continue
            a_s = "success" in at["state"].lower(); d_s = dt["state"] == "success"
            if a_s != d_s: issues.append(f"{tid}: airflow {at['state']} vs dagster {dt['state']}")
            elif dt["value"] != "None" and not at["value"].startswith("'xcom") and at["value"] != "'xcom-unavailable-3x'" and at["value"] != dt["value"]:
                issues.append(f"{tid}: value airflow {at['value'][:60]} vs dagster {dt['value'][:60]}")
        if not issues:
            c["identical outcome"] += 1
        else:
            c["differs"] += 1; problems.append((repo, did, "; ".join(issues)[:300]))
    tot.update(c); print(f"{repo:34} {dict(c)}")
print("\nTOTAL", dict(tot))
for p in problems[:40]: print("  ", p[0][:18], p[1][:36], "::", p[2])
