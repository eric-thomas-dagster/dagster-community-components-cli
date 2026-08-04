# ACORD XML Parser — synthetic insurance messages → flat per-entity DataFrame
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** (pure Python). 12 synthetic ACORD XML messages rotated across four envelope types → 12 flat rows split 6 Policy + 3 Claim + 3 Quote, ready for warehouse / claims / actuarial pipelines.

```
acord_messages    ← synthetic_data_generator (acord_messages, 12 msgs)
       │
       └── acord_flat   ← acord_xml_parser (one row per Policy/Claim/Quote)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | `schema_type: acord_messages` rotates four ACORD envelopes — InsurancePolicyAddRq, InsurancePolicyChangeRq, ClaimsNotificationRq, InsurancePolicyQuoteInqRq — with realistic policies, claims, and quotes inside. |
| `acord_xml_parser` | Auto-strips XML namespaces, detects the envelope type (skipping `<SignonRq>` boilerplate), emits one row per inner Policy / Claim / Quote with envelope context (message_type, transaction_id, sender_id) propagated. |

## Live output

**12 rows × varies cols, by message_type**:

```
InsurancePolicyAddRq         3
InsurancePolicyChangeRq      3
ClaimsNotificationRq         3
InsurancePolicyQuoteInqRq    3
```

**Policy sample (InsurancePolicyAddRq):**

| message_type | transaction_id | policy_number | line_of_business | effective_date | premium_amount | insured_name |
|---|---|---|---|---|---|---|
| InsurancePolicyAddRq | TX00000001 | POL4949905 | GLI | 2025-01-15 | 43,000.60 | Eli Patel |

**Claim sample (ClaimsNotificationRq):**

| claim_number | policy_number | loss_date | report_date | loss_cause | claim_status | loss_amount |
|---|---|---|---|---|---|---|
| CLM561903 | POL8074577 | 2025-01-10 | 2025-01-12 | DDD (Water damage) | Open | 34,053.75 |

**Quote sample (InsurancePolicyQuoteInqRq):**

| quote_number | quote_date | quote_expiration | rating_engine |
|---|---|---|---|
| QUO43371 | 2025-01-15 | 2025-02-14 | SureRate v3 |

## Supported ACORD envelopes

| Envelope | Domain |
|---|---|
| `InsurancePolicyAddRq` / `Rs` | New-business submission (P&C / Surety / Life) |
| `InsurancePolicyChangeRq` / `Rs` | Mid-term endorsements |
| `InsurancePolicyCancelRq` / `Rs` | Cancellations |
| `InsurancePolicyQuoteInqRq` / `Rs` | Quote requests + responses |
| `ClaimsNotificationRq` / `Rs` | First notice of loss (FNOL) |
| `ClaimsResponseRq` / `Rs` | Claim status / disposition |
| `CertificateOfInsuranceRq` / `Rs` | COI generation |
| `AppraisalRq` / `Rs` | Auto appraisal |
| `MotorVehicleReportRq` / `Rs` | MVR pull / return |

Other envelopes fall through with envelope-only rows (no entity extraction); easy to extend by adding a new function to `_TYPE_EXTRACTORS`.

## Why ACORD matters

ACORD XML is the lingua franca between US/UK/AU/CA carriers, MGAs, brokers, and rating engines. Every policy admin system, claims platform, and reinsurance reporting pipeline ingests ACORD daily. The standard is intentionally verbose (carriers extend it with vendor namespaces) — this component handles the high-traffic ~80% of fields without forcing a full ACORD-spec compiler.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_acord_demo.sh | bash
cd acord-demo
uv run dg launch --assets '*'
```

Pure Python — no external dependencies, no network calls.

## See also

<!-- TODO: link related walkthroughs -->
