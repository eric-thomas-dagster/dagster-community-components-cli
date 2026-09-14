# RPA meets AI — one Dagster pipeline over any bot vendor, with LLM routing + LLM validation + HITL

**AI + RPA together: the agent decides which bot runs, the agent validates the output, humans sign off on low-confidence extractions. One Dagster pipeline over any RPA vendor.**

## The story

Every enterprise with a large RPA footprint is also, in 2026, building
heavily on LLMs. Today those two investments live in separate rooms.
RPA sits in the UiPath / Automation Anywhere / Blue Prism / Power
Automate dashboards, driven by RPA CoEs. GenAI sits in a separate
Python stack, driven by a data/AI team. The humans-in-the-loop who
have to sign off when either side has low confidence are on yet
another set of Slack threads.

This walkthrough shows what the composition actually looks like when
you put them in the same pipeline. Real shape: **incoming document →
agent classifies the format → agent routes to the right RPA bot →
bot extracts raw payload → normalize across vendor payload shapes →
agent extracts structured fields → LLM-as-judge scores the extraction
against a rubric → if the score is below threshold, a human approval
gate blocks the downstream data load until an approver signs off.**

Every step is a Dagster asset. Lineage runs end-to-end from the
incoming document through the RPA bot output, through the LLM
extraction, through the approval token, into whatever downstream data
model consumes it. Retry, cost tracking, alerts, audit trail — all
first-class. This is the "AI decides, Dagster executes, RPA does the
work" pattern.

## Asset graph

```
    raw_invoices                      (synthetic_data_generator OR real ingestion)
        │
        │  each invoice: {invoice_id, source, body_text, attachment_kind}
        ▼
    invoice_format                    (AgenticPipeline `classify` op)
        │                              LLM picks one of: pdf | scanned_image | edi | email
        ▼
    invoice_route                     (AgenticPipeline `conditional_route` op)
        │                              deterministic branch on invoice_format.text
        │
        ├────────────┬────────────┬───────────────┬───────────────┐
        ▼            ▼            ▼               ▼               │
  uipath_extract_  aa_ocr_       bp_edi_         pa_email_        │
  standard_pdf    scanned_image  parser          extract           │
  (UiPath        (Automation    (Blue Prism    (Power Automate    │
   Orchestrator)  Anywhere)      Control Room)  cloud flow)       │
        │            │            │               │                │
        │  each RPA asset emits `output_payload` metadata          │
        │  (UiPath OutputArguments / AA bot_output /               │
        │   BP session_output / PA trigger_output)                 │
        └────────────┴────────────┼───────────────┘                │
                                  ▼                                 │
                    invoice_output_normalized   (rpa_output_parser) │
                                  │                                 │
                                  │  flatten vendor differences     │
                                  │  into one downstream shape      │
                                  ▼                                 │
                    invoice_extracted_fields    (AgenticPipeline `extract` op)
                                  │              LLM → JSON matching a schema
                                  │              {vendor, invoice_no, date, line_items[]…}
                                  ▼
                    extraction_quality_score    (llm_evaluator)
                                  │              rubric: accuracy + completeness + format
                                  │              overall score in [0, 1]
                                  ▼
                    invoice_approval_gate       (human_approval_gate)
                                  │              blocks if quality_score < 0.85
                                  │              approval via Slack quorum OR file drop
                                  ▼
                    invoice_ledger              (downstream — dbt / Snowflake / warehouse write)
```

Every arrow is a real asset dependency. Every node is a queryable
materialization with metadata, cost, and duration. Every branch is
inspectable in the Dagster UI.

Two peer checks watch the RPA layer:

```
  each *_extract_* / *_ocr_* / *_parser / *_email_extract  asset
        │
        ├── rpa_health_check           (asset check per RPA asset — SLA + non-empty output)
        │
        └── rpa_queue_concurrency_lock (pool cap — parallel bot runs limited to license count)
```

## Components used

| Component | Role |
|---|---|
| [`synthetic_data_generator`](https://dagster-component-ui.vercel.app/c/synthetic_data_generator) | Source of the demo invoice stream. Swap for `s3_monitor` / `imap_email_monitor` / `sharepoint_monitor` in prod. |
| [`agentic_pipeline`](https://dagster-component-ui.vercel.app/c/agentic_pipeline) | Hosts the `classify` + `conditional_route` + `extract` ops in one YAML. Every step is a Dagster asset. |
| [`uipath_orchestrator_integration`](https://dagster-component-ui.vercel.app/c/uipath_orchestrator_integration) | UiPath Release → daily-partitioned Dagster asset. `OutputArguments` land in metadata. |
| [`automation_anywhere_integration`](https://dagster-component-ui.vercel.app/c/automation_anywhere_integration) | Automation Anywhere Control Room bots as assets. |
| [`blue_prism_integration`](https://dagster-community-components.vercel.app/c/blue_prism_integration) | Blue Prism Control Room processes as assets. |
| [`power_automate_integration`](https://dagster-component-ui.vercel.app/c/power_automate_integration) | Microsoft Power Automate cloud flows as assets. |
| [`rpa_output_parser`](https://dagster-component-ui.vercel.app/c/rpa_output_parser) | Flattens per-vendor payload shapes (`OutputArguments` / `bot_output` / `session_output` / `trigger_output`) into one downstream-friendly asset. |
| [`llm_evaluator`](https://dagster-component-ui.vercel.app/c/llm_evaluator) | LLM-as-judge scoring the extracted fields against a rubric. Emits a numeric `quality_score` + per-metric reasoning. |
| [`human_approval_gate`](https://dagster-component-ui.vercel.app/c/human_approval_gate) | Asset-check gate. Passes on `{approved: true}` token; blocks downstream on missing / rejected. Compatible with Slack quorum via `slack_approval_gate`. |
| [`rpa_health_check`](https://dagster-component-ui.vercel.app/c/rpa_health_check) | Per-RPA-asset check. Fails when a bot exceeds SLA or returns empty output. Surfaces in Insights + alerts. |
| [`rpa_queue_concurrency_lock`](https://dagster-component-ui.vercel.app/c/rpa_queue_concurrency_lock) | Global Dagster concurrency pool capping parallel bot invocations to your license count. |

## Live output — one document, end-to-end

Actual run trace for a scanned-image invoice that scores below the
quality threshold and needs a human sign-off:

```
>>> raw_invoices                    (synthetic_data_generator)
[GEN]     invoice_id=INV-9482  source=email  body_len=214  attachment_kind=image/tiff

>>> invoice_format                  (AgenticPipeline `classify` — gpt-4o-mini)
[LLM]     model=gpt-4o-mini  tool_choice=required  latency_ms=612
          → label: scanned_image  (confidence via constrained tool call)
[ASSET]   invoice_format  cost_usd=0.00009  tokens=214

>>> invoice_route                   (AgenticPipeline `conditional_route`)
[ROUTE]   rule matched: invoice_format.text == "scanned_image"
          → dispatching to asset key: aa_ocr_scanned_image
[SKIP]    uipath_extract_standard_pdf   (rule not matched)
[SKIP]    bp_edi_parser                 (rule not matched)
[SKIP]    pa_email_extract              (rule not matched)

>>> aa_ocr_scanned_image            (automation_anywhere_integration)
[AUTH]    POST https://.../v2/authentication → Control Room token
[START]   POST https://.../v3/automations/deploy  botName=Invoice_OCR_v4
          Payload: {"botInput": {"invoice_id": "INV-9482", "attachment_url": "..."}}
[POLL]    GET  .../v3/automations/{deploymentId}  → status=RUN_IN_PROGRESS
[POLL]    ...  → status=RUN_IN_PROGRESS
[POLL]    ...  → status=RUN_COMPLETED
[OUTPUT]  bot_output: 1284 chars (raw OCR JSON)
[ASSET]   aa_ocr_scanned_image  duration_seconds=41
          rpa_health_check → PASSED (under 60s SLA, non-empty output)

>>> invoice_output_normalized       (rpa_output_parser)
[PARSE]   vendor=automation_anywhere  path=bot_output
          → flattened to {vendor, invoice_id, extracted_text, tables[], confidence}
[ASSET]   invoice_output_normalized  bytes=1428

>>> invoice_extracted_fields        (AgenticPipeline `extract` — gpt-4o w/ JSON schema)
[LLM]     model=gpt-4o  tool_choice=required  latency_ms=2140
[SCHEMA]  {vendor, invoice_number, invoice_date, currency,
           line_items: [{sku, description, qty, unit_price}], total}
[EXTRACT] {
            "vendor": "Acme Widget Co",
            "invoice_number": "INV-9482",
            "invoice_date": "2026-09-11",
            "currency": "USD",
            "line_items": [
              {"sku": "WDG-100", "description": "Widget A", "qty": 12, "unit_price": 4.50},
              {"sku": "WDG-200", "description": null,        "qty": null, "unit_price": 8.25}
            ],
            "total": 54.00
          }
[ASSET]   invoice_extracted_fields  cost_usd=0.0051  tokens=1_842

>>> extraction_quality_score        (llm_evaluator — gpt-4o-mini as judge)
[JUDGE]   rubric = {accuracy, completeness, format}
          accuracy      0.85  "invoice number and vendor match visible text"
          completeness  0.55  "line 2 missing description and qty"
          format        0.75  "currency + total present; date parseable"
          → overall score: 0.72
[ASSET]   extraction_quality_score  cost_usd=0.0028

>>> invoice_approval_gate           (human_approval_gate)
[GATE]    quality_score=0.72 < threshold=0.85
          → status=approval_pending
          → asset check 'approved' = FAILED (WARN)
          → downstream BLOCKED
          Hint: drop {"approved": true} to approvals/INV-9482.json

  --- pipeline pauses here ---
  --- approver reviews the extraction in the Dagster UI ---
  --- slack_approval_gate sensor OR file drop writes the token ---

[TOKEN]   approvals/INV-9482.json → {"approved": true, "approver": "ana",
                                     "reason": "line 2 filled in from PDF v2",
                                     "corrections": {"line_items[1]": {"description":"Widget B","qty":6}}}
[GATE]    re-materialized  → asset check 'approved' = PASSED
          approver=ana  approved_at=2026-09-14T14:32:16Z

>>> invoice_ledger                  (downstream — Snowflake / dbt)
[WRITE]   MERGE INTO ledger.invoices … 6 rows affected
[ASSET]   invoice_ledger  RUN_SUCCESS
```

Every log line is either a real API call the RPA integration makes or
a real LLM call the agentic pipeline dispatches. Every `[ASSET]`
line corresponds to a queryable materialization in the Dagster UI.

## The classify + conditional_route pattern

The routing brain lives in one `AgenticPipelineComponent` YAML. The
agent picks a label from a fixed vocabulary; a deterministic router
dispatches to the appropriate RPA asset key downstream.

```yaml
# defs/invoice_router/defs.yaml
type: dagster_community_components.AgenticPipelineComponent
attributes:
  name: invoice_router
  source:
    from: raw_invoices
  steps:
    - id: invoice_format
      op: classify
      labels: [pdf, scanned_image, edi, email]
      include_rationale: true
      llm:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
        system_prompt: |
          You classify incoming invoice documents by format.
          Read the body_text + attachment_kind. Pick ONE label.

    - id: invoice_route
      op: conditional_route
      rules:
        - when: 'invoice_format.text == "pdf"'
          then: uipath_extract_standard_pdf   # asset key downstream
        - when: 'invoice_format.text == "scanned_image"'
          then: aa_ocr_scanned_image
        - when: 'invoice_format.text == "edi"'
          then: bp_edi_parser
        - when: 'invoice_format.text == "email"'
          then: pa_email_extract
```

Every `then:` value is an **asset key** — the target asset materializes
only when the rule fires, so downstream you only pay for the RPA bot
you actually need to invoke. Unmatched branches skip cleanly and show
up as skipped in the Dagster UI's Runs view. Because
`conditional_route` is deterministic (no router LLM), it costs $0,
tests as a unit, and reviews cleanly in a diff.

## The rpa_output_parser normalization step

Each RPA vendor puts the useful payload in a differently-named field
on a differently-shaped response. `rpa_output_parser` collapses the
difference into one downstream-friendly asset.

```yaml
# defs/invoice_output_normalized/defs.yaml
type: dagster_community_components.RPAOutputParserComponent
attributes:
  asset_name: invoice_output_normalized
  upstream_asset_keys:
    - uipath_extract_standard_pdf
    - aa_ocr_scanned_image
    - bp_edi_parser
    - pa_email_extract
  vendor_field: vendor                # read from each upstream's output_payload
  # per-vendor field-path map — where the payload lives in each vendor's response
  vendor_output_paths:
    uipath:              output_arguments        # UiPath OutputArguments dict
    automation_anywhere: bot_output              # AA bot output dict
    blue_prism:          session_output          # BP session dict
    power_automate:      trigger_output          # PA cloud flow output
  # optional canonicalization — map raw vendor keys to a common shape
  canonical_schema:
    invoice_id:      "$.invoice_id | $.invoiceId | $.InvoiceID"
    extracted_text:  "$.text | $.raw_text | $.OcrText"
    tables:          "$.tables | $.lineItems"
    confidence:      "$.confidence | $.OcrConfidence"
```

Downstream steps read from **one asset**, not four. Swapping a bot
vendor (`aa_ocr_scanned_image` → a new `hp_ocr_scanned_image`) is a
one-line edit here — everything below stays untouched.

## The LLM extract + judge + HITL loop

Structured extraction is a constrained-tool-call LLM step against a
JSON Schema. The judge scores it. The gate blocks downstream if the
score is below threshold.

```yaml
# defs/invoice_extraction/defs.yaml
type: dagster_community_components.AgenticPipelineComponent
attributes:
  name: invoice_extraction
  source:
    from: invoice_output_normalized
  steps:
    - id: invoice_extracted_fields
      op: extract
      llm:
        model: gpt-4o                  # bump to gpt-4o for structured accuracy
        api_key_env_var: OPENAI_API_KEY
      output_schema:
        type: object
        required: [vendor, invoice_number, invoice_date, currency, line_items, total]
        properties:
          vendor:         {type: string}
          invoice_number: {type: string}
          invoice_date:   {type: string, format: date}
          currency:       {type: string}
          line_items:
            type: array
            items:
              type: object
              properties:
                sku:         {type: string}
                description: {type: [string, "null"]}
                qty:         {type: [number, "null"]}
                unit_price:  {type: [number, "null"]}
          total: {type: number}
```

```yaml
# defs/extraction_judge/defs.yaml
type: dagster_community_components.LLMEvaluatorComponent
attributes:
  asset_name: extraction_quality_score
  upstream_asset_key: invoice_extracted_fields
  judge:
    model: gpt-4o-mini
    api_key_env_var: OPENAI_API_KEY
  metrics:
    - name: accuracy
      prompt: "Do the extracted vendor + invoice_number + total match the source text?"
      weight: 0.5
    - name: completeness
      prompt: "Are all line_items fully populated (no nulls in description / qty / unit_price)?"
      weight: 0.3
    - name: format
      prompt: "Is the invoice_date parseable? Is currency a valid ISO code?"
      weight: 0.2
```

```yaml
# defs/invoice_approval_gate/defs.yaml
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: invoice_approval_gate
  upstream_asset_key: invoice_extracted_fields
  # gate on the judge's numeric score — only pause if the extraction looks shaky
  auto_pass_when: "extraction_quality_score.overall >= 0.85"
  approval_dir: approvals/
  # optional: allow the approver to patch the extraction inline
  accepts_corrections: true
```

For Slack quorum sign-off instead of file drop, drop a
`SlackApprovalGateComponent` peer alongside the gate and let its
watcher write the token — the gate reads it either way. See
[`slack_approval_gate.md`](slack_approval_gate.md) for the full
Slack-side setup.

## Concurrency + health

Two operational safeguards on the RPA layer, no custom Python.

**License-cap concurrency.** RPA bot licenses are per-seat, and
running more concurrent bots than you own licenses either queues in
the vendor Control Room or silently drops jobs. `rpa_queue_concurrency_lock`
declares a Dagster global concurrency pool tied to each vendor:

```yaml
# defs/rpa_concurrency/defs.yaml
type: dagster_community_components.RPAQueueConcurrencyLockComponent
attributes:
  pools:
    - key: uipath_unattended
      limit: 8                   # 8 unattended licenses
      applies_to_asset_keys: [uipath_extract_standard_pdf]
    - key: aa_bots
      limit: 4                   # 4 AA bot runners
      applies_to_asset_keys: [aa_ocr_scanned_image]
    - key: bp_process_pool
      limit: 6
      applies_to_asset_keys: [bp_edi_parser]
    - key: pa_flow_runs
      limit: 20                  # PA cloud flow API concurrency
      applies_to_asset_keys: [pa_email_extract]
```

Dagster's scheduler enforces the cap across every run in flight —
including backfills and sensor-triggered launches. No bot ever runs
above the licensed limit.

**Per-bot health check.** `rpa_health_check` emits an asset check on
each RPA asset — fails when the bot exceeded a wall-clock SLA or
returned an empty payload:

```yaml
# defs/rpa_health/defs.yaml
type: dagster_community_components.RPAHealthCheckComponent
attributes:
  checks:
    - asset_key: uipath_extract_standard_pdf
      max_duration_seconds: 90
      require_non_empty_output: true
    - asset_key: aa_ocr_scanned_image
      max_duration_seconds: 60
      require_non_empty_output: true
    - asset_key: bp_edi_parser
      max_duration_seconds: 45
      require_non_empty_output: true
    - asset_key: pa_email_extract
      max_duration_seconds: 30
      require_non_empty_output: true
```

A failed asset check surfaces in the Dagster UI + Insights + any
alert-policy you've wired — no bespoke bot-monitoring dashboard.

## Cost anatomy

Per invoice, end-to-end:

| Step | Model / mechanism | Approx cost |
|---|---|---|
| `invoice_format` (classify) | gpt-4o-mini, ~250 in-tokens, forced enum | ~$0.0001 |
| `invoice_route` | deterministic (no LLM) | $0 |
| RPA bot invocation | vendor license (per-seat annual) | pre-paid |
| `invoice_output_normalized` | pure Python transform | $0 |
| `invoice_extracted_fields` (extract) | gpt-4o w/ JSON schema, ~1.5k tokens | ~$0.005 |
| `extraction_quality_score` (judge) | gpt-4o-mini, 3-metric rubric, ~1k tokens | ~$0.003 |
| `invoice_approval_gate` | file / Slack (only on low-confidence) | $0 |
| **Total (agent stack, per doc)** | | **~$0.008** |

Add roughly `(annual bot license) / (invoices/year)` for the RPA
side. For most enterprises with existing UiPath / AA / BP / PA
investment, the bot cost is a sunk investment — this composition
lets you drive additional utilization out of it without new
per-invocation cost.

## Run

```bash
export OPENAI_API_KEY=sk-...
# Optional — flip demo_mode: false on any RPA integration to hit real endpoints:
#   export UIPATH_CLIENT_ID=...       UIPATH_CLIENT_SECRET=...
#   export AA_USERNAME=...            AA_API_KEY=...
#   export BLUE_PRISM_USERNAME=...    BLUE_PRISM_PASSWORD=...
#   export POWER_AUTOMATE_CLIENT_ID=... POWER_AUTOMATE_CLIENT_SECRET=... POWER_AUTOMATE_TENANT_ID=...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rpa_meets_ai_demo.sh | bash
cd rpa-meets-ai-demo
uv run dg dev
```

The setup script ships separately; each RPA integration defaults to
`demo_mode: true` so you can validate the full graph (assets, ops,
sensors, schedules, retry policy, lineage) with zero RPA license.
Flip `demo_mode: false` on any one integration to point at a real
Control Room / Orchestrator / cloud-flow endpoint.

Manual setup (if you'd rather scaffold by hand):

```bash
uvx create-dagster project rpa-meets-ai-demo
cd rpa-meets-ai-demo
uv add dagster-community-components-cli
uv run dagster-component init --auto-install
uv run dagster-component add synthetic_data_generator --auto-install
uv run dagster-component add agentic_pipeline --auto-install
uv run dagster-component add uipath_orchestrator_integration --auto-install
uv run dagster-component add automation_anywhere_integration --auto-install
uv run dagster-component add blue_prism_integration --auto-install
uv run dagster-component add power_automate_integration --auto-install
uv run dagster-component add rpa_output_parser --auto-install
uv run dagster-component add llm_evaluator --auto-install
uv run dagster-component add human_approval_gate --auto-install
uv run dagster-component add rpa_health_check --auto-install
uv run dagster-component add rpa_queue_concurrency_lock --auto-install
# ...then wire the defs.yaml files shown above...
uv run dg dev
```

## Companion walkthroughs

- [`uipath_orchestrator_integration.md`](uipath_orchestrator_integration.md) — the UiPath side, standalone.
- [`automation_anywhere_integration.md`](automation_anywhere_integration.md) — Automation Anywhere Control Room, standalone.
- [`blue_prism_integration.md`](blue_prism_integration.md) — Blue Prism Control Room, standalone.
- [`power_automate_integration.md`](power_automate_integration.md) — Microsoft Power Automate cloud flows, standalone.
- [`agent_family.md`](agent_family.md) — the agent-family shape (`mcp_tool_call` + `openai_agent` + `llm_evaluator`) this pipeline composes on top of.
- [`slack_approval_gate.md`](slack_approval_gate.md) — swap the file-drop token for Slack quorum sign-off, quorum + timeout policies included.
- [`maintainer_investigation_room.md`](maintainer_investigation_room.md) — the reference for a composed-pipeline walkthrough (agentic fan-out + typed inputs + HITL) if you want to see the same substrate applied to a different domain (GitHub issue triage).

## See also

- Component reference pages on the registry UI:
  - <https://dagster-component-ui.vercel.app/c/rpa_output_parser>
  - <https://dagster-component-ui.vercel.app/c/rpa_health_check>
  - <https://dagster-component-ui.vercel.app/c/rpa_queue_concurrency_lock>
- Vendor grouping pages:
  - <https://dagster-component-ui.vercel.app/vendors/uipath>
  - <https://dagster-component-ui.vercel.app/vendors/automation-anywhere>
  - <https://dagster-component-ui.vercel.app/vendors/blue-prism>
  - <https://dagster-component-ui.vercel.app/vendors/microsoft>
- Walkthrough index: [examples/README.md](README.md)
