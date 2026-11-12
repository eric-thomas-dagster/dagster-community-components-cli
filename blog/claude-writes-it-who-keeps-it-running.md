---
title: "The digest that outgrew its routine"
date: 2026-11-11
author: Eric Thomas
description: "A daily calendar digest is the perfect first project, and a Claude routine runs it fine. Then it grows: more sources, more readers, someone who depends on it. A walk through the four moments a routine starts to strain, what I'd move into a pipeline at each one, and what I'd leave alone."
---

# The digest that outgrew its routine

**Eric Thomas · November 2026**

*This is an independent community project, not an official Dagster Labs position — my own components, my own reading of Anthropic's docs, my own opinions. The story below is a composite of how I'd expect this to go, not one real team's account.*

---

Start with the most "vibe coder" project there is: read my calendar, summarize the day, send me the digest each morning.

Claude Code writes it in one sitting, and then you don't even have to host it. A Claude [routine](https://claude.com/blog/introducing-routines-in-claude-code) is a saved prompt plus the repositories and connectors it needs, and it runs on Anthropic's cloud on a schedule, from an API call, or on a GitHub event. Per [the docs](https://code.claude.com/docs/en/routines.md) it doesn't need your computer on, and your MCP connectors are included by default.

For one person's calendar, that's the right answer. Use the routine. I mean it.

The interesting question is what happens next, because digests don't stay small.

## Moment one: it stops being one source

Within a month the digest wants more than the calendar. Today's open pull requests. The tickets assigned to you. The three Slack threads you were mentioned in. The prompt grows a paragraph for each source, and every morning the model re-reads and re-assembles all of it from scratch.

Two things start to feel off. The output wobbles from day to day, because gathering the facts and writing about them are the same step, so a slightly different reading of the PR list changes the whole briefing. And when it's wrong you can't tell which part went wrong without reading the transcript.

**What I'd move:** the gathering. Each source becomes its own step with its own stored result, and the model's only job is to write about what's already been collected.

```yaml
# calendar_mcp/defs.yaml — register the server once
type: dagster_community_components.MCPServerResourceComponent
attributes:
  resource_key: calendar_mcp
  name: calendar
  type: stdio
  command: [uv, run, python, calendar_server.py]
```

```yaml
# todays_events/defs.yaml — one source = one asset
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: todays_events
  server_resource_key: calendar_mcp
  tool_name: list_events
  tool_args:
    range: today
```

That's the same MCP connector a routine would use. The difference is that the result is now a step you can look at, re-run on its own, and compare with yesterday's. A PR source, a tickets source and a Slack source are three more files like it, and the writing step depends on all of them.

I ran the first of these end to end while writing this post: the asset starts the server, calls `list_events`, and stores the result with the latency, size and a preview as metadata.

## Moment two: other people want one

A teammate sees it and wants their own. Then the team does. Now it's eight digests a morning, each with its own calendar and its own sources.

A routine is one prompt with one configuration, so eight readers means eight routines to keep in sync, and routines have run caps tied to your plan (reported as a handful to a couple of dozen a day at launch, when they were a research preview, so check what applies to you). A change to the format means editing eight prompts.

**What I'd move:** the structure. One pipeline, parameterized by person, so changing the briefing format is one edit. The gathering steps are deterministic code, so they cost nothing in model calls, and only the writing step spends tokens.

## Moment three: somebody relies on it

The first time the digest says a meeting is cancelled when it isn't, it stops being a toy. Someone asks: *what did it see, what did it write, and why?*

With a routine you can open that run's session and read what happened, and that's a real record. What you can't easily do is ask the same question across a month, run a test against it, or hold a bad one before it's sent.

**What I'd move:** the accountability. A check on the digest ("every event in the input appears in the output") that fails visibly instead of sending. A hold before it reaches anyone important. And a history per step, shared by the team and surviving anyone leaving, so "what did the calendar step return at 6:30 last Tuesday" has an answer that doesn't depend on whose account ran it.

This is the moment I'd point a skeptical boss at. It's less about the digest than about being able to show your work.

## Moment four: the digest feeds something else

Then the briefing starts to be an input. It goes into a weekly rollup. A dashboard shows whether it went out. A Slack bot quotes it. Now "run it at 6:30" isn't enough: the rollup must wait for the daily digests, the dashboard needs to know if today's is fresh, and a failed morning must be re-run without redoing the five that worked.

That's dependency and freshness tracking. It's a different shape of problem from "run this prompt at this time", and it's what asset-based orchestration is for.

## What I'd leave as a routine

Not all of it moves. The thing that's actually good at language, writing the three-sentence briefing from collected facts, is exactly what a routine or any model call is for. In the pipeline it's one asset:

```python
@dg.asset(group_name="digest")
def daily_digest(context, todays_events: str) -> str:
    response = litellm.completion(model=model, messages=[...])
    ...
```

And the two aren't exclusive. A routine can call a pipeline, and a pipeline can run an agent step. The split I'd aim for: **the model handles words, and everything around it is code you can test and re-run.**

## The honest summary

| Stage | Stay on a routine | Move to a pipeline |
|---|---|---|
| One person, one or two sources | ✅ | |
| Many sources, flaky output | | The gathering steps |
| Many readers, many digests | | The structure, parameterized |
| Somebody depends on it | | Checks, holds, shared history |
| Feeds other things | | Dependencies and freshness |

If you're at the top of that table, I'd stop reading and use the routine.

## Try the first step

The two files above are real. This is the small server they point at (it needs the `mcp` package, version 2 — on 1.x import `FastMCP` from `mcp.server.fastmcp` instead):

```python
# calendar_server.py
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("calendar")


@mcp.tool()
def list_events(range: str = "today") -> list[dict]:
    return [
        {"start": "09:30", "title": "Standup", "attendees": 6},
        {"start": "14:00", "title": "1:1 with Sam", "attendees": 2},
    ]


if __name__ == "__main__":
    mcp.run()
```

Then:

```bash
uvx create-dagster@latest project digest --uv-sync
cd digest
uv add dagster-community-components mcp
# put calendar_server.py in the project root and the two defs.yaml files under src/digest/defs/
uv run dg check defs
uv run dg launch --assets todays_events
```

Swap the stand-in server for a real calendar MCP server and you have the first moment's version of the pipeline.

If you have a digest that outgrew a routine, I'd like to hear which of the four moments came first.

*Components referenced above are in the [community registry](https://dagster-component-ui.vercel.app); each has an example YAML and README. Routines details are from [Anthropic's docs](https://code.claude.com/docs/en/routines.md) and [announcement](https://claude.com/blog/introducing-routines-in-claude-code).*
