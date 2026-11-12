---
title: "For a daily digest, use a Claude routine. Here's where that stops."
date: 2026-11-11
author: Eric Thomas
description: "A calendar digest is the perfect vibe-coding project, and Claude's own routines will run it in the cloud with your laptop closed. So when does a pipeline need an orchestrator? An honest look at where a routine is enough, where it stops, and which community components cover the other side."
---

# For a daily digest, use a Claude routine. Here's where that stops.

**Eric Thomas · November 2026**

*This is an independent community project, not an official Dagster Labs position — my own components, my own reading of Anthropic's docs, my own opinions.*

*Update: an earlier version of this post claimed a personal user needs an orchestrator just to keep a script running. That undersold Claude's routines. This is the corrected version.*

---

Take the most "vibe coder" project there is: read my calendar, summarize the day, send me the digest each morning.

Claude Code will write it in one sitting. And you may not need to run it anywhere else, because Claude [now runs it for you](https://claude.com/blog/introducing-routines-in-claude-code). A **routine** is a saved prompt, plus the repositories and connectors it needs, that runs on Anthropic's cloud on a schedule, from an API call, or in response to a GitHub event. Per [Anthropic's docs](https://code.claude.com/docs/en/routines.md), it doesn't need your computer on. Your MCP connectors (Calendar, Gmail, Slack, Linear and so on) are included by default, so the "authorize once" part is handled too. Each run is kept as a full session you can open and read.

For a calendar digest, that's the answer. Use a routine. If that's your project, close this tab.

So when does an orchestrator earn its place? I think the dividing line is where the work stops being *one prompt with some tools* and becomes *a pipeline with parts*.

## Where a routine is the right tool

A routine is an agent with a prompt. It's at its best when:

- The job is naturally language-shaped: summarize, triage, draft, review.
- The inputs are a handful of connectors, not a data volume.
- One run is one coherent piece of reasoning, and reading the transcript is how you check it.
- It's yours, or your small team's.

Digest, inbox triage, weekly PR roundups, reminders: that's the territory. I'd reach for it first.

## Where it stops

These are the places I'd expect a routine to feel wrong, not failures of Anthropic's product, just a different shape of problem.

**The job is data, not conversation.** Pulling a few hundred thousand rows, joining two sources, writing a table: you don't want a model reasoning through each step. You want code you can test, with the model used for the one step that needs it. In a pipeline, the LLM call is *an asset*, and the rest is plain Python or SQL.

**Steps have dependencies.** "Run B only after A finished, and only if A's data is fresh." A routine fires on a schedule, an API call or an event. A pipeline asks what's stale and runs just that, then re-runs one failed step without redoing the rest.

**You need per-step history, not per-run history.** A routine's record is a session transcript. For a pipeline you want to ask "what did the `todays_events` step return last Tuesday, and how did it differ from Wednesday?" That's asset metadata and materialization history, not a conversation.

**Volume.** Routines have run caps tied to your plan; at launch they were reported as a handful to a couple of dozen runs a day depending on tier, and they draw on your subscription usage. Fine for a digest. Not for something that should run every few minutes or per record.

**A team needs to own it.** Someone else needs to see what ran, get paged, and change it without asking whose session it was.

**You want it to be dull and deterministic.** Everything above comes down to one preference: put the model only where a model helps, and keep the rest boring.

## What the community components cover on the other side

The registry has ordinary components for the pipeline version of the digest. They're YAML, and each step becomes an asset with its own history.

**An MCP server registered once**, with headers read from environment variables so no secret sits in a file:

```yaml
type: dagster_community_components.MCPServerResourceComponent
attributes:
  resource_key: calendar_mcp
  name: calendar
  type: http
  url: https://mcp.example.com/mcp/
  headers_env:
    Authorization: CALENDAR_TOKEN
```

**A tool call that's a real asset.** `MCPToolCallComponent` points at that server with `server_resource_key`, names the tool, and each run calls it and stores what came back, with latency, response size and a preview as metadata:

```yaml
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: todays_events
  server_resource_key: calendar_mcp
  tool_name: list_events
  tool_args:
    range: today
```

That's the same connector a routine would use, but the result is now a step in a graph: you can look at it, diff it against yesterday, and put a model call or a SQL transform downstream.

**An API without a connector.** `openapi_tool_server` turns an OpenAPI spec into one asset per operation.

**Letting an agent act within limits.** `dagster_workflow_tools` lets an agent materialize assets or run jobs as real, tagged Dagster runs against a hard allow-list, so it can do the three things you named and nothing else.

**Not redoing work.** `sitemap_crawler_ingestion` has a `skip_unchanged` mode, and the context-engineering pipeline reuses embeddings for chunks that didn't change. Don't recompute what didn't change.

## They're not opposed

I don't think this is routine versus orchestrator. A routine can *call* a pipeline, and a pipeline can run an agent step. The honest shape is:

- Start with a routine. It's the fastest path from an idea to something running.
- When the job grows data, dependencies, or an owner other than you, move the data parts into a pipeline and leave the language parts as agent steps.
- Keep the connectors the same. MCP is the shared layer, which is why a registered server and a tool-call asset look so much like what a routine already uses.

## What I got wrong the first time

My first draft listed running when the laptop is closed, handling credentials once, and keeping history as the reasons a personal user would need an orchestrator. A routine covers the first two outright and gives you a version of the third. What it doesn't give you is the pipeline shape: per-step assets, dependencies, deterministic steps, and no dependence on a plan's run cap. That, not the schedule, is the real argument.

If you have a digest that outgrew a routine, I'd like to hear what broke first.

*Components referenced above are in the [community registry](https://dagster-component-ui.vercel.app); each has an example YAML and README. Routines details are from [Anthropic's docs](https://code.claude.com/docs/en/routines.md) and [announcement](https://claude.com/blog/introducing-routines-in-claude-code); routines were a research preview when I read them, so check current limits.*
