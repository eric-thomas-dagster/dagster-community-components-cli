#!/usr/bin/env bash
# Partitions demo — the four canonical partition shapes end-to-end.
#
# WHAT THIS DEMONSTRATES
#   The canonical partition shape (Phase 1 of the partition rework)
#   exercised across four use cases on the same component family:
#
#     1. UNPARTITIONED          — baseline, no partition fields
#     2. STATIC partitions      — fixed list of values (regions)
#     3. DAILY partitions       — time-window backfills
#     4. DYNAMIC partitions     — multi-tenant SaaS, tenants registered at runtime
#
#   Plus: a PerPartitionBackfillJob that drives the DYNAMIC shape with
#   per-tenant concurrency keys (same-tenant runs serialize, cross-
#   tenant parallelize).
#
# Pipeline:
#   tenant_orders (synthetic source)
#         │
#         ├── orders_unpartitioned      → dataframe_to_csv (unpartitioned)
#         ├── orders_per_region         → dataframe_to_csv (static partitions: us, eu, apac)
#         ├── orders_per_day            → dataframe_to_csv (daily partitions, last 7 days)
#         └── orders_per_tenant         → dataframe_to_csv (dynamic partitions: 'tenants')
#
#   tenant_backfill_job                 → PerPartitionBackfillJob
#                                          materializes orders_per_tenant
#                                          for every registered tenant in
#                                          parallel, with concurrency key
#                                          tenant-{partition_key}
#
# COST: \$0 — fully local.

set -euo pipefail
PROJECT_DIR="${1:-partitions-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add dataframe_to_csv               --auto-install
$CLI add per_partition_backfill_job     --auto-install

echo ">>> Writing inline source data + tenant registration"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import os
import pandas as pd
import dagster as dg

# A small set of synthetic tenants. In production this list would come
# from your tenant-management system; here we hardcode three so the demo
# is deterministic.
SYNTHETIC_TENANTS = ["acme", "globex", "initech"]


@dg.asset(group_name="ingest", description="Synthetic order rows: 30 orders × 3 tenants × 3 regions × 7 days")
def tenant_orders() -> pd.DataFrame:
    rows = []
    for tenant in SYNTHETIC_TENANTS:
        for region in ("us", "eu", "apac"):
            for day in pd.date_range("2025-04-25", "2025-05-01", freq="D"):
                for i in range(5):  # 5 orders per (tenant, region, day)
                    rows.append({
                        "order_id": f"{tenant}-{region}-{day:%Y%m%d}-{i:03d}",
                        "tenant_id": tenant,
                        "region": region,
                        "order_date": day.strftime("%Y-%m-%d"),
                        "amount": 100 + i * 17,
                    })
    return pd.DataFrame(rows)


@dg.sensor(name="register_tenants_sensor", default_status=dg.DefaultSensorStatus.STOPPED)
def register_tenants_sensor(context: dg.SensorEvaluationContext):
    """One-shot sensor that registers the synthetic tenant set with the
    DynamicPartitionsDefinition named 'tenants'. In production this
    would poll your tenant-management API for new tenants and add them.

    For the demo, run this manually once before launching
    orders_per_tenant or tenant_backfill_job:
        dg sensor cursor register_tenants_sensor --tick
    """
    existing = set(context.instance.get_dynamic_partitions("tenants"))
    new = [t for t in SYNTHETIC_TENANTS if t not in existing]
    if new:
        context.instance.add_dynamic_partitions("tenants", new)
        context.log.info(f"Registered {len(new)} new tenants: {new}")
    return dg.SkipReason("(no run requested — sensor only registers partition keys)")


defs = dg.Definitions(assets=[tenant_orders], sensors=[register_tenants_sensor])
PYEOF

echo ">>> Writing four-shape sink defs.yaml"

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_unpartitioned
  upstream_asset_key: tenant_orders
  file_path: out/partitions_demo/unpartitioned/orders.csv
  group_name: sinks
EOF

# Need separate dirs since dg-component install would clobber on second add.
mkdir -p "src/$PKG/defs/orders_per_region"
cat > "src/$PKG/defs/orders_per_region/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_per_region
  upstream_asset_key: tenant_orders
  file_path: out/partitions_demo/per_region/orders.csv
  partition_type: static
  partition_values: "us,eu,apac"
  partition_static_column: region
  group_name: sinks
EOF

mkdir -p "src/$PKG/defs/orders_per_day"
cat > "src/$PKG/defs/orders_per_day/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_per_day
  upstream_asset_key: tenant_orders
  file_path: out/partitions_demo/per_day/orders.csv
  partition_type: daily
  partition_start: "2025-04-25"
  partition_date_column: order_date
  group_name: sinks
EOF

mkdir -p "src/$PKG/defs/orders_per_tenant"
cat > "src/$PKG/defs/orders_per_tenant/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_per_tenant
  upstream_asset_key: tenant_orders
  file_path: out/partitions_demo/per_tenant/orders.csv
  partition_type: dynamic
  dynamic_partition_name: tenants
  partition_static_column: tenant_id
  group_name: sinks
EOF

cat > "src/$PKG/defs/per_partition_backfill_job/defs.yaml" <<EOF
type: $PKG.components.per_partition_backfill_job.component.PerPartitionBackfillJobComponent
attributes:
  job_name: tenant_backfill_job
  target_asset_key: orders_per_tenant
  partition_strategy: dynamic_all
  dynamic_partitions_def_name: tenants
  concurrency_key_template: "tenant-{partition_key}"
  job_tags:
    purpose: per-tenant-backfill
EOF

cat <<MSG

>>> Setup complete.

Three steps to run end-to-end:

  cd $PROJECT_DIR

  # 1. Materialize the source asset.
  uv run dg launch --assets tenant_orders

  # 2. Register the synthetic tenants (one-shot sensor).
  #    This populates the DynamicPartitionsDefinition named 'tenants'.
  uv run dg sensor cursor register_tenants_sensor --tick

  # 3. Materialize the four sinks. The dynamic-partition one will only
  #    show up with partition keys after step 2 has run.
  uv run dg launch --assets +orders_unpartitioned                                    # one CSV
  uv run dg launch --assets +orders_per_region --partition us                       # one CSV per region
  uv run dg launch --assets +orders_per_day --partition 2025-04-30                  # one CSV per day
  uv run dg launch --assets +orders_per_tenant --partition acme                     # one CSV per tenant

  # 4. Run the backfill job — materializes orders_per_tenant for every
  #    registered tenant in parallel, with per-tenant concurrency keys.
  uv run dg launch --jobs tenant_backfill_job

Inspect the outputs:
  ls $PROJECT_ABS/out/partitions_demo/per_*

Or open the asset graph:
  uv run dg dev   # http://localhost:3000
MSG
