# Write-Audit-Publish — atomic staging → validate → promote
> ✅ **100% offline** — no API keys, no cloud creds, no Iceberg / Delta / Great-Expectations installs.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH shapes of the DCC WAP API across a happy path AND a bad-data path in ~2 minutes.

## What this demo shows

Write → Audit → Publish (WAP) in action: three runs, one contrast — clean data lands in prod, bad data lands in quarantine, and prod is never corrupted.

| Run | Asset | Shape | Outcome |
|---|---|---|---|
| 1 | `py_orders_happy` | `@lifecycle` on `@dg.asset` (Python) | 100 clean rows → stage → 3/3 audits PASS → **PUBLISHED** to prod parquet |
| 2 | `py_orders_bad` | `@lifecycle` on `@dg.asset` (Python) | 100 rows with 5 negative amounts → stage → `amount_positive_check` **FAILS** → **QUARANTINED**; prod parquet never created; asset materialization fails (downstream stays blocked) |
| 3 | `yaml_orders_happy` | `LifecycleWapComponent` (YAML) | Same shape as run 1 via YAML: `compute.python` references a callable; audits pass → **PUBLISHED** |

## The WAP lifecycle

```
                                   ┌─── ALL PASS ──→  atomic promote → PROD PATH
compute → DataFrame → STAGING ──── AUDIT ─┤
   (Python)             (parquet)         │
                                          └─── ANY FAIL ─→  move to QUARANTINE PATH
                                                             (prod untouched;
                                                              asset materialization
                                                              fails → downstream blocked)
```

Three moving parts, three failure modes handled:

1. **Write** — compute produces a `pandas.DataFrame`; wrapper writes it to a staging path (auto-derived `.staging/<name>` sibling of the prod path, or user-specified).
2. **Audit** — each declared check runs against the staging DataFrame and emits an `AssetCheckResult` (visible in Dagster's asset-check panel).
3. **Publish** — on all checks pass: `os.replace(staging, prod)` — an atomic filesystem rename. On any check fail: `os.replace(staging, quarantine)` — the bad data is preserved for manual triage but production is unaffected.

**Why beats overwrite-and-hope:** the overwrite pattern (write directly to prod, run checks after) already corrupts prod by the time checks fail. WAP inverts the order — prod only exists as a promotion of validated staging, so there's no state in which prod holds unvalidated rows.

## The two shapes — same engine, different authoring surface

### Shape 1: `@lifecycle` decorator (Python)

Wrap an EXISTING `@dg.asset` compute. Best when you already have Python code you want to add WAP semantics to.

```python
# src/<pkg>/defs/py_orders_happy.py
import pandas as pd
import dagster as dg
from dagster_community_components import lifecycle

@dg.asset(
    check_specs=[
        dg.AssetCheckSpec(name="row_count_gte_50", asset="py_orders_happy"),
        dg.AssetCheckSpec(name="amount_positive_check", asset="py_orders_happy"),
        dg.AssetCheckSpec(name="user_id_no_nulls", asset="py_orders_happy"),
    ],
)
@lifecycle(
    write={
        "kind": "filesystem",
        "prod_path": ".wap/prod/orders_happy.parquet",
        "format": "parquet",
    },
    audit=[
        {"kind": "row_count_min", "min": 50, "name": "row_count_gte_50"},
        {"kind": "python", "python": "my_pkg.defs.audits:amount_positive_check",
         "name": "amount_positive_check"},
        {"kind": "col_null_ratio_max", "col": "user_id", "max": 0.0,
         "name": "user_id_no_nulls"},
    ],
    on_pass="publish",
    on_fail="quarantine",
    quarantine={"quarantine_path": ".wap/quarantine/orders_happy.parquet"},
    raise_on_fail=True,   # audit fail → dg.Failure → downstream blocked
)
def py_orders_happy(context) -> pd.DataFrame:
    return pd.DataFrame({...})   # your existing compute, unchanged
```

`@lifecycle` is applied BEFORE `@dg.asset`. The `AssetCheckSpec` names on `@dg.asset` should match the audit `name`s so Dagster's UI shows each check on the asset.

### Shape 2: `LifecycleWapComponent` (YAML)

Author a NEW asset from YAML. `compute.python` references a callable that returns a `DataFrame`.

```yaml
# src/<pkg>/defs/yaml_orders_happy/defs.yaml
type: dagster_community_components.LifecycleWapComponent
attributes:
  asset_name: yaml_orders_happy
  group_name: yaml_component

  compute:
    kind: python
    python: "my_pkg.defs.compute:build_clean_orders"

  write:
    kind: filesystem
    prod_path: ".wap/prod/yaml_orders_happy.parquet"
    format: parquet

  audit:
    - kind: row_count_min
      min: 50
      name: row_count_gte_50
    - kind: python
      python: "my_pkg.defs.audits:amount_positive_check"
      name: amount_positive_check
    - kind: col_null_ratio_max
      col: user_id
      max: 0.0
      name: user_id_no_nulls

  on_pass: publish
  on_fail: quarantine
  quarantine:
    quarantine_path: ".wap/quarantine/yaml_orders_happy.parquet"
  raise_on_fail: true
```

Both shapes share the same audit + publish engine, emit the same `AssetCheckResult` events, and populate the same `wap_*` metadata on the materialization.

## Write backends

The demo uses `kind: filesystem` (local parquet) so it stays fully offline. Three more backends are supported — pick the one that matches your existing lakehouse / warehouse:

| Backend | Config shape | Publish mechanic | Install |
|---|---|---|---|
| `kind: filesystem` (demoed) | `{prod_path, staging_path?, format?}` — local FS or fsspec `s3://` / `gs://` / `abfs://` | `os.replace(staging, prod)` on local; `fs.mv` on cloud | (built in) |
| `kind: sql` | `{resource_key\|database_url_env_var, prod_table, staging_table?, schema?}` | `DROP TABLE prod; ALTER TABLE staging RENAME TO prod` in one txn | (whatever SQLAlchemy dialect you already have) |
| `kind: iceberg` | `{catalog, table, staging_branch?}` | `fast_forward('main', staging_branch)` | `pip install 'pyiceberg[pyarrow]'` |
| `kind: delta` | `{delta_uri, staging_uri?, storage_options?}` | Read staging Delta table + `write_deltalake(prod_uri, df, mode='overwrite')` — one Delta commit | `pip install deltalake` |

Delta example (not run in this demo — requires `pip install deltalake`):

```yaml
write:
  kind: delta
  delta_uri: "s3://lakehouse/analytics/orders"
  staging_uri: null                                  # auto: <delta_uri>/_staging/<asset>/<run_id>/
  storage_options:
    AWS_REGION: us-east-1
```

## Audit kinds

| Kind | Shape | What it checks |
|---|---|---|
| `row_count_min` (demoed) | `{kind, min, name?}` | `len(df) >= min` |
| `row_count_max` | `{kind, max, name?}` | `len(df) <= max` |
| `col_null_ratio_max` (demoed) | `{kind, col, max, name?}` | fraction of nulls in `col` ≤ `max` |
| `col_unique` | `{kind, col, name?}` | no duplicates in `col` |
| `col_range_min` / `col_range_max` | `{kind, col, min\|max, name?}` | `df[col].min() >= min` / `df[col].max() <= max` |
| `python` (demoed) | `{kind, python: 'mod:fn', name}` | user callable receives `df`, returns `{passed, description, metadata}` dict OR bool |
| `great_expectations` | `{kind, suite, data_context_root?, name?}` | delegates to a named GE expectation suite; aggregates per-expectation results into one WAP outcome |

Great Expectations example (not run in this demo — requires `pip install great_expectations >= 0.18`):

```yaml
audit:
  - kind: great_expectations
    suite: orders_daily_suite               # required — GE suite name
    data_context_root: /path/to/great_expectations   # optional; else ge.get_context() auto-detects
    name: ge_orders_daily
```

Reuse an existing GE suite as one WAP audit check — the component builds an in-memory pandas datasource + batch from the staging DataFrame, runs `validator.validate()` against the named suite, and rolls the per-expectation results up into a single pass/fail outcome (with counts + failing expectations in the metadata). Freely mix with the built-in check kinds — every audit-list entry becomes one `AssetCheckResult` regardless of kind.

## Publish policies

- **`on_pass: publish`** (default) — atomic promote.
- **`on_pass: discard`** — write to staging only, drop after audit (dry-run mode; useful for scheduled quality reports that shouldn't touch prod).
- **`on_fail: quarantine`** (default) — move staging to quarantine path/table for triage.
- **`on_fail: discard`** — delete staging on failure (fastest cleanup).
- **`on_fail: tag_and_keep`** — leave staging in place with `wap_status=failed` metadata for followup jobs to pick up.

## Why this belongs in Dagster

- **AssetCheckResult is a first-class primitive.** Every WAP audit becomes an `AssetCheckResult` visible in the UI's check panel, targetable by `AutomationCondition.any_downstream_conditions()`, alerts, and sensors. Bad data → check fails → downstream stays blocked — natively, without extra plumbing.
- **The write engine is pluggable.** Same YAML/decorator shape works over local parquet, cloud parquet, SQLAlchemy warehouses, Iceberg branches, and Delta tables. Swap the backend in one field without rewriting the pipeline.
- **Composable with the rest of the DCC decorator family.** Stack `@smart_retry` around `@lifecycle` for classified retries on transient write failures, or pair with `filesystem_monitor` to trigger a review sensor on quarantine.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_lifecycle_wap_demo.sh | bash
cd lifecycle-wap-demo
uv run dg dev
```

## After the demo — inspect in the UI

```bash
cd lifecycle-wap-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

- **Asset check panel** on each asset shows the 3 audits per run — 3 green on `py_orders_happy` and `yaml_orders_happy`; 2 green + 1 RED (`amount_positive_check`) on `py_orders_bad`.
- **Materialization metadata** on the successful assets carries `wap_publish_outcome=published`, `wap_prod_path=…`, `wap_check_summary=3/3 passed`, `wap_row_count=100`.
- **`.wap/prod/`** holds the two published parquets. **`.wap/quarantine/`** holds `orders_bad.parquet` — 100 rows, 5 with negative amounts, preserved for manual inspection.

## Compose with other DCC decorators

```python
@dg.asset
@smart_retry(rules=[{"match_exc": "TransientS3Error", "action": "retry"}], max_attempts=3)
@lifecycle(write={"kind": "filesystem", ...}, audit=[...])
def daily_orders(context) -> pd.DataFrame:
    return build_dataset()
```

- **`@smart_retry`** — classify transient write failures and retry them before WAP even starts an audit.
- **`filesystem_monitor` sensor** — watch the quarantine directory + trigger a downstream review asset when a bad-data file lands.

## See also

- [`lifecycle_wap` component reference](https://dagster-component-ui.vercel.app/c/lifecycle_wap)
- [`smart_retry` walkthrough](smart_retry.md) — classified retries, stacks cleanly on top of `@lifecycle`
- [`filesystem_monitor` walkthrough](filesystem_monitor.md) — trigger a review job when data lands in quarantine
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
