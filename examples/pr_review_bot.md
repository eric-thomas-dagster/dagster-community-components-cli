# PR Review Bot (mcp_call + delegate + synthesize)

**Fetches a REAL pull request's diff from GitHub's public API, then three reviewer-persona agents each review it from their own lens, synthesized into one polished comment.**

**Setup script:** [`setup_pr_review_bot_demo.sh`](./setup_pr_review_bot_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card`, writes three reviewer agents + a 5-step pipeline. `bash setup_pr_review_bot_demo.sh` and `uv run dg dev`.

## What the demo shows

1. **`fetch_diff`** (`op: mcp_call`) — the first `mcp_call` showcase in this set: a direct, deterministic (no LLM) MCP tool call against a real server that hits GitHub's actual public REST API. No token needed — public repos only.
2. **`security_review` / `style_review` / `test_review`** (`op: delegate`, 3x) — each `required_capabilities`-forces a SPECIFIC registered reviewer (no semantic ambiguity needed — the step already knows which lens it wants). A different flavor of `delegate` than [Route to a Specialist](route_to_specialist.md)'s semantic picking: here, tag-based forcing is the right tool because there's no judgment call to make about *which* reviewer, only about *what they find*.
3. **`final_review`** (`op: synthesize`) — fans all three independent reviews into one comment, one section per lens.

Total cost per run: **~$0.002** (`gpt-4o-mini`, ~6 LLM calls).

## Live-validated output

Default PR: [`dagster-io/dagster#31999`](https://github.com/dagster-io/dagster/pull/31999) — a real, small (3 files, 25 lines), genuinely benign change replacing `distutils.spawn` with `shutil.which`. Picked deliberately: a good test of whether the reviewers invent issues where none exist, while still catching the real, legitimate gap.

**Security reviewer** correctly found nothing concerning and said so explicitly:
> *"No Injected Secrets... Safe Subprocess Usage: replaces distutils.spawn with shutil.which, which is safer... Overall, these modifications are primarily refactoring... making them low-risk."*

**Test coverage reviewer** correctly flagged the real gap — no new tests for the behavior change:
> *"...it would be prudent to ensure that the change is covered by tests that validate the behavior of finding executables in the environment... Ensure that these integration points are covered in the test suite."*

**Style reviewer** gave real, specific, grounded feedback referencing the actual renamed function and actual exception type used in the real diff.

All three were run for real against the actual PR content — none of this is canned.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
