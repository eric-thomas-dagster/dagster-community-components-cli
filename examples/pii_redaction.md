# PII Detection + LLM Redaction — GDPR/HIPAA-Grade Compliance Pipeline

**Components:** `synthetic_data_generator`, `pii_detector`, `pii_redactor`, `langchain_chain_asset` — all existing, composed via YAML.

**Script:** [`setup_pii_redaction_demo.sh`](./setup_pii_redaction_demo.sh)
**Cost:** ~$0.005 per run (30 LLM double-check calls on gpt-4o-mini)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end; real Presidio detected PERSON + EMAIL + PHONE_NUMBER; LLM double-check flagged Presidio's German over-tagging.

## Why this exists

Every enterprise needs this: GDPR / HIPAA / CCPA say you can't ship PII into your warehouse, your logs, your LLM prompts, your third-party sinks. Manual review doesn't scale; a rule-based tool alone misses edge cases (nicknames, in-context names, org-specific IDs). The two-pass shape catches ~99%:

- **Pass 1 — rule-based (Presidio):** fast, deterministic, handles PERSON / EMAIL / PHONE / SSN / CREDIT_CARD / IP_ADDRESS via regex + NER.
- **Pass 2 — LLM double-check:** slower, catches what rules miss (or, as this demo shows, flags what rules *over*-tagged). Returns structured `{clean: bool, flags: []}` per row.

```
support_tickets  →  pii_detected  →  redacted_tickets  →  llm_double_check
   (synth)          (Presidio scan)   (Presidio replace)   (LLM edge-case)
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` (for pass 2)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pii_redaction_demo.sh -o setup_pii_redaction_demo.sh
chmod +x setup_pii_redaction_demo.sh
./setup_pii_redaction_demo.sh
```

## Validated run samples (2026-07-07)

Presidio correctly redacts PERSON + EMAIL in English:

```
ORIGINAL:  Hi, my name is Olga Petrova and my email is olga.petrova@test.io.
           My order #17406 hasn't arrived. Can you check status?
REDACTED:  Hi, my name is <PERSON> and my email is <EMAIL_ADDRESS>.
           My order #17406 hasn't arrived. Can you check status?
```

Presidio's German NER over-tags — the LLM catches this on pass 2:

```
ORIGINAL:  Guten Tag! Mein Name ist Klaus Müller. Ich brauche eine Rechnung für letzten Monat.
REDACTED:  Guten Tag! Mein Name ist <PERSON>. <PERSON> eine Rechnung für <PERSON>.
                                                ^^^^ over-tagged ^^^^^^^^^^^^^
LLM flag:  {"clean": false, "flags": ["appears to have over-tagged 'Ich brauche' and 'letzten Monat' as PERSON"]}
```

The LLM double-check turns Presidio's noisy multi-language output into actionable signals for a compliance queue.

## Fields worth calling out

`PiiDetectorComponent` supports these Presidio entity types (add more via `entity_types:`):

- `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`
- `US_SSN`, `US_DRIVER_LICENSE`, `US_PASSPORT`, `US_BANK_NUMBER`
- `CREDIT_CARD`, `IBAN_CODE`, `IP_ADDRESS`, `URL`
- `LOCATION`, `DATE_TIME`, `NRP` (nationality/religion/political)
- `MEDICAL_LICENSE` (HIPAA)

`PiiRedactorComponent` `replacement_style` options: `placeholder` (default, `<PERSON>`), `mask` (`****`), `hash` (SHA256 of the original).

## Production variants

- **Swap synth for real ingestion.** `zendesk_ingestion` / `intercom_resource` / `salesforce_ingestion` / any DataFrame source.
- **Warehouse-only redacted columns.** Add a downstream sink (`dataframe_to_snowflake`, `dataframe_to_bigquery`) that writes ONLY `ticket_text_redacted` — never the raw column.
- **GDPR audit gate.** Chain a `dagster_asset_check` that fails the run if any `llm_double_check.clean == False` — hard-gate compliance in CI.
- **Custom recognizers.** Add company-specific patterns (internal customer IDs, product codes) to `PiiDetectorComponent`'s config.

## Related

- [Data quality agent](./data_quality_agent.md) — same pattern (Python row-wise LLM), different problem shape (data quality vs. compliance).
- [Cube semantic layer + LLM](./cube_query.md) — LLM-in-the-loop pattern, structured data source.
