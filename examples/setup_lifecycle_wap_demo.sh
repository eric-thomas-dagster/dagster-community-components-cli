#!/usr/bin/env bash
# lifecycle_wap — @lifecycle decorator + LifecycleWapComponent: Write-Audit-Publish
#
# Fully offline. Demonstrates BOTH shapes of the DCC WAP API against a local
# parquet backend:
#
#   SHAPE 1: @lifecycle decorator — wraps a plain @dg.asset (Python)
#   SHAPE 2: LifecycleWapComponent (YAML) — compute.python references a callable;
#            same audit + publish engine
#
# Full Write → Audit → Publish cycle shown in TWO RUNS:
#
#   RUN 1 (py_orders_happy)   — clean data → stage → audits pass → PUBLISH
#   RUN 2 (py_orders_bad)     — bad data  → stage → audits fail → QUARANTINE
#                                (production parquet unchanged, quarantined
#                                 file preserved for triage)
#   RUN 3 (yaml_orders_happy) — same shape as RUN 1 but through the YAML
#                                LifecycleWapComponent shape
#
# 100% offline (no API keys, no cloud creds, no Iceberg/Delta/GE installs).

set -eo pipefail

PROJECT_DIR="${1:-lifecycle-wap-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1
fi

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

# WAP staging + prod + quarantine directories — all inside the project dir
# for Windows path portability.
WAP_ROOT="$PROJECT_ABS/.wap"
mkdir -p "$WAP_ROOT/staging" "$WAP_ROOT/prod" "$WAP_ROOT/quarantine"

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC" pandas pyarrow

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1a: PYTHON DECORATOR — HAPPY PATH ═════════════════════════════
# @lifecycle wraps @dg.asset. Compute produces 100 clean orders. All audits
# pass → publish to prod. Downstream reader can read the published parquet.

cat > "$DEFS/py_orders_happy.py" <<PY
"""SHAPE 1 — @lifecycle decorator on @dg.asset (HAPPY PATH).

100 clean orders — all positive amounts, no null user_ids, unique order_ids.
All 3 audits pass → parquet is atomically promoted from staging to prod.
"""
import pandas as pd
import dagster as dg
from dagster_community_components import lifecycle

PROD_PATH = "$WAP_ROOT/prod/orders_happy.parquet"
QUARANTINE_PATH = "$WAP_ROOT/quarantine/orders_happy.parquet"


@dg.asset(
    group_name="python_decorator",
    check_specs=[
        dg.AssetCheckSpec(name="row_count_gte_50", asset="py_orders_happy"),
        dg.AssetCheckSpec(name="amount_positive_check", asset="py_orders_happy"),
        dg.AssetCheckSpec(name="user_id_no_nulls", asset="py_orders_happy"),
    ],
)
@lifecycle(
    write={
        "kind": "filesystem",
        "prod_path": PROD_PATH,
        "format": "parquet",
    },
    audit=[
        {"kind": "row_count_min", "min": 50, "name": "row_count_gte_50"},
        {"kind": "python", "python": "${PKG}.defs.audits:amount_positive_check",
         "name": "amount_positive_check"},
        {"kind": "col_null_ratio_max", "col": "user_id", "max": 0.0,
         "name": "user_id_no_nulls"},
    ],
    on_pass="publish",
    on_fail="quarantine",
    quarantine={"quarantine_path": QUARANTINE_PATH},
    raise_on_fail=True,
)
def py_orders_happy(context) -> pd.DataFrame:
    context.log.info("[py_orders_happy] compute — building 100 CLEAN orders")
    df = pd.DataFrame({
        "order_id": [f"ord-{i:04d}" for i in range(100)],
        "user_id":  [f"user-{i % 30:03d}" for i in range(100)],
        "amount":   [round(19.99 + (i * 1.17) % 200, 2) for i in range(100)],
        "status":   ["completed"] * 100,
    })
    context.log.info(f"[py_orders_happy] returning {len(df)} rows, min amount={df['amount'].min()}")
    return df
PY

# ═══ SHAPE 1b: PYTHON DECORATOR — BAD-DATA PATH ══════════════════════════
# Same shape, but compute produces 100 orders with 5 NEGATIVE amounts →
# amount_positive_check fails → publish is SKIPPED, staging is moved to
# the quarantine path. Production parquet is untouched.

cat > "$DEFS/py_orders_bad.py" <<PY
"""SHAPE 1 — @lifecycle decorator on @dg.asset (BAD-DATA PATH).

100 orders, but 5 have NEGATIVE amounts (refund bug simulation). The
amount_positive_check audit fails → the staging parquet is quarantined,
NOT published. Prod path stays at whatever the last good version was
(or absent, if this is the first run).

raise_on_fail=True (default) means the asset materialization fails —
downstream stays blocked until the bad data is triaged.
"""
import pandas as pd
import dagster as dg
from dagster_community_components import lifecycle

PROD_PATH = "$WAP_ROOT/prod/orders_bad.parquet"
QUARANTINE_PATH = "$WAP_ROOT/quarantine/orders_bad.parquet"


@dg.asset(
    group_name="python_decorator",
    check_specs=[
        dg.AssetCheckSpec(name="row_count_gte_50", asset="py_orders_bad"),
        dg.AssetCheckSpec(name="amount_positive_check", asset="py_orders_bad"),
        dg.AssetCheckSpec(name="user_id_no_nulls", asset="py_orders_bad"),
    ],
)
@lifecycle(
    write={
        "kind": "filesystem",
        "prod_path": PROD_PATH,
        "format": "parquet",
    },
    audit=[
        {"kind": "row_count_min", "min": 50, "name": "row_count_gte_50"},
        {"kind": "python", "python": "${PKG}.defs.audits:amount_positive_check",
         "name": "amount_positive_check"},
        {"kind": "col_null_ratio_max", "col": "user_id", "max": 0.0,
         "name": "user_id_no_nulls"},
    ],
    on_pass="publish",
    on_fail="quarantine",
    quarantine={"quarantine_path": QUARANTINE_PATH},
    raise_on_fail=True,
)
def py_orders_bad(context) -> pd.DataFrame:
    context.log.info("[py_orders_bad] compute — building 100 orders with 5 NEGATIVE amounts")
    amounts = [round(19.99 + (i * 1.17) % 200, 2) for i in range(100)]
    # Poison 5 rows with negative amounts
    for i in (7, 23, 44, 61, 88):
        amounts[i] = -amounts[i]
    df = pd.DataFrame({
        "order_id": [f"ord-{i:04d}" for i in range(100)],
        "user_id":  [f"user-{i % 30:03d}" for i in range(100)],
        "amount":   amounts,
        "status":   ["completed"] * 100,
    })
    context.log.info(f"[py_orders_bad] returning {len(df)} rows, {(df['amount'] < 0).sum()} with negative amounts")
    return df
PY

# ═══ SHARED: audit callable ══════════════════════════════════════════════
# One `kind: python` audit shared by all three assets. Referenced as
# '${PKG}.defs.audits:amount_positive_check'. Returns the {passed,
# description, metadata} dict shape the component normalizes.

cat > "$DEFS/audits.py" <<'PY'
"""Custom `kind: python` audits — receive df, return WAP-shaped dict."""
import pandas as pd


def amount_positive_check(df: pd.DataFrame) -> dict:
    """Every order must have amount > 0."""
    if "amount" not in df.columns:
        return {"passed": False, "description": "FAIL: 'amount' column missing",
                "metadata": {}}
    n_bad = int((df["amount"] <= 0).sum())
    n_total = int(len(df))
    passed = n_bad == 0
    return {
        "passed": passed,
        "description": (
            f"all {n_total} amounts > 0" if passed
            else f"FAIL: {n_bad}/{n_total} rows have amount <= 0"
        ),
        "metadata": {
            "n_rows": n_total,
            "n_negative_or_zero": n_bad,
            "min_amount": float(df["amount"].min()),
        },
    }
PY

# ═══ SHAPE 2: YAML COMPONENT — HAPPY PATH ════════════════════════════════
# LifecycleWapComponent references a compute callable via `compute.python`.
# Same audit + publish engine as the @lifecycle decorator; different
# authoring surface. Best when you're authoring a new asset from YAML.

mkdir -p "$DEFS/yaml_orders_happy"
cat > "$DEFS/yaml_orders_happy/defs.yaml" <<YAML
type: dagster_community_components.LifecycleWapComponent
attributes:
  asset_name: yaml_orders_happy
  group_name: yaml_component

  compute:
    kind: python
    python: "${PKG}.defs.compute:build_clean_orders"

  write:
    kind: filesystem
    prod_path: "$WAP_ROOT/prod/yaml_orders_happy.parquet"
    format: parquet

  audit:
    - kind: row_count_min
      min: 50
      name: row_count_gte_50
    - kind: python
      python: "${PKG}.defs.audits:amount_positive_check"
      name: amount_positive_check
    - kind: col_null_ratio_max
      col: user_id
      max: 0.0
      name: user_id_no_nulls

  on_pass: publish
  on_fail: quarantine
  quarantine:
    quarantine_path: "$WAP_ROOT/quarantine/yaml_orders_happy.parquet"
  raise_on_fail: true
YAML

# ═══ SHARED: compute callable for the YAML shape ═════════════════════════
# LifecycleWapComponent's compute.python signature is (context) -> DataFrame.

cat > "$DEFS/compute.py" <<'PY'
"""Compute callables referenced by LifecycleWapComponent YAML."""
import pandas as pd


def build_clean_orders(context) -> pd.DataFrame:
    """Same shape as py_orders_happy — 100 clean orders."""
    context.log.info("[yaml compute] building 100 CLEAN orders")
    return pd.DataFrame({
        "order_id": [f"y-ord-{i:04d}" for i in range(100)],
        "user_id":  [f"user-{i % 30:03d}" for i in range(100)],
        "amount":   [round(29.99 + (i * 2.11) % 175, 2) for i in range(100)],
        "status":   ["completed"] * 100,
    })
PY

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect_ok="$3"; local expect="$4"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  if [ "$expect_ok" = "ok" ]; then
    uv run dg launch --assets "$asset" >"$LOG" 2>&1
  else
    # Expected to fail (audit → raise_on_fail=True)
    uv run dg launch --assets "$asset" >"$LOG" 2>&1 || true
  fi
  { grep -E '\[py_orders|\[yaml compute|\[lifecycle_wap\]|WAP audit FAILED|STEP_SUCCESS|STEP_FAILURE|ASSET_CHECK' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: @lifecycle DECORATOR ═══════════════════════════════════"
_run 1 py_orders_happy ok   "clean data → stage → audits pass → PUBLISHED to prod"
_run 2 py_orders_bad   fail "bad data → stage → audits FAIL → QUARANTINED (asset materialization fails)"

echo ""
echo "═══ SHAPE 2: LifecycleWapComponent (YAML) ═══════════════════════════"
_run 3 yaml_orders_happy ok "same as run 1, but via YAML component; audits pass → PUBLISHED"

echo ""
echo ">>> Filesystem state after all runs:"
echo ""
echo "    Production directory ($WAP_ROOT/prod/):"
{ ls -la "$WAP_ROOT/prod/" 2>/dev/null || true; } | sed 's/^/      /'
echo ""
echo "    Quarantine directory ($WAP_ROOT/quarantine/):"
{ ls -la "$WAP_ROOT/quarantine/" 2>/dev/null || true; } | sed 's/^/      /'
echo ""
echo "    Staging directory ($WAP_ROOT/staging/) — auto-derived .staging dirs:"
{ find "$WAP_ROOT" -type d -name ".staging" -exec ls -la {} \; 2>/dev/null || true; } | sed 's/^/      /'

echo ""
echo ">>> Prove published prod files are READABLE:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<PY
import pandas as pd
from pathlib import Path

for path in ("$WAP_ROOT/prod/orders_happy.parquet",
             "$WAP_ROOT/prod/yaml_orders_happy.parquet"):
    p = Path(path)
    if p.exists():
        df = pd.read_parquet(p)
        print(f"    ✓ {p.name:<32} rows={len(df):3d}  cols={list(df.columns)}  min_amount={df['amount'].min():.2f}")
    else:
        print(f"    ✗ {p.name} NOT PRESENT")

# Prod for the bad asset should NOT exist (audit failed, publish skipped)
bad_prod = Path("$WAP_ROOT/prod/orders_bad.parquet")
print()
print(f"    orders_bad.parquet PROD  — should NOT exist (bad-data run failed audits):")
print(f"      exists={bad_prod.exists()}   ← expected False ✓" if not bad_prod.exists()
      else f"      ✗ UNEXPECTED — bad data leaked to prod!")

# Quarantine SHOULD exist with the bad rows
bad_q = Path("$WAP_ROOT/quarantine/orders_bad.parquet")
print(f"    orders_bad.parquet QUAR  — should exist (staging moved to quarantine):")
if bad_q.exists():
    df = pd.read_parquet(bad_q)
    n_neg = int((df["amount"] < 0).sum())
    print(f"      rows={len(df)}  negative_amount_rows={n_neg}   ← preserved for triage ✓")
else:
    print(f"      ✗ MISSING — expected quarantined file")
PY

echo ""
echo ">>> Asset-check events from the event log (all runs):"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
from dagster import DagsterInstance, DagsterEventType

with DagsterInstance.get() as inst:
    print(f"    {'asset':<22}  {'check':<26}  {'passed':<7}  description")
    print(f"    {'-----':<22}  {'-----':<26}  {'------':<7}  -----------")
    rows = []
    for run_rec in inst.get_run_records(limit=20):
        rid = run_rec.dagster_run.run_id
        try:
            recs = inst.get_records_for_run(
                run_id=rid,
                of_type=DagsterEventType.ASSET_CHECK_EVALUATION,
                limit=100,
            ).records
        except Exception:
            continue
        for r in recs:
            ev = r.event_log_entry.dagster_event
            if not ev or not ev.event_specific_data: continue
            eval_ = ev.event_specific_data
            akey = getattr(eval_, "asset_check_key", None)
            if akey is None: continue
            asset_name = "/".join(akey.asset_key.path)
            rows.append((asset_name, akey.name,
                         "PASS" if eval_.passed else "FAIL",
                         (eval_.description or "")[:60]))
    for asset_name, check_name, status, desc in rows:
        print(f"    {asset_name:<22}  {check_name:<26}  {status:<7}  {desc}")
PY

# --- Explainer -----------------------------------------------------------
cat <<DONE

✓ lifecycle_wap demo done.

Two shapes, one Write → Audit → Publish engine:

  ─ SHAPE 1: @lifecycle decorator
      src/$PKG/defs/py_orders_happy.py
      src/$PKG/defs/py_orders_bad.py
      @dg.asset + @lifecycle(write={...}, audit=[...], on_pass="publish",
                             on_fail="quarantine")
      Best when you already have Python code you want to wrap in WAP.

  ─ SHAPE 2: LifecycleWapComponent (YAML)
      src/$PKG/defs/yaml_orders_happy/defs.yaml
      compute.python references a callable that returns a DataFrame.
      Best when you're authoring a new asset from YAML.

What just happened:
  RUN 1 (py_orders_happy)    → PUBLISHED  clean data, 3/3 audits pass
                                → $WAP_ROOT/prod/orders_happy.parquet
  RUN 2 (py_orders_bad)      → QUARANTINED  5 rows with negative amounts;
                                amount_positive_check FAILED → staging moved
                                to quarantine, prod parquet NOT created,
                                asset materialization FAILED (raise_on_fail=True)
                                → $WAP_ROOT/quarantine/orders_bad.parquet
  RUN 3 (yaml_orders_happy)  → PUBLISHED  same shape as RUN 1 via YAML
                                → $WAP_ROOT/prod/yaml_orders_happy.parquet

WAP guarantees:
  - Bad data NEVER touched the prod path.
  - Failed staging is preserved (quarantine) for manual inspection.
  - Downstream stays blocked (asset materialization failed) until triage.
  - Every audit is an AssetCheckResult — visible in Dagster's UI check panel.

Backends demoed:
  - filesystem (local parquet)

Backends mentioned but not run here (require extra installs — see .md):
  - sql  — Postgres / Snowflake / BigQuery / DuckDB (via SQLAlchemy)
  - iceberg  — pyiceberg (branch → fast_forward)
  - delta  — deltalake (staging table → overwrite prod)

Audit kinds demoed:
  - row_count_min, col_null_ratio_max (declarative)
  - python (custom callable → dict with passed/description/metadata)

Audit kinds mentioned but not run here:
  - great_expectations  — delegate to a GE expectation suite
  - dbt_test  — planned (see roadmap in component README)

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → asset check panel per asset

Cleanup: rm -rf $PROJECT_ABS
DONE
