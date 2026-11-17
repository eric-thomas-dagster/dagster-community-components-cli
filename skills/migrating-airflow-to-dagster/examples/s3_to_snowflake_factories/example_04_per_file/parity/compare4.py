import json
a = json.load(open("af_side4.json")); d = json.load(open("dg_side4.json"))
norm = lambda s: " ".join(s.split())
rows, bad = [], 0
def check(name, ok, detail=""):
    global bad; bad += (not ok); rows.append(("OK  " if ok else "DIFF", name, detail))

t1 = d["ticks"][0]
files_af = sorted(f for b in a["batches"] for f in b)
check("tick 1 lists the same prefix as Airflow's list_landing_files", t1["listed"] == [a["ticks"][0]["list_prefix"]], str(t1["listed"]))
check("tick 1 registers exactly the files Airflow would map over (folder placeholder dropped, key with a space kept)", t1["partitions_now"] == files_af, str(files_af))
check("tick 1 requests one run per file", sorted(t1["requests"]) == files_af, f"{len(t1['requests'])} runs")
for key, run in t1["runs"].items():
    ev = run["events"]; sqls = [e[1] for e in ev if e[0] == "sql"]
    copy = next(s for s in sqls if s.lstrip().startswith("COPY INTO"))
    af_sql = next(x for x in a["copy_sql"] if x["files"] == [key])
    check(f"{key}: COPY SQL identical to Airflow's mapped operator", norm(copy) == norm(af_sql["sql"]), norm(copy)[:70])
    use = [s for s in sqls if s.startswith("USE")]
    want = af_sql["overrides"]
    exp = [f"USE {k} {v}" for k, v in (("ROLE", want["role"]), ("WAREHOUSE", want["warehouse"]), ("DATABASE", want["database"])) if v] + ([f"USE SCHEMA {want['schema']}"] if want["schema"] else [])
    check(f"{key}: session overrides = operator's", use == exp)
    ac = next(c for c in a["copy_object_calls"] if c["args"][0] == key)["args"]
    exp_copy = {"Bucket": ac[3], "Key": ac[1], "CopySource": {"Bucket": ac[2], "Key": ac[0], "VersionId": ac[4]}, "ACL": ac[5] or "private"}   # what S3Hook.copy_object passes to boto
    got_copy = next(e[1] for e in ev if e[0] == "copy_object")
    check(f"{key}: archive copy = boto call Airflow's hook makes", got_copy == exp_copy, got_copy["Key"])
    dc = next(c for c in a["delete_objects_calls"] if c["kwargs"]["keys"] == [key])["kwargs"]
    exp_del = {"Bucket": dc["bucket"], "Delete": {"Objects": [{"Key": k} for k in dc["keys"]]}}
    got_del = next(e[1] for e in ev if e[0] == "delete_objects")
    check(f"{key}: delete = boto call Airflow's hook makes", got_del == exp_del)
    order = [e[0] for e in ev]
    check(f"{key}: order per file is COPY INTO -> copy_object -> delete_objects", [o for o in order if o != "list"] == ["sql", "sql", "sql", "sql", "copy_object", "delete_objects"][-len([o for o in order if o != 'list']):] or order.index("copy_object") > max(i for i, o in enumerate(order) if o == "sql") and order.index("delete_objects") > order.index("copy_object"), " > ".join(o for o in order if o != "list"))
for i, (td, ta) in enumerate(zip(d["ticks"], a["ticks"]), 1):
    check(f"tick {i}: lists Airflow's prefix for ds={ta['ds']}", td["listed"] == [ta["list_prefix"]], td["listed"][0])
check("tick 3 (no files landed) is skipped, not an empty run", d["ticks"][2]["skip"] is not None and not d["ticks"][2]["requests"], str(d["ticks"][2]["skip"]))
check("cron and timezone", d["cron"] == a["cron"] and d["tz"] == a["timezone"], f"{d['cron']} {d['tz']}")
check("re-evaluating a tick does not duplicate partitions", d["partitions_before"] == d["partitions_after_reevaluating_tick1"], str(d["partitions_after_reevaluating_tick1"]))
print("\n".join(f"{s} {n}" + (f"  [{x}]" if x else "") for s, n, x in rows)); print(f"\n{len(rows)-bad}/{len(rows)} checks match")
print("\nAirflow dependency structure (real DAG):")
for t, v in a["deps"].items(): print(f"  {t:26} upstream={v['upstream']} trigger_rule={v['trigger_rule']}")
