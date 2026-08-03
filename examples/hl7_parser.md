# HL7 v2 Parser — synthetic hospital messages → flat segment DataFrame

**Validated end-to-end** (pure Python). 12 messages rotating through ADT^A01 admit + ORU^R01 lab result + ORM^O01 order → 64 flat segment rows spanning all 9 supported segment types.

```
hl7_messages         ← synthetic_data_generator (hl7_messages, 12 messages)
       │
       └── hl7_segments  ← hl7_v2_parser (all 9 supported segments)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | `schema_type: hl7_messages` alternates between ADT^A01 admit and ORU^R01 lab result. Realistic pipe-delimited segments. |
| `hl7_v2_parser` | Stdlib-only pipe-delimited parser. Emits one row per kept segment with the parent MSH context (`msg_control_id`, `message_type`, `sending_app`, `version_id`) inherited on every row. |

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
| `OBR` | Observation request — service code, observation/report times, status |
| `ORC` | Order control — placer/filler order numbers, ordering provider, status |
| `PV1` | Patient visit — class (I/O/E), location (poc^room^bed^facility), attending, admit/discharge dt, visit_number |
| `EVN` | Event type — event code, recorded/occurred dt, operator |
| `DG1` | Diagnosis — code (ICD-10), name, codeset, datetime, type (admitting/working/final) |
| `AL1` | Patient allergy — allergen type (DA/FA/EA), allergen code, severity, reaction |

Wave 4 backlog: `IN1` (insurance), `GT1` (guarantor), `NK1` (next of kin), `MRG` (merge patient).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_hl7_parser_demo.sh | bash
cd hl7-parser-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
