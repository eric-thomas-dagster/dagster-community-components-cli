# Loom recording script — "AI-agentic-orchestration on Dagster, 3 shapes, all authored by Claude Code"

Target length: **6–7 minutes.** Target audience: prospects asking "how
does Dagster fit for AI agent workflows / agentic orchestration?"

Three progressively richer demos, each authored by Claude Code from a
natural-language prompt in one go. Same substrate for all three —
`AgenticPipelineComponent` — different ops per demo.

## Setup before recording

- Fresh terminal in an empty dir (`~/tmp/loom` or similar). Do `rm -rf`
  first so viewers see the bare state.
- Env vars exported:
  ```bash
  export OPENAI_API_KEY=sk-...
  export GITHUB_PERSONAL_ACCESS_TOKEN=$(gh auth token)   # for demo 3
  ```
- Claude Code (or Cursor / Copilot) open in a second window, pointed
  at the same dir.
- Optional browser tab: <https://dagster-component-ui.vercel.app/>

## Scene 1 — the framing (30s)

**Show:** empty terminal, empty directory.

**Say:**

> "AI-agentic orchestration — specialist agents fan out, a joiner
> synthesizes, sometimes a skeptic critiques, sometimes a human signs
> off — is a shape most teams end up needing. Dagster ships the
> primitive today. In the next six minutes I'll build three of them
> live: a research assistant, an investment memo, and a maintainer
> investigation room fetching a real GitHub issue. Every one is
> authored by Claude Code from a natural-language prompt. Every step
> becomes a first-class Dagster asset."

## Scene 2 — one-time setup (30s)

**Do:**

```bash
uvx --from dagster-community-components-cli dagster-component init
```

**Say:**

> "That one command wrote `CLAUDE.md`, `.cursorrules`, and Copilot
> instructions. Every coding agent I open here now knows about 963
> Dagster community components and how to search + compose them.
> Everything after this is prompts."

## Scene 3 — Demo 1: research bot (1 min)

**Do:** scaffold the project

```bash
uvx create-dagster@latest project research-bot --no-uv-sync
cd research-bot
```

**Do:** paste this prompt into Claude Code:

> *Build a 5-step research pipeline in one AgenticPipelineComponent
> (asset_name_prefix: `research_bot`). Emit: `baseline` (llm_call),
> `routed` (route: technical / general specialists), `refined`
> (critique_loop, 1 iteration), `debated` (debate: 2 proposers +
> arbitrator), `final` (synthesize the four prior). gpt-4o-mini
> throughout. Source is a literal "Explain how transformer attention
> works". Use `dagster-component add agentic_pipeline` and
> `dagster-component schema` to nail field names.*

**Say (while Claude works):**

> "Watch Claude run `dagster-component add agentic_pipeline` — that
> installs the component AND its pip deps. Then it fetches the schema
> and composes the YAML. No `pip install` surprises."

**Do:** when done, sync any missing pip deps + materialize:

```bash
uvx --from dagster-community-components-cli dagster-component sync-deps --auto-install
uv run dagster asset materialize --select '*' -m research_bot.definitions
```

**Say (over the sync-deps line):**

> "One command guarantees the venv has every dep the picked components
> need — whether Claude wrote the yaml via `dagster-component add` or
> directly. Walks every defs.yaml, resolves each component, installs
> anything missing. Same command applies to every demo in this Loom
> and to any customer scaffolding at home."

**Say (over the materialize):**

> "Five specialists ran, `final` synthesized them into one answer.
> All five are queryable Dagster assets — click any one to see the
> router's reasoning, cost, latency, model. `cd ..` and next demo."

## Scene 4 — Demo 2: investment memo (1 min)

**Do:**

```bash
cd ..
uvx create-dagster@latest project investment-memo --no-uv-sync
cd investment-memo
```

**Do:** paste this prompt into Claude Code:

> *Build an investment committee memo in one AgenticPipelineComponent
> (`debate` op). Partition over `[NVDA, TSLA, META]`. Three proposers
> — bull (BUY), bear (SELL), neutral (HOLD with a target price) — plus
> a committee-chair arbitrator that picks the pick best for a
> moderate-risk, long-horizon portfolio. Source is a literal `"Ticker:
> {partition_key}. Buy, hold, or sell?"`. gpt-4o-mini throughout.
> JSON-sink to `out/{partition_key}/investment_memo.json`.*

**Do:** when done, sync deps + materialize one partition:

```bash
uvx --from dagster-community-components-cli dagster-component sync-deps --auto-install
uv run dagster asset materialize --select '*' --partition NVDA -m investment_memo.definitions
```

**Say:**

> "Same component. Different op — debate. Three analysts argued, the
> arbitrator picked HOLD for NVDA. Click the recommendation asset →
> materialization metadata shows all three proposals verbatim,
> `arbitrator_reasoning`, `winner_index`, cost. Full audit trail for
> the committee record. `cd ..` and the finale."

## Scene 5 — Demo 3: maintainer investigation room (2.5 min)

**Do:**

```bash
cd ..
uvx create-dagster@latest project maintainer-triage --no-uv-sync
cd maintainer-triage
```

**Do:** paste this prompt into Claude Code:

> *Build an AI maintainer triage room for GitHub issues on public
> repos in one AgenticPipelineComponent (`asset_name_prefix: mir`).
> Step 1 is an `mcp_call` to the GitHub MCP server (`npx -y
> @modelcontextprotocol/server-github`, tool `get_issue`, args
> `{owner: dagster-io, repo: dagster, issue_number: 30000}`, parse_as
> auto). Then 4 llm_call specialists (repo/code, docs, reproduction,
> prior history) each with typed `inputs: {issue_facts: {from: intake}}`.
> Then a `synthesize` with 5 typed named inputs joining all evidence.
> Then a `report` llm_call rendering maintainer-facing markdown. Add
> a `HumanApprovalGateComponent` gating the report on a JSON token
> file, plus a `FilesystemMonitorSensorComponent` + `AssetJobComponent`
> for auto-progression. Use `dagster-component add` for each pick.*

**Say (while Claude works — ~important nuance to call out):**

> "This is the meta-component pattern — the whole 9-node execution
> plan is inside ONE component's config. `mcp_call` gives us a
> deterministic tool step. Typed named `inputs:` let each downstream
> step read specific prior outputs by port name — the standard
> execution-plan graph shape."

**Do:** when Claude finishes, sync deps + materialize:

```bash
uvx --from dagster-community-components-cli dagster-component sync-deps --auto-install
uv run dagster asset materialize --select 'mir_intake' -m maintainer_triage.definitions
uv run dagster asset materialize \
  --select 'mir_repo_evidence,mir_docs_evidence,mir_reproduction,mir_history_evidence,mir_preliminary,mir_skeptic,mir_decision,mir_report' \
  -m maintainer_triage.definitions
```

**Do:** open the draft report file and scroll.

**Say:** "Real triage of a real open GitHub issue. Classification,
confidence, owner, next action, uncertainty section. Same shape you'd
send back to the reporter."

**Do:** open `dg dev` and go to the asset graph.

**Say:**

> "Every node is an asset key. `mir_repo_evidence`, `mir_preliminary`,
> `mir_report` — all queryable. Point a dbt model at `mir_report`.
> Wire it into Insights alerting. Materialize a downstream Snowflake
> task off it. The AI-agentic plan isn't in a sidecar workflow tab —
> it's IN the org's data graph."

**Do:** drop an approval token:

```bash
echo '{"approved":true,"approver":"me"}' > approvals/default.json
```

**Say:** "The gate's watching that directory. Sensor picks it up in
5 seconds, launches the ship job automatically. Human sign-off as a
first-class primitive — the check surfaces in the UI, in Insights, and
in any alerting pipeline."

## Scene 6 — close (30s)

**Say:**

> "Three demos, all authored by Claude Code from natural-language
> prompts, all landing on first-class Dagster assets. Whether you want
> the YAML in git or the NL prompt in git, both are one flag away —
> see the `pca_*` variants for the state-backed authoring path.
>
> Everything you saw is on the community registry site. Six
> walkthroughs — three shapes × two authoring paths. Link in the
> description."

## What to have on standby

- Backup screenshot of each completed asset graph in case any `dg dev`
  boot is slow on recording day
- The `investigation_draft.md` from a prior run so the maintainer
  report is visible even if the live MCP call has any hiccup
- The reference prompts above in a text file, ready to paste

## What NOT to say

- Don't compare pricing.
- Don't name a specific competitor as the target of any differentiator —
  frame everything as pure Dagster capability.
- Don't demo BOTH the `mcp_call`-op version AND the `MCPToolPickerComponent`
  version of the maintainer. Pick one for the Loom. The `mcp_call` version
  is more visually cohesive for the "one execution plan" beat.
