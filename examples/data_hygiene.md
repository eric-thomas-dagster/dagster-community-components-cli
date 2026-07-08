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
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_data_hygiene_demo.sh | bash
cd data-hygiene-demo
uv run dg launch --assets '*'
```

Pure Python — no external dependencies. The whole chain runs in under 20 seconds.

## Replacing any link in the chain

The graph is a strict linear DAG, so you can swap any transform for an alternative without changing the others:

- Replace `field_mapper` with a dict literal in a downstream transform
- Replace `data_masking` with `pii_redactor` for regex-based detection
- Replace `count_records` with `aggregate` for sum / mean / median per group

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build a data-quality / hygiene pipeline:
      1. Generate 300 synthetic customer rows (schema_type: customers).
      2. Add audit columns (created_at, updated_at) to every row.
      3. Mask/redact the email column so raw addresses are not stored.
      4. Add a hash column derived from customer_id + email to serve as a stable fingerprint.
      5. Add a surrogate key column (deterministic integer id).
      6. Add a total record count as metadata (or a count row).
      7. Write the hygienic result to /tmp/customers_clean.csv.
  include_ids: ['synthetic_data_generator']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **7/7 clean picks in 39s, ~$0.0055 total cost.** Outputs written: `/tmp/customers_clean.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.
