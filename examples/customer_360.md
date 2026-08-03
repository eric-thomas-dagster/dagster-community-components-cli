# Customer 360 (natural-language build)

Per-customer aggregate view — orders rolled up to `total_spent` / `order_count` / `avg_order_value` and joined back to the customer profile. Built entirely from a natural-language task via [`planned_catalog_agent`](./planned_catalog_agent.md), no hand-authored per-component defs.yaml.

## Pipeline

```
synthetic_data_generator (customers, 300)
synthetic_data_generator (orders, 1500)
       │
       ├─→ summarize (by customer_id: total_spent, order_count, avg_order_value)
       │       │
       │       └─→ dataframe_join (per-customer metrics ⋈ customer profile)
       │              │
       │              └─→ select_columns → sort → dataframe_to_csv
```

## The task

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build a Customer 360 view:
      1. Generate 300 synthetic customers (schema_type: customers).
      2. Generate 1500 synthetic orders (schema_type: orders).
      3. Group orders by customer_id, computing:
           total_spent = sum of total
           order_count = count of orders
           avg_order_value = mean of total
      4. Join the per-customer metrics back to the customer profile on customer_id.
      5. Sort customers by total_spent descending.
      6. Select these columns: [customer_id, first_name, last_name, email, city,
         state, lifetime_value, total_spent, order_count, avg_order_value].
      7. Write to /tmp/customer_360.csv.
  include_ids: [synthetic_data_generator]
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 15
  defs_state: { management_type: LOCAL_FILESYSTEM, refresh_if_dev: false }
```

## Validated run (2026-07-08)

- **7/7 clean picks in 13.3s, ~$0.0052 total cost** on gpt-4o-mini.
- 234 real customers written to `/tmp/customer_360.csv`.

Sample output:

```
customer_id,first_name,last_name,email,city,state,lifetime_value,total_spent,order_count,avg_order_value
CUST000072,Sarah,Johnson,sarah.johnson519@example.com,San Antonio,TX,1144.83,4134.11,5,826.82
CUST000224,Olivia,Davis,olivia.davis272@example.com,New York,NY,4856.44,3489.91,5,697.98
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_customer_360_demo.sh \
  -o setup_customer_360_demo.sh
bash setup_customer_360_demo.sh
```

## See also

- [Data Combination](data_combination.md) — smaller version of the same join-and-aggregate pattern
- [Kitchen Sink](kitchen_sink.md) — the full breadth demo (21 components in one graph)
- [RFM Segmentation](rfm_segmentation.md) — a complementary per-customer view via the RFM component
