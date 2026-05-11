# HL7 v2 Parser — synthetic hospital messages → flat segment DataFrame

**Validated end-to-end** (pure Python). 8 alternating ADT^A01 admit + ORU^R01 lab-result HL7 messages → 24 flat segment rows.

```
hl7_messages         ← synthetic_data_generator (hl7_messages, 8 messages)
       │
       └── hl7_segments  ← hl7_v2_parser (MSH + PID + OBX rows per message)
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | `schema_type: hl7_messages` alternates between ADT^A01 admit and ORU^R01 lab result. Realistic pipe-delimited segments. |
| [`hl7_v2_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hl7_v2_parser) | Stdlib-only pipe-delimited parser. Emits one row per kept segment with the parent MSH context (`msg_control_id`, `message_type`, `sending_app`, `version_id`) inherited on every row. |

## Live output

24 segment rows total:

| segment | message_type | msg_control_id |
|---|---|---|
| MSH | ADT^A01 | MSG0000001 |
| PID | ADT^A01 | MSG0000001 |
| MSH | ORU^R01 | MSG0000002 |
| PID | ORU^R01 | MSG0000002 |
| OBX | ORU^R01 | MSG0000002 |
| OBX | ORU^R01 | MSG0000002 |
| ... | ... | ... |

OBX rows (lab values) carry the full result detail:

| code | code_name | value | units | abnormal_flags |
|---|---|---|---|---|
| GLU | Glucose | 180.0 | mg/dL | **H** |
| HR | Heart Rate | 103 | /min | N |
| GLU | Glucose | 132.0 | mg/dL | **H** |
| HR | Heart Rate | 102 | /min | N |

The `H` flag on glucose values >100 mg/dL is HL7-standard ("high"); the parser surfaces it as a column directly.

## Why HL7 v2 still matters

HL7 v2.x has been the dominant messaging format inside hospitals since the 1990s. Despite FHIR's growing adoption for APIs, the *internal* feeds — LIS results, ADT (admit/discharge/transfer) events, RIS imaging orders — are still overwhelmingly HL7 v2. Every hospital data warehouse needs to parse these.

## Supported segments (with full extractors)

| Segment | What it carries |
|---|---|
| `MSH` | Message header — sending app, datetime, message type, control id, version |
| `PID` | Patient demographics — id, name, DOB, sex, address |
| `OBX` | Observation result — code, value, units, ref range, abnormal flags |

Wave 2 will add `OBR` (observation request), `ORC` (order), `PV1` (visit), `EVN` (event), `DG1` (diagnosis), `AL1` (allergy), `IN1` (insurance), `GT1` (guarantor).

## Run it

```bash
./setup_hl7_parser_demo.sh
cd hl7-parser-demo
uv run dg launch --assets '*'
```

## Sister components

- [`fhir_resource_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/fhir_resource_normalizer) — modern healthcare standard (JSON, R4/R5)
- [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) — same vendor-data-to-canonical pattern for HR
- [`iso20022_payment_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/iso20022_payment_parser) — same pattern for fintech payments
