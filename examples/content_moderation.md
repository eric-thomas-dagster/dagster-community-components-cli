# Content moderation — rule-based + ML-based, side by side

**Validated end-to-end** — 30 synthetic user comments fan out through two
moderation components in parallel. RUN_SUCCESS in ~22 seconds.

```
user_comments (synthetic 30 rows: clean, spam, hateful, multilingual, PII)
       │
       ├── moderation_scorer  → rule-based: risk_score + sentiment + decision
       └── text_moderator     → OpenAI /moderations: hate / harassment / sexual / violence / self_harm
```

Both moderators read the **same upstream** so you can directly compare
rule-based and ML signals on identical input.

## Components used

| Component | Asset | What it does |
|---|---|---|
| `synthetic` source | `user_comments` | 30 synthetic comments — clean, spam, hateful, multilingual, PII-bearing |
| `moderation_scorer` | `moderation_scores` | Rule-based: keyword risk + sentiment + length signals → `risk_score`, `sentiment_score`, `moderation_decision` (approved / needs_review / flagged) |
| `text_moderator` | `moderated_comments` | OpenAI moderation endpoint policy categories: hate, harassment, sexual, violence, self_harm |

## Cost

**Free.** OpenAI's `/moderations` endpoint isn't billed against the
gpt-4o-mini quota, and the rule-based scorer is pure local Python.

## Required env vars

```bash
OPENAI_API_KEY=sk-...      # SDK still needs a key even for the free endpoint
```

## Run it

```bash
./setup_content_moderation_demo.sh
cd content-moderation-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev    # http://localhost:3000
```

## Why two moderators on the same data?

- **Rule-based** is instant and cheap, but misses subtle toxicity, can't
  read multilingual content, and over-flags spam-shaped clean text.
- **ML moderation** catches multilingual hate speech, evades simple
  keyword bypasses, and gives per-category probabilities — but is opaque
  and rate-limited.

Production deployments often run both: rule-based pre-filter for the
easy 90%, ML for the ambiguous 10%, escalate to humans when the two
disagree.
