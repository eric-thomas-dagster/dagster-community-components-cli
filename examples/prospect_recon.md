# Prospect Recon — a real production routine, not an illustration

A full-fidelity Dagster port of a real, personal routine run every weekday
evening against an actual sales-engineering job: autonomously decide which
handed-off prospect to work on, research them across Calendar/Drive/the web,
build a governed knowledge base, write a structured brief, commit it to
GitHub, and notify a human. A second routine ("Demo Factory", not part of
this demo) reads that brief overnight to build a demo — unmodified, so this
writes into its exact expected files and format.

This is not an illustrative toy. The real routine's full instructions (not
summarized) drove every design decision here, and it's meant to be run
against real accounts so it **replaces** the actual routine, not a demo of
it. It's also the flagship **Dagster + FastMCP + Prefect** story:

- **Dagster builds and shows the context.** The asset graph, materialization
  history, and the governed knowledge base itself
  (`prospect_recon_context_kb`) are real, inspectable Dagster assets — not a
  staged illustration.
- **FastMCP governs access to that context and writes receipts.**
  `mcp_servers/context_governance_server.py` is the access boundary for the
  generation-time receipt: the moment `prospect_recon_brief` actually
  consumes the governed KB to produce an artifact Demo Factory will read, it
  goes through an MCP tool call (`write_receipt`), not a direct in-process
  resource call. Build-time provenance (chunking, classification, the
  research tool-call trail) keeps its own existing, already-tested in-process
  `receipts:` wiring on `prospect_recon_context_kb` / `prospect_recon_research`.
- **Prefect builds the demo project.** That's Demo Factory, a separate,
  unmodified routine (out of scope here) that reads this demo's output from
  `dagster-demo-factory` overnight.

## Architecture

```
calendar_raw ─────────────┐
drive_recent_raw ─────────┼──▶ prospect_recon_candidate (request-file/ledger precedence,
drive_search_raw ─────────┘      then Calendar+Drive exclusion/dedup/tie-break)
                                           │
                                           ▼
                          prospect_recon_research (agentic tool_use_loop:
                                           │         Drive/Notion/web MCP search)
                                           ▼
                          prospect_recon_evidence_df (bridge)
                                           │
                                           ▼
                          prospect_recon_context_kb (chunk → redact → dedupe →
                                           │           classify → stamp → embed → index)
                                           ▼
                          prospect_recon_items_json (bridge)
                                           │
                                           ▼
                          prospect_recon_brief (forced-function-call extraction,
                                           │     full two-half doc, live template fetch)
                                           │
                                           ├──▶ mcp_servers/context_governance_server.py
                                           │      (FastMCP: write_receipt — generation-
                                           │       time context-consumption receipt)
                                           ▼
                          prospect_recon_commit_brief (commit to dagster-demo-factory)
                                     │           │              │
                                     ▼           ▼              ▼
                        prospect_recon_gmail_draft  prospect_recon_slack_notify
                                     │
                                     ▼ (also, directly off prospect_recon_brief)
                        prospect_recon_notion_brief (secondary output)
```

Trigger: `prospect_recon_schedule`, cron `30 18 * * 1-5` (6:30pm America/New_York,
weekdays) — matches the real routine's own trigger exactly.

## What each component does

- **`calendar_raw` / `drive_recent_raw` / `drive_search_raw`** — [`GoogleCalendarIngestionComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_calendar_ingestion) + two [`GoogleDriveIngestionComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_drive_ingestion) instances (a plain recent-files listing and a native-search pass — Drive's `q` search is blind to non-Google-native files, so the recent-listing pass is the only way to see PDFs/Office files at all).
- **`prospect_recon_candidate`** — plain Python (`prospect_recon_pipeline/definitions.py`), not a component instance: the real, deterministic port of the routine's request-file/dangling-ledger precedence + Calendar/Drive exclusion rules, dedup, and tie-break. Not an LLM judgment call — the routine's own rules are fully deterministic once signals are gathered.
- **`prospect_recon_research`** — [`AgenticPipelineComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/agentic_pipeline)'s `tool_use_loop` op over two stdio MCP servers: `mcp_servers/drive_search_server.py` (Drive + Notion search/read, reusing `google_drive_ingestion`/`notion_resource`'s own auth) and Tavily's own MCP server (`npx -y tavily-mcp`) for public web search. The routine's AE-notes search and public-web research are themselves agentic — it decides what to search next based on what it finds, not a fixed query list.
- **`prospect_recon_context_kb`** — [`ContextEngineeringPipelineComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/context_engineering_pipeline): chunk → redact (regex-based PII) → dedupe → classify → stamp → embed → index, with a [`ContextReceiptStoreComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/context_receipt_store) wired into `receipts:` for real retrieval/outcome provenance. The leadership-facing "vision" piece — a governed, receipted knowledge base per discovered prospect, not a staged illustration.
- **`prospect_recon_brief`** — plain Python, not an `AgenticPipelineComponent` instance: its `output_schema` is derived from `briefs/_TEMPLATE.md` fetched **live** from `dagster-demo-factory` at compute time, which a component's fixed-at-parse-time YAML config can't express. Forced function-calling (same mechanism `agentic_pipeline`'s own `extract` op uses) writes the full two-half document (discovery + "Build directives") as one coherent `markdown_body`, then calls the FastMCP governance server's `write_receipt` tool — best-effort; a governance-server failure logs a warning but never blocks the brief.
- **`prospect_recon_commit_brief`** — the hard integration contract: writes the brief + `state/ledger.json` update (+ archives a consumed request file) to `dagster-demo-factory` on `main` — direct push first, branch+PR+immediate-merge on conflict, exactly the routine's own fallback rule.
- **`prospect_recon_gmail_draft` / `prospect_recon_slack_notify`** — [`GmailDraftCreateComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/gmail_draft_create) (creates a reviewable draft only — the routine's instructions are explicit that Gmail cannot send) + [`SlackMessageSendComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/slack_message_send) (the routine's own preferred channel when available, since it can genuinely send).
- **`prospect_recon_notion_brief`** — [`NotionBriefPageComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/reverse_etl/notion_brief_page), a **secondary, parallel** output. The real routine's instructions never call Notion despite it being a listed connector — kept because an AE could plausibly start dropping discovery notes in Notion someday, the same role Drive plays today.

## Run

```bash
export GITHUB_TOKEN=ghp_...                        # dagster-demo-factory read/write
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json  # Calendar + Drive (SA shared on both)
export TAVILY_API_KEY=tvly-...
export OPENAI_API_KEY=sk-...                       # KB embeddings + research loop + brief extraction
export GMAIL_ACCESS_TOKEN=...                      # gmail.compose scope, draft only
export SLACK_BOT_TOKEN=xoxb-...
export NOTION_TOKEN=secret_...                     # optional, secondary brief copy
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_prospect_recon_demo.sh \
  -o setup_prospect_recon_demo.sh
bash setup_prospect_recon_demo.sh
```

Requirements: `uv`, `npx` (Node — for the Tavily web-search MCP server). Every
run makes real LLM calls (KB embeddings, the research tool-use loop, brief
extraction) and, if a candidate is found, a real commit + PR + merge against
`PROSPECT_RECON_DEMO_FACTORY_REPO`, a real Gmail draft, and a real Slack DM —
point `PROSPECT_RECON_DEMO_FACTORY_REPO` at a scratch/fork repo the first few
times you run this.

Unlike every other demo in this directory, **the script does not auto-launch
it**. There's no safe synthetic scenario to run against — this depends on
your own real Calendar/Drive data and writes to a real downstream repo,
inbox, and Slack. The script scaffolds the full project and stops; fill in
real credentials in `.env`, then:

```bash
set -a && source .env && set +a
uv run dg check defs
uv run dg dev
```

Materialize via the UI (`prospect_recon` asset group), or:

```bash
uv run dg launch --select "prospect_recon_candidate+"
```

To close the receipt loop after a real sales call:

```bash
uv run python3 -c "
from dagster_community_components import ContextReceiptStore
store = ContextReceiptStore('./data/prospect-recon-receipts.sqlite3')
store.record_outcome('<receipt_id from the run's logs>', signal='thumbs_up')
"
```

## Known, documented simplifications (see code comments for the full reasoning)

- **Signal B's company-from-file judgment call** is an LLM judgment call,
  matching the real routine: `_classify_drive_company` reads a Drive file's
  actual content (`google_drive_ingestion`'s `inline_text_content` extracts
  real text from PDF/.docx/.xlsx too, via pdfplumber/python-docx/openpyxl,
  not just plain-text-ish MIME types) and weighs it against domains already
  found via Calendar. `_guess_company_from_filename` is kept only as the
  last-resort fallback, reached only for file types with no text
  representation at all (images, unknown binary formats), when those
  optional extraction libraries aren't installed, or when the LLM itself has
  low/no confidence.
- **The brief authors the full two-half document** (discovery +
  "Build directives": asset graph sizing, fidelity, demo shape, checks, demo
  mode, three buckets, demo name, money shot, capability talk track —
  confirmed via a live fetch of `briefs/_TEMPLATE.md` and a real deployed
  brief) as one coherent `markdown_body`, instructed directly against the
  live-fetched template text — not decomposed into dozens of schema fields
  and reassembled, which would be far more fragile for a document this rich
  (a table, several bulleted lists, a multi-paragraph talk track).
  `derive_schema_from_template` / `FALLBACK_BRIEF_SCHEMA` stay as a lighter
  structural signal (schema_source metadata, a handful of structured fields
  the Gmail/Slack notifications use directly).
- **`state/ledger.json`'s real shape** (confirmed via a live fetch, not
  assumed) is two buckets — `"brief-ready"` and `"built"`, each a list of
  entries keyed by `slug`/`company` (no `domain` field, and the bucket name
  `"built"` doesn't match its own entries' `status: "deployed"` string, kept
  faithfully). The dangling-ledger fallback reads the `"brief-ready"`
  bucket; the "already deployed" exclusion rule matches by slug against
  `"built"`.
- **The no-candidate short-circuit is partial.** When no real candidate is
  found, downstream assets still materialize (Dagster doesn't make this free
  to skip through generic pipeline components without modifying them) but
  the research step is explicitly instructed not to call any tools and
  finalize immediately, and the brief/commit/notify assets short-circuit to
  a minimal "no candidate, here's why" message — the same behavior the real
  routine's own "notify me (draft + push), stop" rule describes, at the cost
  of one still-cheap LLM call instead of zero.
- **Notion is a secondary output, not part of the real contract.** The real
  routine's instructions never call Notion despite it being a listed
  connector — `notion_brief_page`'s YAML says so explicitly.
- **Multi-account (Dagster + Prefect) ingestion is deferred.** Every
  ingestion leg here is parameterized by its own credential env vars, so a
  second (e.g. Prefect-side) Google/Notion account is new `defs/` instances
  pointed at different credentials later — not a redesign. Not built here;
  there's no FastMCP gateway on that side yet.
