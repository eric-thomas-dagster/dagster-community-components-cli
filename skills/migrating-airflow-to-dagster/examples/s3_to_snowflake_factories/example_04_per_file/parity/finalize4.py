import json, subprocess, sys
sys.path.insert(0, sys.argv[1]); import status, validate_defs
m = json.load(open("m_reusable_final.json"))
DAG = "example_04_s3_to_snowflake_dynamic_task_mapping"; TABLE = "analytics/raw/orders_raw"; SRC = "s3/example-landing-bucket/orders"
ARCH = "s3/example-landing-bucket/processed/orders"
PLUMB = "plumbing replaced by the partition key (the file) plus landed_files() and archive_key_for(), both checked against Airflow's own task functions"
plan = {"list_landing_files": {"none": "folded into the schedule: it lists the day's prefix, registers each file as a dynamic partition and requests one run per file"},
        "copy_file_batches": {"none": PLUMB}, "archive_kwargs": {"none": PLUMB}, "delete_kwargs": {"none": PLUMB},
        "copy_file_into_snowflake": {"assets": [TABLE], "deps": {TABLE: [SRC]}},
        "archive_landing_file": {"assets": [ARCH], "deps": {ARCH: [TABLE]}}, "delete_landing_file": {"assets": [ARCH], "deps": {ARCH: [TABLE]}}}
mine = [u for u in m["units"] if u.get("dag_id") == DAG]
for u in mine:
    status.reopen(m, u["id"], "converted with dynamic partitions")
    u["target"] = {"jobs": [DAG], "schedules": [{"name": f"{DAG}_schedule", "cron": "0 0 * * *"}]} if u["kind"] == "dag" else plan[u["task_id"]]
defs = json.loads(subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd="trial/ex04_trial", capture_output=True, text=True).stdout[subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd="trial/ex04_trial", capture_output=True, text=True).stdout.find("{"):])
passed, unplanned, problems = validate_defs.check({"units": mine}, defs)
print("gate on example_04 units:", len(passed), "match,", len(problems), "problems,", len(unplanned), "unplanned"); [print("  PROBLEM", p) for p in problems]
EV = ("dg check defs loads (separate project: example_04 produces the same table as example_01, so the two designs cannot coexist in one deployment); "
      "validate_defs target matches; parity: 24/24 against the real Airflow DAG (compare4.py)")
for uid in passed: status.advance(m, uid, EV)
json.dump(m, open("m_reusable_final.json", "w"), indent=1, default=str)
print(status.summary(m)[0].splitlines()[0])
