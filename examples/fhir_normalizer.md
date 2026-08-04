# FHIR Normalizer — synthetic FHIR R4 resources → flat per-resource DataFrames
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end** (pure Python, no external services). 28 generated FHIR resources covering 6 types → 4 specialized downstream tables, each as flat tabular data ready for BQ / parquet / etc.

```
fhir_resources           ← synthetic_data_generator (fhir_patients, 28 resources)
       │
       ├── patients_flat        ← fhir_resource_normalizer (Patient only)
       ├── observations_flat    ← fhir_resource_normalizer (Observation only)
       ├── claims_flat          ← fhir_resource_normalizer (Claim + Coverage)
       └── provider_directory   ← fhir_resource_normalizer (Practitioner + Organization)
```

One component, four different `resource_types` filters → four purpose-built tables.

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | `schema_type: fhir_patients` emits `(row_id, resource)` where `resource` is a parsed FHIR JSON dict. Mix of Patient + Observation resources. |
| `fhir_resource_normalizer` | Flattens FHIR R4/R5 JSON resources to a flat DataFrame. Per-resource extractors for `Patient` / `Observation` / `Encounter` / `Condition` / `MedicationRequest`, plus generic fallback. Inherits the `hris_normalizer` `value_maps` shape for column-value normalization. |

## Live output

**`patients_flat`** (4 rows):
| id | first_name | last_name | gender | birth_date | city |
|---|---|---|---|---|---|
| pat-00001 | Eli | Hernandez | male | 1982-08-23 | Boston |
| pat-00002 | Jamal | Brown | male | 1956-07-13 | Austin |
| pat-00003 | Lin | Smith | male | 1980-03-25 | Boston |
| pat-00004 | ... | ... | ... | ... | ... |

Note: `gender` came through as `M`/`F` from the source FHIR; `value_maps` normalized to `male`/`female`.

**`observations_flat`** (8 rows):
| id | patient_id | code | display | value | unit |
|---|---|---|---|---|---|
| obs-00001-hr | pat-00001 | 8867-4 | Heart rate | 76.0 | /min |
| obs-00001-temp | pat-00001 | 8310-5 | Body temperature | 36.7 | Cel |
| obs-00002-hr | pat-00002 | 8867-4 | Heart rate | 85.0 | /min |
| ... | ... | ... | ... | ... | ... |

`patient_id` is auto-extracted from the resource's `subject.reference` (`"Patient/pat-00001"` → `"pat-00001"`) so you can join Observations back to Patients directly.

## Resource types currently supported

| Resource | Extracted columns |
|---|---|
| `Patient` | id, first_name, last_name, gender, birth_date, deceased, city, state, country, postal_code |
| `Observation` | id, patient_id, status, code_system, code, display, effective_dt, value, unit |
| `Encounter` | id, patient_id, status, class_code, class_display, start, end, reason_text |
| `Condition` | id, patient_id, code_system, code, display, clinical_status, onset_dt, recorded_dt |
| `MedicationRequest` | id, patient_id, status, intent, med_system, med_code, med_display, authored_on, dosage_text |
| `Claim` | id, status, use, patient_id, provider_id, insurer_id, total_amount, total_currency, billable_start, billable_end, created, priority_code |
| `Coverage` | id, status, type_code, type_display, policy_holder_id, subscriber_id, beneficiary_id, payor_id, subscriber_member_id, period_start, period_end, network |
| `Practitioner` | id, active, first_name, last_name, prefix, suffix, gender, birth_date, **npi**, city, state, postal_code, country |
| `Organization` | id, active, name, alias, type_code, type_display, city, state, postal_code, country, part_of_id |
| `Bundle` | id, bundle_type, timestamp, total, entry_count, entries_by_type (dict) |
| Other | Generic fallback: resource_type, id, status, patient_id |

## value_maps (inherited from `hris_normalizer` pattern)

```yaml
value_maps:
  gender:
    M: male       # FHIR sometimes encodes single-letter codes
    F: female
    male: male    # canonical pass-through (case-insensitive by default)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_fhir_normalizer_demo.sh | bash
cd fhir-normalizer-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
