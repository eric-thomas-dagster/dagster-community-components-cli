# Worked example: planning waves

Two runs of `scripts/plan.py`. The numbers in `plan.md` are heuristics (the file says so and prints the formula); this folder only shows what the output looks like.

1. **Static only** (this folder): `dags/` is seven small DAG files, one per pattern (a Cosmos `DbtDag`, S3 to Snowflake, plain Python, a sensor-driven load, branching, a Kubernetes pod, a notification). `manifest.json` is `inventory.py` on that folder. Nothing was executed. Without the IR and the conversion report the plan can only use what the scanner sees, so most DAGs read as "needs care".

   ```bash
   python3 ../../scripts/inventory.py dags --out manifest.json
   python3 ../../scripts/plan.py manifest.json --out-dir .          # writes plan.md and plan.json
   ```

2. **With the IR and the conversion report** (`with_conversion/`): the Cosmos fixture DAGs from `scripts/tests/fixtures_cosmos`, with the IR `airflow_ir.py` extracted and the report `convert_ir.py` wrote. The converter produced all three without a stubbed task, so they plan as mechanical, half the effort, wave 1.

   ```bash
   python3 ../../scripts/plan.py with_conversion/manifest.json --ir with_conversion/ir.json \
     --report with_conversion/report.json --out-dir with_conversion
   ```

The plan is deterministic: running it again on the same files gives identical `plan.md` and `plan.json` (a test checks the committed copies).
