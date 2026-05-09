# HRIS Normalizer — vendor-agnostic employee-data pipeline

**Validated end-to-end against a synthetic 20-row vendor export.** Maps
vendor-specific column names + status codes (e.g. `T` / `L` / `REG-FT`)
to a canonical employee schema, then runs HR analytics on top.

```
employees_raw           (synthetic 20-row vendor HRIS export with
                         vendor-y columns: emp_id, given_name,
                         status='A'/'T'/'L', emp_type='REG-FT'/'REG-PT')
       │
       └── employees_normalized   ← hris_normalizer (canonical schema)
                │
                ├── headcount_by_dept         ← pandas (active vs total per dept)
                └── employees_normalized_csv  ← /tmp/employees_normalized.csv
```

## Components covered (1)

| Component | What it does |
|---|---|
| `hris_normalizer` | Vendor-agnostic schema mapping. Takes any DataFrame of employee data (Workday, BambooHR, ADP, Gusto, Rippling, Hibob, internal export) and maps it to a canonical schema (`employee_id`, `email`, `first_name`, `last_name`, `manager_employee_id`, `department`, `job_title`, `location`, `country`, `employment_status`, `employment_type`, `hire_date`, `termination_date`, plus derived `tenure_days`, `is_active`, `full_name`). |

## Validation status — live

RUN_SUCCESS materializing the chain end-to-end:

- 20 vendor rows → 20 normalized rows
- `T` → `terminated`, `L` → `on_leave`, `REG-PT` → `part_time`, `REG-FT` → `full_time`
- `is_active` and `tenure_days` derived from normalized `employment_status` + `hire_date`
- Source vendor columns preserved with `vendor_` prefix unless `drop_extra_columns: true`

`headcount_by_dept` output (real run):

```
      department  total_employees  active_employees  avg_tenure_days  terminated_count  active_pct
     Engineering                5                 4           1196.4                 1        80.0
           Sales                5                 3           1048.8                 0        60.0
Customer Success                3                 2           1415.0                 1        66.7
       Marketing                3                 1            852.3                 1        33.3
         Finance                2                 1           1130.0                 1        50.0
          People                2                 2           2322.0                 0       100.0
```

## Cost

**$0.** Pure local pandas + synthetic data.

## Run it

```bash
./setup_hris_normalizer_demo.sh
cd hris-normalizer-demo
uv run dg launch --assets '*'

cat /tmp/employees_normalized.csv
```

## How the column_map works

Provide one entry per canonical field your vendor exposes; missing fields
are filled with `None`:

```yaml
attributes:
  column_map:
    employee_id:         emp_id
    email:               work_email
    first_name:          given_name
    last_name:           family_name
    manager_employee_id: supervisor_id
    department:          dept
    job_title:           position
    location:            office
    country:             country_iso
    employment_status:   status
    employment_type:     emp_type
    hire_date:           start_date
    termination_date:    end_date
```

## How value normalization works

Vendor-specific status / type values are auto-normalized. The `status_map`
and `type_map` ergonomic shorthands map into a single generic
`value_maps` dict that works for **any** canonical field:

```yaml
status_map: { A: active, T: terminated, L: on_leave }
type_map:   { REG-FT: full_time, REG-PT: part_time, CONTRACT: contractor }

# Generic value_maps — works on any canonical field
value_maps:
  department:
    ENG: Engineering
    OPS: Operations
    GTM: Sales
```

`case_insensitive_map: true` (default) lowercases both the input value
AND the user-supplied map keys before lookup, so `T` and `t` and `Active`
and `active` all match the same way.

## Bug surfaced and fixed validating this demo

1. **`case_insensitive_map` only lowercased the input value, not the map keys** — so user maps like `{A: active, T: terminated}` never matched the lowercased `'a'` / `'t'` lookup keys. Fixed: lowercase both sides at lookup time. Caught immediately on the first run when the CSV showed raw `T` / `L` instead of the canonical values.
2. **Hardcoded value-normalization for status + type only** — refactored to a generic `value_maps` dict that works on any canonical field. `status_map` / `type_map` are kept as ergonomic sugar.

## Sister components (planned)

- `merge_dev_hris_ingestion` — Merge.dev's unified HRIS API: one client → 50+ HRIS systems.
- Vendor-specific ingestion peers (Workday, BambooHR, ADP, etc.).
- `hr_metrics` (analytics) — pre-built turnover rate, span of control, tenure distribution, headcount over time.

## Why a generic component (not `workday_normalizer` / `bamboohr_normalizer`)?

Same pattern as `litellm_inference_asset` vs `openai_llm`: we have a vendor-agnostic generic + per-vendor natives in parallel. Most teams running HR analytics either (a) standardize on one vendor and want a clean canonical schema, or (b) have already normalized at ingest and just need the HR analytics layer. `hris_normalizer` covers (a); upstream `merge_dev_hris_ingestion` + this normalizer covers (b).
