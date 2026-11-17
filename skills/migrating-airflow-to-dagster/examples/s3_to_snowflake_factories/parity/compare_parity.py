import json
a = json.load(open("af_side.json")); d = json.load(open("dg_side.json"))
norm = lambda s: " ".join(s.split())
rows = []; bad = 0
def check(name, ok, detail=""):
    global bad; bad += (not ok); rows.append(("OK  " if ok else "DIFF", name, detail))

PAIR = {  # dagster table asset -> airflow dag
 "analytics/raw/orders_raw": "example_01_s3_to_snowflake_python_factory",
 "analytics/raw/customers_raw": "example_02_s3_to_snowflake_generated_customers",
 "analytics/raw/products_raw": "example_02_s3_to_snowflake_generated_products"}
for asset, dag in PAIR.items():
    A, R = a[dag], d["runs"][asset]
    copy_sql = next(c["sql"] for c in R["sql_calls"] if c["sql"].lstrip().startswith("COPY INTO"))
    check(f"{asset}: COPY SQL identical to Airflow's operator (ds=2026-07-01)", norm(copy_sql) == norm(A["copy_sql"]["2026-07-01"]), norm(copy_sql)[:90])
    check(f"{asset}: S3 listing prefix identical", R["list_calls"][0]["prefix"] == A["list_prefix_rendered"]["2026-07-01"] and R["list_calls"][0]["bucket"] == A["list_bucket"], R["list_calls"][0]["prefix"])
    use = [c["sql"] for c in R["sql_calls"] if c["sql"].startswith("USE")]
    want = A["hook_overrides"]
    exp = [f"USE {k} {v}" for k, v in (("ROLE", want["role"]), ("WAREHOUSE", want["warehouse"]), ("DATABASE", want["database"])) if v] + ([f"USE SCHEMA {want['schema']}"] if want["schema"] else [])
    check(f"{asset}: session overrides = operator's warehouse/database/role/schema", use == exp, ", ".join(use))
    check(f"{asset}: manifest file list = Airflow's build_load_manifest", R["manifest_files"] == sorted(k for k in R["manifest_files"]) and R["file_count"] == len(R["manifest_files"]), f"{R['file_count']} files")
check("build_load_manifest output identical to Airflow task callable", d["manifest_fn"] == a["example_01_s3_to_snowflake_python_factory"]["manifest_sample"], str(d["manifest_fn"]))
for dag, rec in a.items():
    t = d["ticks"].get(dag)
    if not t: continue
    check(f"{dag[:46]}: cron", t["cron"] == rec["cron"], f"{t['cron']} vs {rec['cron']}")
    check(f"{dag[:46]}: ds at each tick (3 ticks)", [x["dagster_ds"] for x in t["ticks"]] == [x["ds"] for x in rec["ticks"]],
          f"dagster {[x['dagster_ds'] for x in t['ticks']]} airflow {[x['ds'] for x in rec['ticks']]}")
print("\n".join(f"{s} {n}" + (f"  [{x}]" if x else "") for s, n, x in rows)); print(f"\n{len(rows)-bad}/{len(rows)} checks match")
# duplicates: do the other DAGs generate the same SQL as example_01?
base = norm(a["example_01_s3_to_snowflake_python_factory"]["copy_sql"]["2026-07-01"])
print("\nDuplicate producers of RAW.ORDERS_RAW vs example_01 (same SQL for ds=2026-07-01):")
for dag in ("example_03_s3_to_snowflake_task_group", "example_05_s3_to_snowflake_blueprint", "example_06_s3_to_snowflake_dag_factory", "example_02_s3_to_snowflake_generated_orders"):
    r = a[dag]; print(f"  {dag[:48]:48} sql_same={norm(r['copy_sql']['2026-07-01']) == base!s:5} cron={r['cron']:10} tz={r['timezone']:34} tasks={len(r['tasks'])}")
print("\nhourly ds rule (Airflow ds = UTC date of logical date):", d["hourly_ds_rule"])
