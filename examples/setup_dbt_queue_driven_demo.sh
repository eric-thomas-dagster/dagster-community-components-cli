#!/usr/bin/env bash
# setup_dbt_queue_driven_demo.sh
#
# **Message-driven dbt orchestration** — a self-contained walkthrough of the
# pattern where an external system sends messages to Dagster saying either
# "build this model with these vars" or "build all models", Dagster picks
# them up via a sensor, runs the right job, and publishes a completion
# message back out.
#
# Simulated end-to-end (no external queue): the sensor generates messages
# randomly (80% single-model / 15% skip / 5% run-all) so the demo runs
# with just `dg dev` — no Docker, no queue broker.
#
# What it demonstrates
#   • Subclassing dagster-dbt's DbtProjectComponent to accept runtime vars
#     (the same pattern you'd use for full_refresh, threads, target, etc.)
#   • Two @job defs — one for the single-model path, one for run-all —
#     both wrapping the same dbt asset set
#   • A @sensor that emits RunRequests with asset_selection + run_config
#     (the vars flow through via the op's Config)
#   • A queue-completion @asset with deps on all dbt models — materializes
#     LAST in every run, writes a success message to output_queue.jsonl
#
# Cost: $0. Everything local. No credentials.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_dbt_queue_driven_demo.sh                      # → dbt_queue_demo/
#   ./setup_dbt_queue_driven_demo.sh my_pipeline          # custom name

set -eo pipefail

PROJECT_NAME="${1:-dbt_queue_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-dbt + dbt-duckdb)…"
uv add --quiet \
  'dagster-dbt>=0.29.0' 'dbt-core>=1.7.0' 'dbt-duckdb>=1.7.0' 'duckdb>=0.9.0' \
  || fail "uv add failed"
ok "Dependencies installed"

# ── dbt project ─────────────────────────────────────────────────────────────
mkdir -p dbt_project/{models,seeds}

cat > dbt_project/dbt_project.yml <<'YAML'
name: 'demo'
version: '1.0.0'
profile: 'demo'
model-paths: ["models"]
seed-paths: ["seeds"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  demo:
    +materialized: table

seeds:
  demo:
    +quote_columns: false

# The var that the sensor injects per single-model run.
vars:
  start_date: "1900-01-01"
YAML

cat > dbt_project/profiles.yml <<'YAML'
demo:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: demo.duckdb
      threads: 4
YAML

# ── Seed CSVs ───────────────────────────────────────────────────────────────
cat > dbt_project/seeds/raw_customers.csv <<'CSV'
customer_id,email,signup_date,plan
1,alice@example.com,2024-01-15,premium
2,bob@example.com,2024-02-20,basic
3,carol@example.com,2024-03-10,premium
4,dave@example.com,2024-01-05,basic
5,eve@example.com,2024-04-22,premium
CSV

cat > dbt_project/seeds/raw_orders.csv <<'CSV'
order_id,customer_id,order_date,amount
1001,1,2024-10-15,120.50
1002,1,2024-11-20,85.00
1003,2,2024-10-05,45.00
1004,3,2024-10-22,300.00
1005,3,2024-11-30,250.00
1006,4,2024-09-15,30.00
1007,5,2024-11-08,175.50
1008,1,2025-01-10,150.75
1009,3,2025-01-05,220.00
1010,5,2025-01-25,165.00
CSV

# ── dbt models ──────────────────────────────────────────────────────────────
cat > dbt_project/models/customers.sql <<'SQL'
{{ config(materialized='table') }}
select
  customer_id,
  email,
  signup_date,
  plan
from {{ ref('raw_customers') }}
SQL

# THIS model uses the `start_date` var — the sensor injects it per run.
cat > dbt_project/models/orders.sql <<'SQL'
{{ config(materialized='table') }}
select
  order_id,
  customer_id,
  order_date,
  amount
from {{ ref('raw_orders') }}
where order_date >= '{{ var("start_date") }}'
SQL

cat > dbt_project/models/order_summary.sql <<'SQL'
{{ config(materialized='table') }}
select
  c.customer_id,
  c.email,
  c.plan,
  count(o.order_id)         as total_orders,
  coalesce(sum(o.amount), 0) as total_revenue,
  max(o.order_date)         as last_order_date
from {{ ref('customers') }} c
left join {{ ref('orders') }} o using (customer_id)
group by 1, 2, 3
order by total_revenue desc nulls last
SQL

ok "Wrote dbt project (2 seeds + 3 models — 'orders' uses {{ var('start_date') }})"

# ── Compile the manifest ────────────────────────────────────────────────────
info "Running dbt seed + parse to produce target/manifest.json…"
(cd dbt_project && uv run dbt seed --profiles-dir . 2>&1 | tail -3) || fail "dbt seed failed"
(cd dbt_project && uv run dbt parse --profiles-dir . 2>&1 | tail -3) || fail "dbt parse failed"
ok "Manifest compiled"

# ── Custom dbt component subclass ───────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/lib"
touch "src/${PROJECT_NAME}/lib/__init__.py"

cat > "src/${PROJECT_NAME}/lib/custom_dbt.py" <<'PY'
"""Custom subclass of DbtProjectComponent — accepts runtime vars via op config.

The stock DbtProjectComponent lets you set static cli_args or partition-context
templates. For message-driven runs where a sensor injects arbitrary vars per
request, we override op_config_schema + get_cli_args so `--vars` gets appended
from the run's op_config.
"""
import json
from collections.abc import Iterator
from typing import Any

import dagster as dg
from dagster_dbt import DbtCliResource, DbtProjectComponent


class DbtRunVars(dg.Config):
    """Per-run overrides for the dbt op. All fields optional — omit to use
    dbt defaults.

    - vars:       dict passed as `--vars '<json>'` (per-model vars)
    - state_path: string passed as `--state` — useful for `--defer --state ./prod`
                  (dev-iteration pattern: missing upstream models borrow from prod).
                  Path must exist on the AGENT filesystem when the op runs.

    NOTE: no `select` field. `dbt.cli(context=context)` already auto-adds `--select`
    for the assets Dagster's job selection has narrowed to; adding another one
    would UNION (dbt's behavior for multiple --select flags), not narrow further.
    For Slim CI ("only run state-modified models"), narrow at Dagster's
    asset_selection level in your CI launch — see dbt_slim_ci walkthrough.
    """
    vars: dict[str, Any] = {}
    state_path: str | None = None


class DbtProjectWithRuntimeVarsComponent(DbtProjectComponent):
    """DbtProjectComponent that accepts per-run CLI arg overrides via op config.

    Common uses:
      1. Sensor injects per-message vars for a single-model build (see the
         message-driven dbt demo).
      2. Dev-iteration --defer: pass state_path so dbt borrows missing
         upstreams from prod artifacts rather than building them locally.
    """

    @property
    def op_config_schema(self) -> type[dg.Config] | None:
        return DbtRunVars

    def get_cli_args(self, context: dg.AssetExecutionContext) -> list[str]:
        args = super().get_cli_args(context)
        cfg = context.op_execution_context.op_config
        if cfg.get("vars"):
            args += ["--vars", json.dumps(cfg["vars"])]
        if cfg.get("state_path"):
            args += ["--state", cfg["state_path"]]
        return args
PY

ok "Wrote lib/custom_dbt.py — DbtProjectWithRuntimeVarsComponent subclass"

# ── defs/dbt/defs.yaml — the dbt component instance ─────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/dbt"

cat > "src/${PROJECT_NAME}/defs/dbt/defs.yaml" <<YAML
type: ${PROJECT_NAME}.lib.custom_dbt.DbtProjectWithRuntimeVarsComponent
attributes:
  project: "{{ project_root }}/dbt_project"
  op:
    name: dbt_op
YAML

ok "Wrote defs/dbt/defs.yaml"

# ── defs/queue_completion.py — the write-back asset ─────────────────────────
cat > "src/${PROJECT_NAME}/defs/queue_completion.py" <<'PY'
"""queue_completion — the success-only write-back asset.

Materializes AFTER every dbt model in the run (via deps on all dbt models).
Writes one JSON line to output_queue.jsonl with what was built + which vars
were used. Failure → dbt op fails → completion asset never runs → nothing is
appended to the queue. That's the intended behavior: successes only.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dagster as dg


class CompletionConfig(dg.Config):
    action: str = "run_all"      # "run_model" | "run_all"
    model: str | None = None      # populated for run_model, None for run_all
    vars: dict[str, Any] = {}


@dg.asset(
    deps=[
        dg.AssetKey("customers"),
        dg.AssetKey("orders"),
        dg.AssetKey("order_summary"),
    ],
    group_name="queue",
    description=(
        "Publishes a success message to output_queue.jsonl after dbt "
        "materialization. Runs last in every dbt run via deps on all "
        "dbt models."
    ),
)
def queue_completion(
    context: dg.AssetExecutionContext, config: CompletionConfig
) -> dg.MaterializeResult:
    payload = {
        "run_id": context.run_id,
        "action": config.action,
        "model": config.model,
        "vars": config.vars,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "success",
    }
    output_path = Path("output_queue.jsonl")
    with output_path.open("a") as f:
        f.write(json.dumps(payload) + "\n")
    context.log.info(f"Published to output_queue.jsonl: {payload}")
    return dg.MaterializeResult(
        metadata={
            "payload": dg.MetadataValue.json(payload),
            "output_queue_path": dg.MetadataValue.path(str(output_path.resolve())),
        }
    )
PY

ok "Wrote defs/queue_completion.py"

# ── defs/jobs.py — two jobs, one for single, one for all ────────────────────
cat > "src/${PROJECT_NAME}/defs/jobs.py" <<'PY'
"""Two @job defs — one for single-model runs, one for run-all.

Both jobs wrap the SAME asset set (all dbt models + queue_completion). The
distinction is which assets get selected at RunRequest time:

  - run_single_model_job: sensor narrows via RunRequest(asset_selection=[<model>, queue_completion])
  - run_all_models_job:   no narrowing — full asset set materializes
"""
import dagster as dg

_dbt_selection = (
    dg.AssetSelection.groups("customers")
    | dg.AssetSelection.groups("orders")
    | dg.AssetSelection.groups("order_summary")
    | dg.AssetSelection.assets(dg.AssetKey("customers"))
    | dg.AssetSelection.assets(dg.AssetKey("orders"))
    | dg.AssetSelection.assets(dg.AssetKey("order_summary"))
    | dg.AssetSelection.assets(dg.AssetKey("queue_completion"))
)

run_single_model_job = dg.define_asset_job(
    "run_single_model_job",
    selection=_dbt_selection,
    description="Message-driven single-model build. Sensor narrows the asset_selection per RunRequest.",
)

run_all_models_job = dg.define_asset_job(
    "run_all_models_job",
    selection=_dbt_selection,
    description="Message-driven full-project build. Materializes every dbt model + queue_completion.",
)
PY

ok "Wrote defs/jobs.py"

# ── defs/sensors.py — the simulated queue sensor ────────────────────────────
cat > "src/${PROJECT_NAME}/defs/sensors.py" <<'PY'
"""queue_sensor — simulates reading messages from an external queue.

Weighted random on every tick:
  • 80% — single-model request for `orders` with fresh `start_date` var
  • 15% — no message (skip)
  •  5% — run-all request

In a real deployment the random block gets swapped for reading from
RabbitMQ / Redis Streams / SQS / Kafka and parsing the payload.
"""
import random
import uuid
from datetime import date, timedelta

import dagster as dg

from .jobs import run_single_model_job, run_all_models_job


def _random_start_date() -> str:
    """Any date within the last 400 days — proves vars flow end-to-end."""
    offset = random.randint(0, 400)
    return (date.today() - timedelta(days=offset)).isoformat()


@dg.sensor(
    jobs=[run_single_model_job, run_all_models_job],
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def queue_sensor(context: dg.SensorEvaluationContext):
    """Simulates polling an input queue. Yields RunRequests with the right
    job + asset_selection + run_config based on the message payload."""
    roll = random.random()

    if roll < 0.15:
        yield dg.SkipReason("simulated: no message in queue this tick")
        return

    run_key = str(uuid.uuid4())

    if roll < 0.95:
        # 80% window (0.15 → 0.95): single-model request
        start_date = _random_start_date()
        context.log.info(
            f"simulated queue msg: run_model orders with start_date={start_date}"
        )
        yield dg.RunRequest(
            run_key=run_key,
            job_name="run_single_model_job",
            asset_selection=[
                dg.AssetKey("orders"),
                dg.AssetKey("queue_completion"),
            ],
            run_config={
                "ops": {
                    "dbt_op": {"config": {"vars": {"start_date": start_date}}},
                    "queue_completion": {
                        "config": {
                            "action": "run_model",
                            "model": "orders",
                            "vars": {"start_date": start_date},
                        }
                    },
                }
            },
        )
    else:
        # 5% window (0.95 → 1.0): run-all
        context.log.info("simulated queue msg: run_all")
        yield dg.RunRequest(
            run_key=run_key,
            job_name="run_all_models_job",
            run_config={
                "ops": {
                    "dbt_op": {"config": {"vars": {}}},
                    "queue_completion": {
                        "config": {"action": "run_all", "model": None, "vars": {}}
                    },
                }
            },
        )
PY

ok "Wrote defs/sensors.py"

# ── Validate ────────────────────────────────────────────────────────────────
info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -8 || fail "dg check defs failed"
ok "Definitions validated"

# ── Final message ───────────────────────────────────────────────────────────
cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

Next steps:

  cd $PROJECT_NAME
  uv run dg dev

Then in the UI (http://localhost:3000):

  • Assets tab       → see 3 dbt models (customers / orders / order_summary)
                       + queue_completion (group: queue)
  • Automation tab   → queue_sensor is running by default; it'll fire every
                       ~30s with weighted random messages.
  • Runs tab         → watch runs come in. Most will be single-model 'orders'
                       runs with a fresh start_date var per run.
  • output_queue.jsonl → tail this file in your terminal to see success
                       messages accumulate:
                         tail -f $PROJECT_NAME/output_queue.jsonl

  Manual test — bypass the sensor:
    Launchpad → run_single_model_job → paste config:
       ops:
         dbt_op:
           config:
             vars: {start_date: "2025-01-01"}
         queue_completion:
           config:
             action: run_model
             model: orders
             vars: {start_date: "2025-01-01"}
    Select assets: orders, queue_completion. Materialize.

EOF
