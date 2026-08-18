# Loom recording script — "AI maintainer investigation room, authored by Claude Code, on Dagster"

Target length: **4–5 minutes.** Target audience: prospects asking "how does
Dagster fit for AI agent workflows / agentic orchestration?"

## Setup before recording

- Fresh terminal in an empty dir (`~/tmp/loom-demo` or similar). Do
  `rm -rf` first so viewers see the bare state.
- Two env vars exported:
  ```bash
  export OPENAI_API_KEY=sk-...
  export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
  ```
- Claude Code (or Cursor / Copilot) open in a second window, pointed at
  the same dir.
- Browser tab open on <https://dagster-component-ui.vercel.app/examples/maintainer_investigation_room>.

## Scene 1 — the setup (30s)

**Show:** the empty terminal, empty directory.

**Say:**

> "AI-agentic-orchestration is the pattern where a coding agent authors
> a multi-node LLM workflow — specialist agents fan out, a triage joiner
> synthesizes, a skeptic critiques, a human signs off. Dagster ships all
> the pieces today. In the next four minutes I'll go from an empty
> directory to a real, materialized maintainer investigation on a live
> GitHub issue — authored by Claude Code, all first-class Dagster assets."

## Scene 2 — zero to something, live (2 min)

**Do:**

```bash
uvx create-dagster@latest project maintainer-triage --no-uv-sync
cd maintainer-triage
uvx --from dagster-community-components-cli dagster-component init
```

**Say (over the `init`):**

> "That last command wrote `CLAUDE.md`, `.cursorrules`, and Copilot
> instructions. Every coding agent I open in this repo now knows about
> 963 Dagster community components and how to search + compose them."

**Do:** switch to Claude Code (or Cursor), paste the reference prompt:

```
Build an AI maintainer triage room for GitHub issues on public repos.
Fetch a real issue + comments via the GitHub MCP server (`npx -y
@modelcontextprotocol/server-github`), fan out 4 LLM specialists
(repo/code, docs, reproduction, prior history), synthesize a triage
decision, have a skeptic critique it, draft a final report, gate the
report on a human sign-off via a JSON token file, and add a filesystem
sensor that auto-progresses the gate when a token drops. 100%
components + YAML, no Python. Use `dagster-component search` and
`dagster-component schema <id>` — no invented fields. Target issue:
#30000 in dagster-io/dagster. Don't run it — just compose the defs.yaml
files.
```

**Say (while Claude works, ~important nuance to call out):**

> "Notice what Claude Code is doing under the hood — for each component
> it picks, it's running `dagster-component add <id>`. That's the CLI
> the `init` step wired up. `add` doesn't just write the defs.yaml
> file; it also reads the component's `requirements.txt` and runs
> `uv add` for each pip dep automatically. No manual dep management,
> no `ModuleNotFoundError` surprises on first `dg check`."

**When Claude finishes:** run

```bash
uv run dagster definitions validate -m maintainer_triage.definitions
```

**Say:** "Passed. Zero to something in one prompt — pip deps installed as
a side effect of the component picks."

**If Claude Code composed defs.yaml without invoking `dagster-component add`
(some agents do this) — fall back to explicit installs:**

```bash
uv add "dagster-community-components" openai litellm mcp
```

**Say:** "One catch: if the assistant writes defs.yaml directly instead
of using `dagster-component add`, pip deps aren't auto-installed.
Point your assistant at the `dagster-component add` workflow — the CLI
is the source of truth for what a component needs."

## Scene 3 — run it (1 min)

**Do:**

```bash
uv run dagster asset materialize --select 'issue_facts' -m maintainer_triage.definitions
uv run dagster asset materialize \
  --select 'triage,skeptic,report' \
  -m maintainer_triage.definitions
```

**Say (over the fan-out run):**

> "Watch the specialists run. Repo context, docs context, reproduction
> analysis, prior history — four different context windows, four
> different reasoning passes. Then triage synthesizes. Skeptic
> critiques. Report drafts."

**Do:** open the draft report file and scroll it.

**Say:** "Real triage of a real GitHub issue. This one landed on
'needs-more-info' with three concrete asks for the reporter — those are
what a maintainer would actually send back."

## Scene 4 — the plan lives in your data graph (1 min)

**Do:** open `dg dev` and go to the asset graph view.

**Say:**

> "Every node you just saw is an asset key. `repo_context`,
> `docs_context`, `triage`, `report` — all first-class asset keys.
> That means I can point a dbt model at `report`. I can wire it into
> Insights alerting. I can materialize a downstream Snowflake task off
> it. The AI-agentic plan isn't in a sidecar workflow tab — it's IN
> the org's data graph, so the rest of the data platform composes with
> it the same way it composes with any other asset."

**Do (optional):** point at the human_approval_gate asset and drop a
token via CLI:

```bash
echo '{"approved":true,"approver":"me"}' > approvals/default.json
```

**Say:** "The gate's watching that directory. The sensor picks it up in
5 seconds and launches the ship job automatically. Human sign-off as a
first-class primitive — the check surfaces in the UI, in Insights, and
in any alerting pipeline. No bespoke wiring per approval."

## Scene 5 — close (30s)

**Say:**

> "So — coding-agent-authors-the-plan, one NL prompt, four minutes to a
> real materialized graph. Every specialist output is a queryable
> asset. Every LLM call is an observable materialization. The human
> sign-off is a check the platform already knows how to alert on. When
> a customer asks 'does Dagster do AI-agentic orchestration?', the
> honest answer is 'yes, and the plan lives in your data graph, not in
> a workflow tab.'
>
> The walkthrough with the setup script and the reference prompt is on
> the community registry site. Link in the description."

## What to have on standby

- A backup screenshot of the completed asset graph in case `dg dev`
  boots slowly on recording day
- The `investigation_draft.md` from an earlier run so you have a
  visible artifact if the live run stalls

## What NOT to say

- Don't compare pricing. The story is capability + shape + observability.
- Don't dwell on any specific friction point that got fixed in shipping.
- Don't demo the two-component (MCPToolPicker + AgenticPipeline)
  variant AND the single-YAML (`mcp_call` op) variant. Pick one for the
  Loom. Recommend the single-YAML variant since it visually lands the
  "one execution plan" beat.
