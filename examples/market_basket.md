# Market Basket demo

Generates 200 synthetic shopping baskets with realistic item co-occurrence,
runs apriori to find frequent itemsets + derive association rules
(support / confidence / lift), filters to high-lift rules, summarizes by
antecedent count, and writes the strong rules to CSV.

Pipeline (7 components, all autoloaded by `dg`):
  file_ingestion → market_basket_rules ─┬─→ filter (lift > 1.5)  → CSV (strong rules)
                                             │
                                             └─→ summarize (top antecedents) → CSV

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `market_basket_rules` | analytics | Apriori association rules |
| 3 | `filter` | transformation | Row filter by predicate |
| 4 | `summarize` | transformation | Group-by aggregate |
| 5 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_market_basket_demo.sh | bash
cd market-basket-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
