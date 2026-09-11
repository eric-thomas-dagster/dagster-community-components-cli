#!/usr/bin/env bash
# data_contract — @data_contract producer + @requires_contract consumer
#                 + breaking-change detection + JSON Schema import.
#
# Fully offline. Demonstrates 4 features + BOTH shapes of the API:
#
#   SHAPE 1 (main flow): @data_contract + @requires_contract Python
#     decorators — one producer, two consumers (satisfied + version-fail).
#
#   SHAPE 2 (bonus):     DataContractComponent YAML (with `compute:`)
#     RequiresContractComponent YAML (with `wraps:` composability over
#     another DCC component).
#
# What runs:
#   RUN 1  materialize orders (v1.0.0)      → contract observation emitted
#   RUN 2  materialize daily_totals         → @requires_contract PASSES
#   RUN 3  materialize strict_consumer      → @requires_contract FAILS
#                                             (min_version=3.0.0 > 1.0.0)
#   RUN 4  rewrite orders to v2.0.0 with `status` DROPPED + `amount`
#          narrowed float→int; materialize → detect_breaking_changes emits
#          an ADDITIONAL AssetObservation tagged contract_breaking_change
#   RUN 5  YAML shape: materialize orders_yaml (DataContractComponent)
#   BONUS  contract_from_json_schema('/tmp/schema.json') → DCC contract dict
#
# 100% offline (no API keys, no external services).

set -eo pipefail

PROJECT_DIR="${1:-data-contract-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Env ---------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC" pandas

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: PYTHON DECORATORS ══════════════════════════════════════════
# @data_contract on the producer + @requires_contract on two consumers.

# --- PRODUCER: orders (v1.0.0) --------------------------------------------
mkdir -p "$DEFS/orders"
cat > "$DEFS/orders/__init__.py" <<'PY'
from .asset import orders
PY
cat > "$DEFS/orders/asset.py" <<'PY'
"""SHAPE 1 — @data_contract producer.

Contract v1.0.0:
  - order_id     str, not-null, unique
  - amount       float, not-null, min=0
  - status       str, allowed_values=[placed, shipped, cancelled]

Every materialization emits an AssetObservation with a
`contract_snapshot` metadata blob — consumers (`@requires_contract`)
read that snapshot from the event log to enforce version + schema
requirements BEFORE their compute runs.

`detect_breaking_changes=True` also diffs the current contract against
the PRIOR emission and emits a second `contract_breaking_change=true`
observation on any dropped column / narrowed type / nullable→not-null.
"""
import pandas as pd
import dagster as dg
from dagster_community_components import data_contract


@data_contract(
    contract={
        "version": "1.0.0",
        "owners": ["data-platform@example.com"],
        "consumers": ["analytics-team", "finance"],
        "schema": [
            {"name": "order_id", "type": "string", "nullable": False, "unique": True},
            {"name": "amount",   "type": "float64", "nullable": False, "min": 0},
            {"name": "status",   "type": "string", "allowed_values": ["placed", "shipped", "cancelled"]},
        ],
    },
    on_violation="block",
    detect_breaking_changes=True,
    on_breaking_change="warn",   # emit observation but don't fail the run
)
@dg.asset(group_name="producer")
def orders(context) -> pd.DataFrame:
    context.log.info("[orders] building 5 rows @ contract v1.0.0")
    return pd.DataFrame({
        "order_id": ["A1", "A2", "A3", "A4", "A5"],
        "amount":   [10.50, 22.00, 3.25, 47.75, 8.00],
        "status":   ["placed", "shipped", "placed", "cancelled", "shipped"],
    })
PY

# --- CONSUMER (satisfied): daily_totals -----------------------------------
mkdir -p "$DEFS/daily_totals"
cat > "$DEFS/daily_totals/__init__.py" <<'PY'
from .asset import daily_totals
PY
cat > "$DEFS/daily_totals/asset.py" <<'PY'
"""SHAPE 1 — @requires_contract consumer, PASSING variant.

Requires the upstream `orders` contract to be present + >= v1.0.0 +
have `order_id` + `amount` columns declared. Producer just emitted
v1.0.0 with both columns → this consumer passes and emits its own
`requires_contract_satisfied=true` observation.
"""
import dagster as dg
from dagster_community_components import requires_contract


@dg.asset(group_name="consumer", deps=["orders"])
@requires_contract(
    upstream="orders",
    min_version="1.0.0",
    require_columns=["order_id", "amount"],
)
def daily_totals(context):
    context.log.info("[daily_totals] upstream contract check PASSED — compute would run here")
    return {"rows_seen": 5}
PY

# --- CONSUMER (strict, will FAIL): strict_consumer ------------------------
mkdir -p "$DEFS/strict_consumer"
cat > "$DEFS/strict_consumer/__init__.py" <<'PY'
from .asset import strict_consumer
PY
cat > "$DEFS/strict_consumer/asset.py" <<'PY'
"""SHAPE 1 — @requires_contract consumer, FAILING variant.

Requires min_version=3.0.0 but the upstream producer only emits v1.0.0
→ @requires_contract raises dg.Failure BEFORE compute runs. Message:

    upstream contract version 1.0.0 < required 3.0.0
"""
import dagster as dg
from dagster_community_components import requires_contract


@dg.asset(group_name="consumer", deps=["orders"])
@requires_contract(
    upstream="orders",
    min_version="3.0.0",   # producer is 1.0.0 — this will FAIL
)
def strict_consumer(context):
    context.log.info("[strict_consumer] this line should NEVER print")
    return {"rows_seen": 0}
PY

# ═══ SHAPE 2: YAML — DataContractComponent + RequiresContractComponent ════
# Same enforcement engine, no Python. The compute callable is referenced
# via `compute.python: 'mod:fn'` so the YAML shape stays a shell around
# the contract itself.

# Compute callable for the YAML producer shape:
cat > "$DEFS/yaml_compute.py" <<'PY'
"""Compute callable referenced by defs/orders_yaml/defs.yaml + defs/yaml_consumer/defs.yaml."""
import pandas as pd


def build_orders_yaml(context=None) -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["Y1", "Y2", "Y3"],
        "amount":   [12.0, 5.5, 33.25],
        "status":   ["placed", "shipped", "placed"],
    })


def build_consumer_yaml(context=None):
    return {"rows_seen": 3, "note": "YAML-shape consumer ran"}
PY

# YAML-shape PRODUCER — DataContractComponent
mkdir -p "$DEFS/orders_yaml"
cat > "$DEFS/orders_yaml/defs.yaml" <<YAML
# SHAPE 2 — DataContractComponent. Same contract engine as @data_contract,
# but the whole asset is declared in YAML. Compute is a mod:fn reference.
type: dagster_community_components.DataContractComponent
attributes:
  asset_name: orders_yaml
  group_name: yaml_shape
  compute:
    kind: python
    python: "${PKG}.defs.yaml_compute:build_orders_yaml"
  contract:
    version: "1.0.0"
    owners: [data-platform@example.com]
    consumers: [analytics-team]
    schema:
      - {name: order_id, type: string,  nullable: false, unique: true}
      - {name: amount,   type: float64, nullable: false, min: 0}
      - {name: status,   type: string,  allowed_values: [placed, shipped, cancelled]}
  on_violation: block
  detect_breaking_changes: true
  on_breaking_change: warn
YAML

# YAML-shape CONSUMER — RequiresContractComponent
mkdir -p "$DEFS/yaml_consumer"
cat > "$DEFS/yaml_consumer/defs.yaml" <<YAML
# SHAPE 2 — RequiresContractComponent. Consumer-side contract gate in YAML.
# Same engine as @requires_contract. Uses \`compute:\` here for simplicity;
# the more powerful mode is \`wraps:\` — see README for the composability
# pattern that lets you gate ANY DCC component's compute with an upstream
# contract check (e.g. \`wraps: DataframeTransformerComponent\`).
type: dagster_community_components.RequiresContractComponent
attributes:
  asset_name: yaml_consumer
  group_name: yaml_shape
  upstream: orders_yaml
  min_version: "1.0.0"
  require_columns: [order_id, amount]
  compute:
    kind: python
    python: "${PKG}.defs.yaml_compute:build_consumer_yaml"
YAML

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect="$3"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  # Some runs are EXPECTED to fail (e.g. strict_consumer) — never fail the
  # setup script on a bad exit code, we inspect the log instead.
  uv run dg launch --assets "$asset" >"$LOG" 2>&1 || true
  { grep -E '\[orders\]|\[daily_totals\]|\[strict_consumer\]|contract|breaking|Failure|STEP_SUCCESS|STEP_FAILURE|dg\.Failure' "$LOG" || true; } | head -20 | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATORS (@data_contract + @requires_contract) ═══"
_run 1 orders          "contract v1.0.0 emitted — 5 rows, all checks pass"
_run 2 daily_totals    "requires_contract PASSES (contract >= v1.0.0)"
_run 3 strict_consumer "requires_contract FAILS (upstream v1.0.0 < required v3.0.0)"

# --- 6. Breaking-change detection ----------------------------------------
# Rewrite the producer with a NEW contract (v2.0.0) that DROPS the `status`
# column + narrows `amount` from float64 → int64. Re-materialize → the
# outer @data_contract(detect_breaking_changes=True) diffs current vs
# prior and emits an ADDITIONAL AssetObservation tagged
# contract_breaking_change=true with a markdown summary.

echo ""
echo "═══ BREAKING-CHANGE DETECTION ═══"
echo ">>> Rewriting src/$PKG/defs/orders/asset.py with contract v2.0.0"
echo "    (drops \`status\` column; narrows \`amount\` from float64 → int64)"
cat > "$DEFS/orders/asset.py" <<'PY'
"""Producer REWRITTEN — contract v2.0.0.

Changes from v1.0.0:
  - `status` column DROPPED       → breaking (dropped_column)
  - `amount` type float64 → int64 → breaking (narrowed_type)

With `detect_breaking_changes=True`, the next materialization emits an
extra AssetObservation tagged `contract_breaking_change=true` with a
rendered markdown summary. `on_breaking_change="warn"` keeps the run
green (flip to `"fail"` for a hard block).
"""
import pandas as pd
import dagster as dg
from dagster_community_components import data_contract


@data_contract(
    contract={
        "version": "2.0.0",
        "owners": ["data-platform@example.com"],
        "consumers": ["analytics-team", "finance"],
        "schema": [
            {"name": "order_id", "type": "string", "nullable": False, "unique": True},
            # amount narrowed float64 → int64  ← breaking
            {"name": "amount",   "type": "int64", "nullable": False, "min": 0},
            # status DROPPED  ← breaking
        ],
    },
    on_violation="block",
    detect_breaking_changes=True,
    on_breaking_change="warn",
)
@dg.asset(group_name="producer")
def orders(context) -> pd.DataFrame:
    context.log.info("[orders] building 5 rows @ contract v2.0.0 (BREAKING)")
    return pd.DataFrame({
        "order_id": ["B1", "B2", "B3", "B4", "B5"],
        "amount":   [11, 22, 3, 47, 8],
    })
PY

_run 4 orders "contract v2.0.0 — expect ADDITIONAL contract_breaking_change observation"

# --- 7. YAML shape --------------------------------------------------------
echo ""
echo "═══ SHAPE 2: YAML (DataContractComponent + RequiresContractComponent) ═══"
_run 5 orders_yaml   "DataContractComponent — YAML shape emits contract v1.0.0"
_run 6 yaml_consumer "RequiresContractComponent — YAML consumer passes upstream v1.0.0"

# --- 8. JSON Schema import demo ------------------------------------------
# contract_from_json_schema('...') reads a JSON Schema file + returns a
# DCC contract dict. Teams that already publish schemas as JSON Schema
# (OpenAPI, event bus, contract-registry, etc.) don't have to hand-mirror
# them into DCC contract dicts.
echo ""
echo "═══ BONUS: contract_from_json_schema('/tmp/orders.schema.json') ═══"
cat > "$PROJECT_ABS/orders.schema.json" <<'JSON'
{
  "$id": "https://example.com/orders.schema.json",
  "title": "Order",
  "type": "object",
  "required": ["order_id", "amount"],
  "properties": {
    "order_id": {"type": "string", "pattern": "^[A-Z][0-9]+$"},
    "amount":   {"type": "number", "minimum": 0, "maximum": 1000000},
    "status":   {"type": "string", "enum": ["placed", "shipped", "cancelled"]},
    "email":    {"type": ["string", "null"]}
  }
}
JSON

uv run python - <<PY | sed 's/^/    /'
import json
from dagster_community_components import contract_from_json_schema

contract = contract_from_json_schema(
    "$PROJECT_ABS/orders.schema.json",
    version="1.0.0",
    owners=["data-platform@example.com"],
    consumers=["analytics-team"],
)
print("Loaded DCC contract dict from orders.schema.json:")
print(json.dumps(contract, indent=2, default=str))
PY

# --- 9. Query the event log for contract observations -------------------
echo ""
echo ">>> Contract observations from the event log:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'version':<8}  {'breaking':<9}  {'satisfied':<10}  tags/summary")
    print(f"    {'-----':<20}  {'-------':<8}  {'--------':<9}  {'---------':<10}  ------------")
    for asset_name in ("orders", "daily_totals", "strict_consumer", "orders_yaml", "yaml_consumer"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs:
                continue
            tags = dict(obs.tags or {})
            version = tags.get("contract_version", "-")
            breaking = tags.get("contract_breaking_change", "-")
            satisfied = tags.get("requires_contract_satisfied", "-")
            extra = ""
            if breaking == "true":
                extra = f" prior={tags.get('contract_prior_version','?')} → curr={tags.get('contract_current_version','?')}"
            elif satisfied == "true":
                extra = f" upstream_v={tags.get('upstream_contract_version','?')} req>={tags.get('required_min_version','?')}"
            print(f"    {asset_name:<20}  {version:<8}  {breaking:<9}  {satisfied:<10}  {extra}")
PY

# --- Explainer -----------------------------------------------------------
cat <<DONE

✓ data_contract demo done.

Four features exercised over one flowing pipeline:

  1. PRODUCER (@data_contract v1.0.0)
       src/$PKG/defs/orders/asset.py
       emits AssetObservation with contract_snapshot metadata
       → consumers read that snapshot from the event log.

  2. CONSUMER SATISFIED (@requires_contract, min_version=1.0.0)
       src/$PKG/defs/daily_totals/asset.py
       upstream v1.0.0 >= required v1.0.0 → PASSES, emits its own
       requires_contract_satisfied=true observation.

  3. CONSUMER FAILS (@requires_contract, min_version=3.0.0)
       src/$PKG/defs/strict_consumer/asset.py
       upstream v1.0.0 < required v3.0.0 → dg.Failure BEFORE compute.
       "upstream contract version 1.0.0 < required 3.0.0"

  4. BREAKING-CHANGE DETECTION (detect_breaking_changes=True)
       Producer rewritten to v2.0.0: status dropped + amount narrowed.
       Next materialization emits an ADDITIONAL observation tagged
       contract_breaking_change=true with a markdown summary of the diff.

  5. YAML SHAPE (DataContractComponent + RequiresContractComponent)
       src/$PKG/defs/orders_yaml/defs.yaml
       src/$PKG/defs/yaml_consumer/defs.yaml
       Same engine, same events, zero Python — the contract lives in YAML.

  BONUS. contract_from_json_schema('/tmp/orders.schema.json')
       Turn a JSON Schema file into a DCC contract dict — good for teams
       already publishing schemas (OpenAPI, event bus, contract-registry).

Every enforcement primitive is a Dagster event:
  - Schema violations   → AssetCheckResult per column
  - Freshness / SLA     → runtime AssetCheckResult vs. prior materialization
  - Contract version    → asset code_version (Dagster detects bumps in the UI)
  - Ownership / consumers → AssetObservation tags (searchable in the log)
  - Breaking changes    → AssetObservation with rendered markdown summary
  - Downstream gating   → AutomationCondition.eager() on failing check

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → Assets → orders → Checks + Observations panel

Cleanup: rm -rf $PROJECT_ABS
DONE
