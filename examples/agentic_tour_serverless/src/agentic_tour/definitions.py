"""agentic_tour — 4 agentic pipelines as ONE Dagster+ Serverless code location.

Every AgenticPipelineComponent op showcased on a realistic use case, bundled
as a single deployable code location. The whole thing is defined in this
one file — no YAML, no companion files, no /tmp/ paths. Deploy with:

    uvx --with pex --from dagster-cloud-cli dagster-cloud serverless \\
        deploy-python-executable . \\
        --location-name agentic-tour \\
        --module-name agentic_tour.definitions \\
        --python-version 3.12

Environment variable required at runtime (set in Dagster+ Serverless
location config): OPENAI_API_KEY.

The 4 pipelines:

  1. investment_memo  — `debate` op. Bull + bear + neutral analysts;
     arbitrator picks the moderate-risk portfolio recommendation.
     Partitioned across 3 tickers (NVDA / TSLA / META).

  2. support_triage   — `route` op. Router picks the best specialist
     (technical / billing / product / account) for each ticket.
     Partitioned across 5 realistic customer support ticket types.

  3. press_release    — `critique_loop` op. Drafter writes a launch
     press release; critic reviews; drafter revises. 2 iterations.
     Partitioned across 3 different product launches.

  4. framework_brief  — `synthesize` op. 4 parallel `llm_call` steps
     each analyze one aspect of a JS framework (dx / performance /
     ecosystem / community); synthesize merges them into one briefing.
     Partitioned across 3 frameworks (react / vue / svelte).

Total cost for a full backfill (14 partitions): ~$0.02 all-in on gpt-4o-mini.
"""
from typing import Any, Optional

import dagster as dg
from dagster_community_components import AgenticPipelineComponent


# ── helpers ──────────────────────────────────────────────────────────

class _MinimalLoadContext:
    """Stand-in for dg.ComponentLoadContext — AgenticPipelineComponent
    doesn't use anything from the context in build_defs, so a bare
    object suffices. Real Dagster+ Serverless deploys don't need YAML
    loading (we're bypassing the YAML component-loading path by
    instantiating the component in Python directly)."""


def _apply_static_partitions(
    defs: dg.Definitions,
    partition_keys: list[str],
) -> dg.Definitions:
    """Take a Definitions built by AgenticPipelineComponent.build_defs
    and stamp a StaticPartitionsDefinition onto every asset in it.

    Why we do this here (instead of via post_processing YAML): this
    file has no YAML — everything is Python. The `post_processing:`
    block only fires when Dagster loads a component from YAML.
    Instantiating the component in Python skips that layer, so we
    apply partitions to the built assets ourselves.
    """
    pd = dg.StaticPartitionsDefinition(partition_keys)

    def _apply(spec: dg.AssetSpec) -> dg.AssetSpec:
        return spec._replace(partitions_def=pd)

    new_assets: list[Any] = []
    for a in (defs.assets or []):
        if hasattr(a, "map_asset_specs"):
            new_assets.append(a.map_asset_specs(_apply))
        else:
            new_assets.append(a)
    return dg.Definitions(assets=new_assets)


def _build(component: AgenticPipelineComponent, partition_keys: Optional[list[str]] = None) -> dg.Definitions:
    d = component.build_defs(_MinimalLoadContext())  # type: ignore[arg-type]
    if partition_keys:
        d = _apply_static_partitions(d, partition_keys)
    return d


OPENAI_KEY = "OPENAI_API_KEY"
MODEL = "gpt-4o-mini"


# ── 1. debate ─ investment memo (bull / bear / neutral + arbitrator) ─

investment_memo = AgenticPipelineComponent(
    asset_name_prefix="investment_memo",
    group_name="investment_committee",
    kinds=["llm", "agent", "debate"],
    description="Investment committee memo — bull, bear, and neutral analysts propose; committee chair picks the moderate-risk recommendation.",
    source={
        "kind": "literal",
        "text": (
            "Investment committee memo request. Ticker: {partition_key}. "
            "Should the portfolio buy, hold, or sell {partition_key} at current prices? "
            "Consider recent performance, competitive position, and material risks. "
            "Give a concrete recommendation."
        ),
    },
    steps=[
        {
            "id": "recommendation",
            "op": "debate",
            "proposers": [
                {
                    "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "system_prompt": "You are a bull analyst. Argue for BUY. Cite growth vectors, competitive moats, and any recent positive catalysts. Be specific and quantitative. One paragraph.",
                    "temperature": 0.8, "max_tokens": 500,
                },
                {
                    "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "system_prompt": "You are a bear analyst. Argue for SELL or aggressive underweight. Cite valuation risk, competitive threats, and any material headwinds. Be specific and quantitative. One paragraph.",
                    "temperature": 0.8, "max_tokens": 500,
                },
                {
                    "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "system_prompt": "You are a neutral analyst. Argue for HOLD with a specific target price range. Present the balanced case — one bull argument, one bear argument, one wait-and-see catalyst. One paragraph.",
                    "temperature": 0.8, "max_tokens": 500,
                },
            ],
            "arbitrator": {
                "model": MODEL, "api_key_env_var": OPENAI_KEY,
                "system_prompt": "You are the portfolio committee chair. Pick the recommendation best suited for a moderate-risk, long-horizon institutional portfolio. Focus on risk-adjusted return, not maximum upside. Explain why in 1-2 sentences.",
            },
        }
    ],
    outputs={"assets": ["recommendation"]},
)


# ── 2. route ─ customer support triage ───────────────────────────────

# Static partitions = realistic ticket types. The literal source text uses
# {partition_key} as the ticket description keyword (kept short for the demo;
# in prod you'd source from your ticketing system via upstream_asset).
support_ticket_texts = {
    "login_issue": "I can't log into my account. Password reset emails aren't arriving. Been stuck for 2 hours, have a demo in 30 min.",
    "billing_question": "My invoice shows $499 but the pricing page says the Team plan is $299/mo. What's the extra $200 for?",
    "feature_request": "It would be great if you could add SSO with Okta. We need this for our compliance team before renewal.",
    "bug_report": "The CSV export button on the dashboard returns a 500 error when the report has >10k rows. Repro every time.",
    "general_inquiry": "Do you have a public roadmap? I want to know if X feature is planned before I commit to a 2-year contract.",
}

support_triage = AgenticPipelineComponent(
    asset_name_prefix="support_triage",
    group_name="customer_support",
    kinds=["llm", "agent", "route"],
    description="Customer support ticket triage — router picks the right specialist agent for each ticket type.",
    source={
        "kind": "literal",
        # We embed all 5 tickets as a Python-dict lookup by using {partition_key}
        # in a passthrough — the pipeline will fetch the text at compute time
        # via the source. Since the AgenticPipelineComponent doesn't support
        # partition_key → dict-lookup natively, we do it via a mapping step
        # (see steps below).
        # To keep it single-file, the source just embeds the mapping via a
        # switch prompt; the router sees the partition key + ticket text.
        "text": "See the mapping in the pipeline definition.",
    },
    steps=[
        # Step 1: transform the raw source into the actual ticket text for this partition.
        # We use an llm_call as a "text passthrough" — the LLM just echoes the
        # ticket text back so downstream steps see it. Cheap + universal.
        {
            "id": "ticket_text",
            "op": "llm_call",
            "model": MODEL,
            "api_key_env_var": OPENAI_KEY,
            "system_prompt": (
                "You are a text lookup service. Given a ticket_id below, return "
                "ONLY the exact ticket text — no other output. If the ticket_id "
                "doesn't match, return the literal text 'unknown ticket'.\n\n"
                "Ticket mapping (Python dict):\n" + str(support_ticket_texts)
            ),
            "prompt_template": "ticket_id: {partition_key} — output only the ticket text.",
            "max_tokens": 300,
        },
        {
            "id": "routed",
            "op": "route",
            "source": "ticket_text",
            "router": {"model": MODEL, "api_key_env_var": OPENAI_KEY},
            "specialists": [
                {
                    "name": "technical", "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "description": "Technical / engineering issues — login failures, bugs, API errors, integration problems, performance issues. NOT for pricing or account management.",
                    "system_prompt": "You are a senior support engineer. Diagnose the technical issue, ask for exactly one piece of info if truly needed, and propose the next step. Concise.",
                    "max_tokens": 400,
                },
                {
                    "name": "billing", "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "description": "Billing / invoicing / pricing / plan questions — invoice amounts, payment methods, subscription upgrades, discrepancies with public pricing.",
                    "system_prompt": "You are a billing specialist. Explain pricing clearly, cross-reference plan tiers, and offer to escalate to finance if the discrepancy is beyond documented pricing. Concise.",
                    "max_tokens": 400,
                },
                {
                    "name": "product", "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "description": "Feature requests / product roadmap / SSO / integrations — new capabilities the customer wants added, integrations with third-party tools, roadmap questions.",
                    "system_prompt": "You are a product manager. Acknowledge the request, note if it's already on the roadmap (say 'I need to check' if unknown — never fabricate), and outline what data you'd want from the customer to evaluate. Concise.",
                    "max_tokens": 400,
                },
                {
                    "name": "account", "model": MODEL, "api_key_env_var": OPENAI_KEY,
                    "description": "Account management / renewals / contracts / general inquiries — how to reach account manager, contract questions, roadmap-under-NDA requests, pre-renewal check-ins.",
                    "system_prompt": "You are an account manager. Route to the right team based on customer stage (POC / active / renewal), offer to schedule a call. Concise.",
                    "max_tokens": 400,
                },
            ],
            "fallback": "account",
        },
    ],
    outputs={"assets": ["routed"]},
)


# ── 3. critique_loop ─ press release polish ──────────────────────────

# 3 distinct launch scenarios; each partition drafts+critiques+revises 2×.
press_release_briefs = {
    "sso_launch": (
        "Draft a 3-paragraph launch press release announcing SSO with Okta + Azure AD + "
        "Google Workspace, targeted at enterprise IT buyers. Emphasize compliance "
        "(SOC 2 Type II, HIPAA), rollout time (<30 min), and zero downtime for existing users."
    ),
    "insights_launch": (
        "Draft a 3-paragraph launch press release for a new dashboard that automatically "
        "detects anomalies in cost + latency metrics for the customer's data pipelines. "
        "Targeted at platform + FinOps teams. Emphasize automation, savings from catching "
        "runaway costs early, and no-code alert configuration."
    ),
    "eu_region": (
        "Draft a 3-paragraph launch press release announcing EU region availability "
        "(Dublin) for the platform, targeted at European enterprise buyers. "
        "Emphasize GDPR compliance, data residency, latency under 20ms across "
        "European POPs, and the same feature set as the US region."
    ),
}

press_release = AgenticPipelineComponent(
    asset_name_prefix="press_release",
    group_name="marketing",
    kinds=["llm", "agent", "critique_loop"],
    description="Press release polish — drafter writes; senior editor critiques; drafter revises. 2 iterations. Full history in materialization metadata.",
    source={"kind": "literal", "text": "See the brief lookup in the pipeline definition."},
    steps=[
        # Same pattern as support_triage: use a passthrough llm_call to fetch
        # the per-partition brief text.
        {
            "id": "brief",
            "op": "llm_call",
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": (
                "You are a text lookup service. Return ONLY the exact brief text — no other output.\n\n"
                "Brief mapping (Python dict):\n" + str(press_release_briefs)
            ),
            "prompt_template": "launch_id: {partition_key} — output only the brief text.",
            "max_tokens": 400,
        },
        {
            "id": "polished",
            "op": "critique_loop",
            "source": "brief",
            "drafter": {
                "model": MODEL, "api_key_env_var": OPENAI_KEY,
                "system_prompt": "You are a senior technical writer. Write clear, punchy press releases. Every sentence pulls its weight. No filler.",
                "max_tokens": 600,
            },
            "critic": {
                "model": MODEL, "api_key_env_var": OPENAI_KEY,
                "system_prompt": "You are a demanding editor. Critique for clarity, specificity, and buyer-relevance. Be concrete — point at specific sentences to change. If the draft is already tight, say so and don't invent problems.",
                "max_tokens": 400,
            },
            "iterations": 2,
        },
    ],
    outputs={"assets": ["polished"]},
)


# ── 4. synthesize ─ framework comparison briefing ────────────────────

# 4 parallel angles fan into one synthesize. Shows the "compose multiple
# upstream analyses into one report" fan-in shape.
framework_brief = AgenticPipelineComponent(
    asset_name_prefix="framework_brief",
    group_name="platform_research",
    kinds=["llm", "agent", "synthesize"],
    description="JS framework comparison briefing — 4 parallel angles (DX / performance / ecosystem / community) synthesized into one report per framework.",
    source={
        "kind": "literal",
        "text": "You are researching the JavaScript framework {partition_key} for adoption on a new SaaS project.",
    },
    steps=[
        {
            "id": "dx",
            "op": "llm_call",
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": "You are a senior frontend engineer. Focus ONLY on developer experience: syntax ergonomics, TypeScript integration, tooling quality, hot-reload speed, learning curve. One tight paragraph.",
            "max_tokens": 400,
        },
        {
            "id": "performance",
            "op": "llm_call", "source": "source",
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": "You are a performance engineer. Focus ONLY on runtime + build-time performance: bundle size, first-paint, hydration cost, SSR / RSC support, benchmark results. One tight paragraph.",
            "max_tokens": 400,
        },
        {
            "id": "ecosystem",
            "op": "llm_call", "source": "source",
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": "You are a platform architect. Focus ONLY on ecosystem: available UI libraries, state management, form libraries, third-party integrations, meta-framework maturity (Next.js / Nuxt / SvelteKit). One tight paragraph.",
            "max_tokens": 400,
        },
        {
            "id": "community",
            "op": "llm_call", "source": "source",
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": "You are a hiring manager. Focus ONLY on community + hiring: developer availability, mindshare trend, quality of docs, active maintainers, corporate backing. One tight paragraph.",
            "max_tokens": 400,
        },
        {
            "id": "briefing",
            "op": "synthesize",
            "sources": ["dx", "performance", "ecosystem", "community"],
            "model": MODEL, "api_key_env_var": OPENAI_KEY,
            "system_prompt": (
                "You are the CTO synthesizing four angle-specific analyses of the "
                "framework into one adoption briefing. Structure: (1) 1-line "
                "recommendation, (2) top 3 reasons for, (3) top 2 concerns, "
                "(4) go/no-go verdict."
            ),
            "max_tokens": 700,
        },
    ],
    outputs={"assets": ["dx", "performance", "ecosystem", "community", "briefing"]},
)


# ── merge into ONE code location ─────────────────────────────────────

defs = dg.Definitions.merge(
    _build(investment_memo, partition_keys=["NVDA", "TSLA", "META"]),
    _build(support_triage,  partition_keys=list(support_ticket_texts.keys())),
    _build(press_release,   partition_keys=list(press_release_briefs.keys())),
    _build(framework_brief, partition_keys=["react", "vue", "svelte"]),
)
