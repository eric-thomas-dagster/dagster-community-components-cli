# FHIR Normalizer — synthetic FHIR R4 resources → flat per-resource DataFrames

**Validated end-to-end** (pure Python, no external services). 12 generated FHIR resources → 4 Patient rows + 8 Observation rows, each as flat tabular data ready for BQ / parquet / etc.

```
fhir_resources           ← synthetic_data_generator (fhir_patients, 12 resources)
       │
       ├── patients_flat       ← fhir_resource_normalizer (filter: Patient)
       └── observations_flat   ← fhir_resource_normalizer (filter: Observation)
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | `schema_type: fhir_patients` emits `(row_id, resource)` where `resource` is a parsed FHIR JSON dict. Mix of Patient + Observation resources. |
| [`fhir_resource_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/fhir_resource_normalizer) | Flattens FHIR R4/R5 JSON resources to a flat DataFrame. Per-resource extractors for `Patient` / `Observation` / `Encounter` / `Condition` / `MedicationRequest`, plus generic fallback. Inherits the [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) `value_maps` shape for column-value normalization. |

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
| Other | Generic fallback: resource_type, id, status, patient_id |

Wave 2 will add `Claim`, `Coverage`, `Practitioner`, `Organization`, `Bundle`, `Procedure`, `DiagnosticReport`, `Immunization`, etc.

## value_maps (inherited from [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) pattern)

```yaml
value_maps:
  gender:
    M: male       # FHIR sometimes encodes single-letter codes
    F: female
    male: male    # canonical pass-through (case-insensitive by default)
```

## Run it

```bash
./setup_fhir_normalizer_demo.sh
cd fhir-normalizer-demo
uv run dg launch --assets '*'
```

## Sister components

- [`hl7_v2_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hl7_v2_parser) — legacy healthcare standard (pipe-delimited)
- [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) — same pattern for HR data
- [`iso20022_payment_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/iso20022_payment_parser) — same pattern for fintech payments
- [`dataframe_to_bigquery`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_bigquery) — common downstream sink
