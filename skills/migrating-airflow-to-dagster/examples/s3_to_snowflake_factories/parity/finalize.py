import json, subprocess, sys
sys.path.insert(0, sys.argv[1]); import status, validate_defs
m = json.load(open("m_reusable_rec.json"))
D = lambda s: f"example_{s}"
DAG = {"01": "example_01_s3_to_snowflake_python_factory", "cust": "example_02_s3_to_snowflake_generated_customers",
       "prod": "example_02_s3_to_snowflake_generated_products"}
TABLE = {DAG["01"]: "analytics/raw/orders_raw", DAG["cust"]: "analytics/raw/customers_raw", DAG["prod"]: "analytics/raw/products_raw"}
SRC = {DAG["01"]: "s3/example-landing-bucket/orders", DAG["cust"]: "s3/example-landing-bucket/customers", DAG["prod"]: "s3/example-landing-bucket/products"}
DUP = ("Duplicate producer of ANALYTICS.RAW.ORDERS_RAW (example_01 produces it). A Dagster asset has one producing definition; many may "
       "depend on it. COPY SQL for ds=2026-07-01 is byte-identical to example_01's (compare_parity.py). Owner decision needed on which schedule "
       "wins; differences: {}.")
DEFER = {
 "example_02_s3_to_snowflake_generated_orders": ("Hourly variant of the orders load: same table as example_01 but hourly cadence vs daily. One asset cannot "
     "have two partitionings or two producers; owner decision needed. Hourly tick and ds semantics were verified (27 ticks) for when it is chosen."),
 "example_03_s3_to_snowflake_task_group": DUP.format("wraps the load group with prepare_load_window and publish_load_complete steps"),
 "example_05_s3_to_snowflake_blueprint": DUP.format("schedule timezone is a fixed -05:00 offset (no DST) rather than America/Chicago; no post-load SQL"),
 "example_06_s3_to_snowflake_dag_factory": DUP.format("UTC schedule; no post-load SQL; no log_load_manifest step"),
 "example_04_s3_to_snowflake_dynamic_task_mapping": ("REDESIGN: dynamic task mapping runs COPY, archive-copy and delete once per landed file. Options: dynamic "
     "partitions (one per file key) with a sensor, or one asset that loops; the archive/delete steps are side effects, not data products. Not converted in this trial."),
 "example_astronauts": "Astro starter example (API call plus dynamic mapping), not part of the S3-to-Snowflake family; out of scope for this trial.",
}
REPLACED = "Replaced by the S3ToSnowflakeLoad component (one class, one YAML instance per DAG); component loads under dg check defs and passes 19/19 parity checks."
for u in m["units"]:
    dag = u.get("dag_id") or ""; k = u["kind"]; tid = u.get("task_id", "")
    if k in ("taskgroup_template", "blueprint_template") or dag == "_s3_to_snowflake_dag":
        u["target"] = {"none": REPLACED}; continue
    if k == "dag_factory" or k == "blueprint_dag":
        u["target"] = {}                                   # handled by DEFER below (examples 5 and 6)
    if dag in DAG.values():
        t = TABLE[dag]
        if k == "factory_call":
            u["target"] = {"assets": [t], "deps": {t: [SRC[dag]]}}
        elif k == "task":
            if tid.endswith("copy_into_snowflake"):
                u["target"] = {"assets": [t], "deps": {t: [SRC[dag]]}}
            elif tid.endswith("post_load_transform"):
                a = f"{dag}/post_load_transform"; u["target"] = {"assets": [a], "deps": {a: [t]}}
            else:
                u["target"] = {"none": f"step folded into the {t} asset (list/manifest/log produce no data product; the manifest is recorded as metadata)"}
        else:  # dag
            u["target"] = {"jobs": [dag], "schedules": [{"name": f"{dag}_schedule", "cron": "0 0 * * *"}]}
defs = json.loads(subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd="trial/reusable_trial", capture_output=True, text=True).stdout.split("\n", 0)[0][subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd="trial/reusable_trial", capture_output=True, text=True).stdout.find("{"):])
passed, unplanned, problems = validate_defs.check(m, defs)
print("gate:", len(passed), "match,", len(problems), "problems,", len(unplanned), "unplanned"); [print("  PROBLEM", p) for p in problems]
EVID = "dg check defs loads; validate_defs target matches; parity: 19/19 checks (COPY SQL, S3 prefix, session overrides, manifest, cron, ds at ticks) against the real Airflow DAG objects"
by_id = {u["id"]: u for u in m["units"]}
for uid in passed:
    status.advance(m, uid, EVID)
for uid in unplanned:
    u = by_id[uid]; why = DEFER.get(u.get("dag_id") or "", None)
    if why is None and u["kind"] == "factory_call": why = DEFER.get(u.get("dag_id"), None)
    status.defer(m, uid, why or "no plan recorded")
json.dump(m, open("m_reusable_final.json", "w"), indent=1, default=str)
print(status.summary(m)[0].splitlines()[0]); print("complete gate exit:", 0 if status.summary(m)[1] else 1)
