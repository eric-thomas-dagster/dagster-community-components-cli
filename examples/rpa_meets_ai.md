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
with built-in judges → a human approval
gate blocks the downstream data load until an approver writes an approval token.**

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
                                  │              built-in judges (answer_relevance, coherence…)
                                  │              per-judge score in [0, 1]
                                  ▼
                    invoice_approval_gate       (human_approval_gate)
                                  │              blocks until an approval token exists
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
| [`llm_evaluator`](https://dagster-component-ui.vercel.app/c/llm_evaluator) | LLM-as-judge scoring the extracted fields against a rubric. Emits a 0-1 score + reason per built-in evaluation (`score.<name>` metadata). |
| [`human_approval_gate`](https://dagster-component-ui.vercel.app/c/human_approval_gate) | Approval-token gate asset. Passes on `{approved: true}` token; blocks downstream on missing / rejected. Compatible with Slack quorum via `slack_approval_gate`. |
| [`rpa_health_check`](https://dagster-component-ui.vercel.app/c/rpa_health_check) | Per-RPA-asset check. Fails when a bot's latest run is stale, not in a success status, or returned empty output. Surfaces in Insights + alerts. |
| [`rpa_queue_concurrency_lock`](https://dagster-component-ui.vercel.app/c/rpa_queue_concurrency_lock) | Event-log-backed concurrency pool capping parallel bot invocations to your license count. |

## Live output — one document, end-to-end

Illustrative run trace (shape, not captured output) for a scanned-image invoice that scores below the
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

The trace is a sketch of the intended flow: log formats, scores, the
threshold-driven `[GATE]` decision and the `corrections` token field are
illustrative. As shipped, the gate is token-only (see the gate notes
below) and `conditional_route` picks a specialist rather than dispatching
to an asset key.

## The classify + conditional_route pattern

The routing brain lives in one `AgenticPipelineComponent` YAML. The
agent picks a label from a fixed vocabulary; a deterministic router
dispatches to the appropriate RPA asset key downstream.

```yaml
# defs/invoice_router/defs.yaml
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: invoice            # steps become invoice_format, invoice_route
  group_name: invoice_routing
  source:
    kind: upstream_asset
    upstream_asset_key: raw_invoices
  steps:
    - id: format
      op: classify
      labels: [pdf, scanned_image, edi, email]
      include_rationale: true
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You classify incoming invoice documents by format.
        Read the body_text + attachment_kind. Pick ONE label.

    - id: route
      op: conditional_route
      source: format
      conditions:
        - when: {equals: pdf}
          then: uipath_standard_pdf
        - when: {equals: scanned_image}
          then: aa_ocr_scanned_image
        - when: {equals: edi}
          then: bp_edi_parser
        - when: {equals: email}
          then: pa_email_extract
      default: pa_email_extract
      specialists:
        - name: uipath_standard_pdf
          description: "Prepare the UiPath standard-PDF extraction job arguments."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Return the JSON input arguments for the UiPath standard PDF invoice process."
        - name: aa_ocr_scanned_image
          description: "Prepare the Automation Anywhere OCR bot input."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Return the JSON botInput for the scanned-image OCR bot."
        - name: bp_edi_parser
          description: "Prepare the Blue Prism EDI parser session inputs."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Return the JSON inputs for the EDI parser process."
        - name: pa_email_extract
          description: "Prepare the Power Automate email-extraction flow trigger."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Return the JSON trigger payload for the email extraction flow."
  outputs:
    assets: [format, route]
```

`conditional_route` is a deterministic picker (`equals` / `contains` /
`regex` / `jsonpath` on the upstream step text, first match wins, `default`
required). It chooses which **specialist** (a small LLM call with its own
system prompt) answers, with no router LLM, so the pick itself costs $0
and tests as a unit. It does **not** by itself decide which downstream
asset materializes: the bot assets are separate integration components, and
the routing decision is available to them as the `invoice_route` asset's
text/metadata. If you need a branch asset to be skipped for unpicked
branches, use `LlmMultiPathRouterComponent`, which emits only the outputs
the classifier picks (see [`agentic_router_plain.md`](agentic_router_plain.md)).

## The rpa_output_parser normalization step

Each RPA vendor puts the useful payload in a differently-named field
on a differently-shaped response. `rpa_output_parser` reads each upstream
bot's `output_payload` metadata, picks the vendor's payload field using a
built-in map keyed on the payload's `vendor` value (UiPath, Automation
Anywhere, Blue Prism, Power Automate), and emits one row per upstream with
the columns `vendor, run_id, status, asset_key, materialized_at,
output_field, raw_payload`. The vendor map is built in and there is no
per-field canonical-schema mapping: downstream steps (the `extract` step
below) do the field-level normalization.

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
  group_name: rpa_normalized
  fail_on_missing_upstream: false     # true -> raise if an upstream bot never ran
```

Downstream steps read from **one asset**, not four. Swapping a bot
vendor (`aa_ocr_scanned_image` → a new `hp_ocr_scanned_image`) is a
one-line edit to `upstream_asset_keys` — everything below stays untouched.

## The LLM extract + judge + HITL loop

Structured extraction is a constrained-tool-call LLM step against a
JSON Schema. The judge scores it. The gate blocks downstream if the
score is below threshold.

```yaml
# defs/invoice_extraction/defs.yaml
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: invoice            # step `extracted_fields` -> invoice_extracted_fields
  group_name: invoice_extraction
  source:
    kind: upstream_asset
    upstream_asset_key: invoice_output_normalized
  steps:
    - id: extracted_fields
      op: extract
      model: gpt-4o                    # bump to gpt-4o for structured accuracy
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
  outputs:
    assets: [extracted_fields]
```

```yaml
# defs/extraction_judge/defs.yaml
type: dagster_community_components.LLMEvaluatorComponent
attributes:
  asset_name: extraction_quality_score
  upstream_asset_key: invoice_extracted_fields
  answer_jsonpath: text               # the extract step's JSON text
  evaluations:                        # built-in judges; each is one LLM call
    - answer_relevance
    - coherence
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
```

`llm_evaluator` runs a fixed menu of judges (`answer_relevance`,
`groundedness`, `context_relevance`, `harmfulness`, `helpfulness`,
`coherence`), each scored 0-1 with a reason, and surfaces every score as
`score.<name>` metadata. Custom rubric prompts and weighted overall scores
are not supported by this component.

```yaml
# defs/invoice_approval_gate/defs.yaml
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: invoice_approval_gate
  upstream_asset_key: invoice_extracted_fields
  approval_dir: /tmp/approvals
  default_approval_key: default       # unpartitioned: reads /tmp/approvals/default.json
  deps: [extraction_quality_score]    # lineage: the approver reviews the judge scores first
```

The gate is token-based: it fails with `approval_pending` until
`<approval_dir>/<partition_key>.json` contains `{"approved": true, ...}`, and
passes the upstream payload through once it does. It has no score-threshold
auto-pass and no inline corrections: the approver reads the judge's
`score.*` metadata on `extraction_quality_score` and decides whether to write
the token. (A threshold-driven auto-approve would need a small custom asset
in front of the gate.)

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
keeps an event-log-backed concurrency pool per vendor:

```yaml
# defs/rpa_concurrency/defs.yaml  (one instance per pool; `---` separates documents)
type: dagster_community_components.RPAQueueConcurrencyLockComponent
attributes:
  asset_name: uipath_unattended_slot
  pool_key: uipath_unattended
  max_concurrent: 8                  # 8 unattended licenses
  on_capacity: wait                  # or skip | fail
---
type: dagster_community_components.RPAQueueConcurrencyLockComponent
attributes:
  asset_name: aa_bot_slot
  pool_key: aa_bots
  max_concurrent: 4                  # 4 AA bot runners
---
type: dagster_community_components.RPAQueueConcurrencyLockComponent
attributes:
  asset_name: bp_process_slot
  pool_key: bp_process_pool
  max_concurrent: 6
---
type: dagster_community_components.RPAQueueConcurrencyLockComponent
attributes:
  asset_name: pa_flow_slot
  pool_key: pa_flow_runs
  max_concurrent: 20                 # PA cloud flow API concurrency
```

Each instance emits one slot-holding asset (`asset_name`) that acquires a
slot in the named pool, and every asset sharing a `pool_key` shares one
counter. Pool state is kept as acquire/release observations in the Dagster
event log (stale acquires expire after `ttl_seconds`), so it counts across
runs, backfills and sensor-launched runs. When the pool is full the step
polls (`wait`), skips, or fails per `on_capacity`. Attach it to a bot with
`upstream_asset_key`; it does not wrap other assets by name.

**Per-bot health check.** `rpa_health_check` attaches an asset check to an
RPA asset: it fails when the latest materialization is older than
`max_age_hours`, its `status` is not a success value, or (optionally) the
output payload is empty:

```yaml
# defs/rpa_health/defs.yaml  (one instance per bot asset)
type: dagster_community_components.RPAHealthCheckComponent
attributes:
  target_asset: uipath_extract_standard_pdf
  check_name: uipath_pdf_health
  max_age_hours: 6.0
  require_non_empty_output: true
  severity: ERROR                    # ERROR blocks downstream, WARN only flags
---
type: dagster_community_components.RPAHealthCheckComponent
attributes:
  target_asset: aa_ocr_scanned_image
  check_name: aa_ocr_health
  max_age_hours: 6.0
  require_non_empty_output: true
---
type: dagster_community_components.RPAHealthCheckComponent
attributes:
  target_asset: bp_edi_parser
  check_name: bp_edi_health
  max_age_hours: 6.0
  require_non_empty_output: true
---
type: dagster_community_components.RPAHealthCheckComponent
attributes:
  target_asset: pa_email_extract
  check_name: pa_email_health
  max_age_hours: 6.0
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
