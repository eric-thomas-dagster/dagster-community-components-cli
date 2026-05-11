# Data Hygiene Pipeline — 9 utility transforms in one chain

**Validated end-to-end** (pure Python). 50 synthetic CRM customer rows pass through 9 utility components — auditing, validating, normalizing, masking, hashing, keying, numbering, counting — to demonstrate the *data-hygiene toolbox* that ships in the registry.

```
raw_customers
  → audited      ← audit_columns        (add run_id, materialization_time, source_system)
  → schema_check ← schema_validator     (assert required fields)
  → mapped       ← field_mapper         (rename messy source columns)
  → canon        ← map_values           (canonicalize state codes → state names)
  → masked       ← data_masking         (mask emails, pseudonymize IDs)
  → hashed       ← hash                 (row fingerprint for change-detection)
  → keyed        ← surrogate_key        (stable SK from business key)
  → numbered     ← record_id            (monotonic ID with prefix)
  → counts       ← count_records        (group-by tally)
```

## Components covered (10)

| Component | What it adds |
|---|---|
| `synthetic_data_generator` | 50 synthetic customer rows (`customers` schema) |
| `audit_columns` | `dagster_run_id`, `dagster_asset_key`, `materialization_time` + arbitrary `static_columns` |
| `schema_validator` | JSON-schema validation — flags or fails rows that don't conform |
| `field_mapper` | Column renaming (`customer_id` → `cust_id`, `email` → `email_address`, ...) |
| `map_values` | Value lookup (`CA` → `California`, ...). Falls through to `default_value` if no match |
| `data_masking` | PII masking — `method: hash` / `redact` / `partial` / `pseudonymize` per-column |
| `hash` | Row fingerprint across N columns (SHA-256). Use for CDC / change-detection |
| `surrogate_key` | Stable surrogate key from business-key columns. Truncatable. |
| `record_id` | Monotonic ID with prefix (`CUST-1000`, `CUST-1001`, ...). Sortable input order. |
| `count_records` | Aggregation — `group_by` + count column |

## Live output

Starting shape: **50 rows × 10 columns** (customer_id, first_name, last_name, email, phone, city, state, signup_date, lifetime_value, is_active).

After the 9-step chain: **50 rows × 20 columns**. Each transform adds 1-3 columns:

```
Final row sample:
  cust_id              p_c9191682c528           (pseudonymized)
  first_name           Jane
  last_name            Smith
  email_address        3d5899c0ac6cee75         (hashed)
  phone                +1-481-350-4657
  city                 Chicago
  state_code           IL
  state_name           Illinois                 ← map_values
  lifetime_value       6799.32
  dagster_run_id       53a3d676-7c3b-4f48-...   ← audit_columns
  dagster_asset_key    audited
  materialization_time 2026-05-11T22:50:32Z
  source_system        crm_export
  pipeline_owner       data-platform
  _validation_errors   None                     ← schema_validator (clean)
  row_hash             42645b454e05e6e5...      ← hash
  customer_sk          8f3fa3af041e383a         ← surrogate_key (truncated SHA)
  customer_record_id   CUST-1000                ← record_id
```

Final `counts` aggregation:

| state_name | customer_count |
|---|---|
| Arizona | 6 |
| California | 12 |
| Illinois | 5 |
| New York | 3 |
| Pennsylvania | 5 |
| Texas | 19 |

## Why this pipeline

The transforms in this chain rarely make sense in isolation — they're *toolbox* components that compose into real-world data-prep pipelines:

- **Audit + validate + lineage** — every row carries its origin run, source system, validation result. You can prove provenance in compliance reports.
- **Rename + canonicalize** — bridge raw vendor exports to your warehouse's canonical schema without bespoke Python.
- **Mask PII** — `data_masking` with `method: hash` and `pseudonymize` keeps emails / IDs usable for joins while removing personally-identifying values.
- **Hash + surrogate key** — `row_hash` lets you detect changes between runs; `customer_sk` is the join-friendly key that survives source-system renames.
- **Number + count** — `record_id` for ordered downstream consumers; `count_records` for ops dashboards.

## Run it

```bash
./setup_data_hygiene_demo.sh
cd data-hygiene-demo
uv run dg launch --assets '*'
```

Pure Python — no external dependencies. The whole chain runs in under 20 seconds.

## Replacing any link in the chain

The graph is a strict linear DAG, so you can swap any transform for an alternative without changing the others:

- Replace `field_mapper` with a dict literal in a downstream transform
- Replace `data_masking` with `pii_redactor` for regex-based detection
- Replace `count_records` with `aggregate` for sum / mean / median per group
