# Typed named inputs — the "any op joins from any prior op by port name" pattern

`AgenticPipelineComponent` v2 adds an `inputs:` field on every step. Any
step can pull from any prior step by named port, and each named port
becomes a `{port_name}` placeholder in the step's `prompt_template` +
`system_prompt` (and, for `mcp_call`, in string `tool_args`).

That's the **typed-I/O execution-plan shape** — the standard "node cards
with labeled input ports, edges wired by port name" primitive that
agentic-workflow tools converge on. Dagster does it in one YAML, where
each port output is a first-class asset key.

## The primitive

```yaml
- id: join_step
  op: llm_call   # (or synthesize, or mcp_call)
  inputs:
    <port_name>: {from: <step_id>}            # read prior step's text output
    <port_name>: {literal: <value>}           # inline literal value
  prompt_template: |
    Each named port becomes a placeholder — {port_name}
```

Backward compat: `source: <id>` and `sources: [<ids>]` still work. If
both are present, `inputs:` takes precedence for named substitution;
`source:` continues to feed the legacy `{text}` placeholder.

## Toy example — 3-way join

Two independent research paths, joined by a synthesizer that reads each
by name plus a static policy literal:

```
                            source (literal: "explain attention")
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
       ┌─────────────┐                         ┌─────────────┐
       │ technical   │                         │ intuitive   │
       │ (llm_call)  │                         │ (llm_call)  │
       └──────┬──────┘                         └──────┬──────┘
              │                                       │
              │                                       │
              └───────────────────┬───────────────────┘
                                  ▼
              ┌────────────────────────────────────────┐
              │  synthesis  (synthesize)               │
              │    inputs:                             │
              │      technical:  {from: technical}     │
              │      intuitive:  {from: intuitive}     │
              │      audience:   {literal: "engineers"}│
              │    prompt_template: |                  │
              │      TECHNICAL: {technical}            │
              │      INTUITIVE: {intuitive}            │
              │      AUDIENCE:  {audience}             │
              └────────────────────────────────────────┘
                                  │
                                  ▼
                            synthesis (asset)
```

```yaml
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: research
  source:
    kind: literal
    text: "Explain transformer attention"
  steps:
    - id: technical
      op: llm_call
      source: source          # fan out from the initial source
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "You are a senior ML engineer. Precise, formula-friendly."
      max_tokens: 300

    - id: intuitive
      op: llm_call
      source: source          # fan out from the initial source
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "You are a science communicator. Analogies over formulas."
      max_tokens: 300

    - id: synthesis
      op: synthesize
      inputs:                 # typed multi-input join
        technical: {from: technical}
        intuitive: {from: intuitive}
        audience:  {literal: "senior engineers who prefer precision"}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        Merge the two explanations for the audience below.
      prompt_template: |
        TECHNICAL
        =========
        {technical}

        INTUITIVE
        =========
        {intuitive}

        AUDIENCE
        ========
        {audience}
  outputs:
    assets: [technical, intuitive, synthesis]
```

## Why this shape matters

- **One primitive, any join topology.** No special "synthesize" op
  required — every op that emits text (`llm_call`, `synthesize`,
  `mcp_call`) supports `inputs:`. Fan-out → typed-join → fan-out →
  typed-join, arbitrary depth.
- **Every port is an asset key.** `technical`, `intuitive`, `synthesis`
  above are all first-class Dagster assets. Downstream dbt models,
  warehouse writers, or notification sinks can depend on any specific
  port, not just the final one.
- **`{literal}` inputs = plan-level parameters.** The `audience`
  literal above is a compile-time value baked into the plan. Same
  primitive covers a "PLAN INPUTS" pattern — declare the parameters
  once as literals, reference them by name from every step that needs
  them.
- **Explicit edges = readable graphs.** Every input line names its
  source. No "reads whatever was last." No debugging by log-grepping to
  figure out which specialist fed the final synthesizer. The YAML is
  the diagram.

## Full end-to-end demo

The **[Maintainer Investigation Room](maintainer_investigation_room.md)**
walkthrough is the canonical typed-inputs example — a 9-node execution
plan that fetches a real GitHub issue via MCP, fans out 4 typed-input
specialists, joins to a preliminary triage, runs a skeptic, joins to a
final decision, and drafts a maintainer-facing report. Every join uses
`inputs: {port: {from: id}}`. `bash setup_maintainer_investigation_room_demo.sh`
scaffolds and runs the whole thing end-to-end against a real
dagster-io/dagster issue.

## Feature matrix — which ops support `inputs:`

| Op | `inputs:` supported | Notes |
|---|---|---|
| `llm_call` | ✅ | `{port_name}` substitutes in `system_prompt` + `prompt_template`. |
| `synthesize` | ✅ | Preferred over `sources:` for new pipelines — named join is clearer. |
| `mcp_call` | ✅ | `{port_name}` also substitutes in string `tool_args` values. |
| `route` | ❌ | Structured sub-config already — use `inputs:` on the *downstream* step that consumes the router's output. |
| `debate` | ❌ | Same — route/debate/critique_loop are multi-agent internals. |
| `critique_loop` | ❌ | Same. |

## Related patterns

- **[Maintainer Investigation Room](maintainer_investigation_room.md)** —
  the full 9-node execution-plan demo built on this primitive.
- **[Agentic Pipeline](agentic_pipeline.md)** — the classic
  `source: <id>` chain shape, still supported. Use when your pipeline
  is a linear chain rather than a fan-out/fan-in shape.
- **[Agentic Orchestration](agentic_orchestration.md)** — same
  agents-humans-legacy-systems story with `llm_prompt_executor` per
  agent instead of one pipeline component. Simpler when each agent
  needs its own asset boundary.
- **[Iterative Supervisor Agent](iterative_supervisor_agent.md)** —
  ReAct loop as N Dagster assets. Complement to typed inputs: use when
  the plan's shape is *dynamic* (planner picks next step at runtime),
  not statically declared.
