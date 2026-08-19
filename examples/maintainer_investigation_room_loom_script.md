# Loom recording script — "AI-agentic-orchestration on Dagster, 3 shapes, all authored by Claude Code"

Target length: **7–8 minutes.** Target audience: prospects asking "how
does Dagster fit for AI agent workflows / agentic orchestration?"

Three progressively richer demos, each authored by Claude Code from a
natural-language prompt in one go. Same substrate for all three —
`AgenticPipelineComponent` — different ops per demo. **Optional Scene
5b** shows the upgrade path — every new primitive from v0.10.69–v0.10.73
composed on top of the same MIR pattern (per_step_ops, tool_use_loop,
handoff to LangGraph, debate, critique_loop, SlackApprovalGate).
Recommended for the 8-minute version; skip for the 6-minute cut.

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

## Scene 2 — the one command customers need per project (30s)

**Say:**

> "Every demo I'm about to run has the same one-command setup after
> `uvx create-dagster project`: `dagster-component init`. That one
> command does three things:
>
> 1. Writes `CLAUDE.md` / `.cursorrules` / Copilot instructions so the
>    coding agent knows about 963 community components.
> 2. Injects the Python entry point into `pyproject.toml` so the
>    Dagster UI's Components tab lists this project's components.
> 3. Runs `uv pip install -e .` so the entry point actually registers.
>
> Skip any of the three demos' `init` step and the UI won't see your
> project. Include it and everything Just Works."

## Scene 3 — Demo 1: research bot (1 min)

**Do:** scaffold + init + persistent DAGSTER_HOME

```bash
uvx create-dagster@latest project research-bot --no-uv-sync
cd research-bot
uv sync
uvx --from dagster-community-components-cli dagster-component init --auto-install
export DAGSTER_HOME=$(pwd)/.dagster_home && mkdir -p "$DAGSTER_HOME"
```

**Say (over init running):**

> "Init just wrote three AI-tool files, added the entry point to
> pyproject.toml, and editable-installed the project. Any component
> Claude Code adds from here on will show in the Dagster UI."

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
uv sync
uvx --from dagster-community-components-cli dagster-component init --auto-install
export DAGSTER_HOME=$(pwd)/.dagster_home && mkdir -p "$DAGSTER_HOME"
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
uv sync
uvx --from dagster-community-components-cli dagster-component init --auto-install
export DAGSTER_HOME=$(pwd)/.dagster_home && mkdir -p "$DAGSTER_HOME"
```

**Do:** paste this prompt into Claude Code:

> *Build an AI maintainer triage room for GitHub issues on public
> repos in one AgenticPipelineComponent (`asset_name_prefix: mir`).
> Make it dynamic-partitioned on a DynamicPartitionsDefinition called
> `mir_investigations` with `partition_key_parser:
> "{owner}/{repo}#{issue_number:int}"`, so every triage is one
> persistent partition. Step 1 is an `mcp_call` to the GitHub MCP
> server (`npx -y @modelcontextprotocol/server-github`, tool
> `get_issue`, args `{owner: "{partition.owner}", repo:
> "{partition.repo}", issue_number: "{partition.issue_number}"}`,
> parse_as auto). Then 4 llm_call specialists (repo/code, docs,
> reproduction, prior history) each with typed `inputs: {issue_facts:
> {from: intake}}`. Then a `synthesize` with 5 typed named inputs
> joining all evidence, a skeptic critique, a final decision, and a
> `report` llm_call rendering maintainer-facing markdown (heading
> `# Issue triage — {partition.owner}/{partition.repo}#{partition.issue_number}`).
> Add a `PartitionedAssetLauncherJobComponent` (`job_name:
> launch_mir_triage`) as the config-driven entry point — targets all
> 9 mir_* asset keys, dynamic_partitions_name `mir_investigations`,
> partition_key_template `"{owner}/{repo}#{issue_number}"`, config_schema
> `{owner: {type: str, default: dagster-io}, repo: {type: str, default:
> dagster}, issue_number: {type: int}}`. Add a HumanApprovalGateComponent
> gating the report on a JSON token file, plus a
> FilesystemMonitorSensorComponent + AssetJobComponent for
> auto-progression. Use `dagster-component add` for each pick.*

**Say (while Claude works — key nuance to call out):**

> "Two moves here. First: the whole 9-node execution plan lives in one
> AgenticPipelineComponent, dynamic-partitioned. Every triage becomes
> one persistent partition. Second: a companion launcher job is the
> config-shaped entry point — takes owner/repo/issue_number as run
> config, derives the partition key, materializes the pipeline. Same
> entry point for a human clicking Materialize in the UI and for an
> external system POSTing run_config via GraphQL."

**Do:** when Claude finishes, sync deps + launch via the config-driven
launcher (partition_key derived at run time):

```bash
uvx --from dagster-community-components-cli dagster-component sync-deps --auto-install

cat > /tmp/mir_launch.yaml <<CFG
ops:
  launch_mir_triage_op:
    config:
      owner: dagster-io
      repo: dagster
      issue_number: 30000
CFG
uv run dagster job execute -m maintainer_triage.definitions -j launch_mir_triage --config /tmp/mir_launch.yaml
```

**Say:** "One command. The launcher registered a new dynamic partition
`dagster-io/dagster#30000` and materialized every step of the pipeline
for that partition. Every subsequent triage — same launcher, different
config, new partition — nothing to edit in YAML."

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

## Scene 5b — the "comprehensive" MIR-v3 upgrade path (1 min)

**Purpose:** show off what we shipped on top of the base MIR pattern —
per-step ops, agent tool-use loops, framework composition, Slack quorum
HITL. Not a fresh scaffold; just walk through the v3 walkthrough page
+ Runs page of a pre-materialized run.

**Do:** open [`maintainer_investigation_room_v3.md`](maintainer_investigation_room_v3.md)
in a browser tab.

**Say:**

> "That same triage pipeline can be extended with everything else we
> shipped. `per_step_ops: true` on the AgenticPipelineComponent flips
> it from one op in the Runs page to 11 ops — every step is
> independently retryable, dbt-style resume. The specialists get more
> ambitious: `tool_use_loop` gives one of them GitHub MCP tools to
> iteratively explore the codebase; `handoff` lets us drop an existing
> LangGraph state machine in for the reproduction analysis without
> rewriting it. Then `debate` runs 3 skeptics against the preliminary
> triage; `critique_loop` iterates the final report with a critic;
> `SlackApprovalGateComponent` posts to a channel and writes the
> approval token on N-of-M reaction quorum. Same substrate, same YAML
> shape — just more primitives composed together."

**Do:** open `dg dev` for the v3 project (or a screenshot), navigate to
the pipeline's Runs page. Point at the 11 step_keys.

**Say:**

> "Compare to the earlier demo — one op, whole-pipeline retry. Here,
> 11 ops. If step 6 fails, retry restarts from step 6, upstream state
> already on the IO manager. And the tool_use_loop op — I can click
> into that step's materialization, see the full 5-8 tool call trace
> in metadata: which MCP tool the LLM called, what args, what came
> back, what it did next."

**Do:** click into a `mir_repo_evidence` materialization's metadata,
show the `tool_call_trace` JSON blob.

**Say:**

> "Handoff step's materialization shows the same for LangGraph —
> per-node trajectory in metadata, framework result as a first-class
> asset object. Framework is one node in the Dagster graph; Dagster is
> the harness at the pipeline level. Both stories work."

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
