# AWS CloudWatch metrics + Logs Insights

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

Counterpart to the Azure Log Analytics demo but for AWS. Two
sources to pull operational telemetry into Dagster pipelines for
analytics, anomaly detection, capacity planning.

## Components used

| Component | Category | Role |
|---|---|---|
| `aws_cloudwatch_metrics_query` | source | GetMetricData → DataFrame |
| `aws_cloudwatch_logs_insights_query` | source | Logs Insights query → DataFrame |

## Status

Code-validated. To run end-to-end:

```bash
# AWS credentials (any normal boto3 chain works — env vars, ~/.aws, IAM role, etc.)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
```

## Metrics — defs.yaml

```yaml
type: dagster_component_templates.AwsCloudwatchMetricsQueryComponent
attributes:
  asset_name: lambda_invocations_last_hour
  region: us-east-1
  namespace: AWS/Lambda
  metric_name: Invocations
  statistic: Sum
  period_seconds: 300
  range_minutes: 60
  dimensions:
    FunctionName: my-prod-function
  aws_access_key_id_env_var: AWS_ACCESS_KEY_ID
  aws_secret_access_key_env_var: AWS_SECRET_ACCESS_KEY
```

## Logs Insights — defs.yaml

```yaml
type: dagster_component_templates.AwsCloudwatchLogsInsightsQueryComponent
attributes:
  asset_name: error_logs_last_hour
  region: us-east-1
  log_group_names: ["/aws/lambda/my-prod-function"]
  range_minutes: 60
  query_string: |
    fields @timestamp, @message
    | filter @message like /ERROR/
    | sort @timestamp desc
    | limit 1000
```

## When to use vs CloudWatch SDK directly

The components are right when you want CloudWatch results as a
**Dagster asset** with materialization metadata, lineage, schedules, and
auto-materialize. For ad-hoc query in a custom op, use boto3 directly.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
