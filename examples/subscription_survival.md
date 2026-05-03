# Subscription survival demo


Generates 300 synthetic subscriptions (free/pro/enterprise tiers with
realistic churn rates) via synthetic_data_generator's `subscriptions`
schema, then fits Kaplan-Meier survival curves grouped by plan_tier.

Pipeline (3 components, all autoloaded by `dg`):
    synthetic_data_generator → survival_analysis → dataframe_to_csv

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_subscription_survival_demo.sh | bash
cd subscription-survival-demo
uv run dg launch --assets '*'
```
