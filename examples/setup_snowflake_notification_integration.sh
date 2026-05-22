#!/usr/bin/env bash
# Set up a Snowflake NOTIFICATION INTEGRATION (AWS SNS) for Snowpipe AUTO_INGEST.
#
# Mirrors the trust dance in setup_snowflake_iceberg_volume.sh:
#   1. Create an AWS SNS topic + IAM role with sns:Subscribe permission
#   2. Print Snowsight SQL to create the NOTIFICATION INTEGRATION
#   3. Wait for you to run that + paste back Snowflake's SNS principal ARN
#   4. Update the IAM role trust + SNS topic policy with that principal
#
# After this script + creating an S3 event notification on the bucket that
# publishes to the SNS topic, the workspace demo's capability scan flips
# `Snowpipe AUTO_INGEST` from ❌ to ✅.
#
# AWS-only for now. GCP (Pub/Sub) and Azure (Event Grid) have analogous
# patterns — file follow-up issues if you need them.

set -eo pipefail

if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════
  This script is interactive — it can't run via `curl | bash`.

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_notification_integration.sh -o setup_snowflake_notification_integration.sh
    chmod +x setup_snowflake_notification_integration.sh
    ./setup_snowflake_notification_integration.sh
════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  exit 1
fi

echo "═══════════════════════════════════════════════════════════════════════"
echo "  Snowflake NOTIFICATION INTEGRATION setup (AWS SNS)"
echo "═══════════════════════════════════════════════════════════════════════"
echo
echo "Creates the cloud-side SNS topic + IAM role so Snowflake can subscribe"
echo "to S3 PUT events for Snowpipe AUTO_INGEST."

if ! command -v aws >/dev/null 2>&1; then
  echo "  ⚠ aws CLI required. Install: 'brew install awscli' then 'aws configure'."
  exit 1
fi

if ! aws sts get-caller-identity --no-paginate >/dev/null 2>&1; then
  echo "  ⚠ aws CLI not authenticated. Run 'aws configure' first."
  exit 1
fi

AWS_ACCT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
SUFFIX=$(date +%s | tail -c 5)

echo
read -r -p "AWS region [$AWS_REGION]: " R; AWS_REGION="${R:-$AWS_REGION}"
read -r -p "Integration name in Snowflake [DAGSTER_DEMO_NOTIF]: " INT_NAME
INT_NAME="${INT_NAME:-DAGSTER_DEMO_NOTIF}"
read -r -p "SNS topic name [dagster-snowpipe-events-$SUFFIX]: " TOPIC
TOPIC="${TOPIC:-dagster-snowpipe-events-$SUFFIX}"
read -r -p "IAM role name [snowflake-notif-role-$SUFFIX]: " ROLE
ROLE="${ROLE:-snowflake-notif-role-$SUFFIX}"

# ── 1. Create the SNS topic ────────────────────────────────────────────
echo
echo ">>> Creating SNS topic $TOPIC in $AWS_REGION ..."
TOPIC_ARN=$(aws sns create-topic --name "$TOPIC" --region "$AWS_REGION" \
  --query TopicArn --output text)
echo "  ✓ SNS topic ARN: $TOPIC_ARN"

# ── 2. Create the IAM role (placeholder trust) ─────────────────────────
TRUST_FILE=$(mktemp -t sf_notif_trust.XXXX).json
POLICY_FILE=$(mktemp -t sf_notif_policy.XXXX).json
cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${AWS_ACCT}:root"},
    "Action": "sts:AssumeRole"
  }]
}
EOF
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["sns:Subscribe", "sns:Unsubscribe", "sns:ListSubscriptionsByTopic"],
     "Resource": "$TOPIC_ARN"}
  ]
}
EOF

echo ">>> Creating IAM role $ROLE ..."
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" \
    --policy-document file://"$TRUST_FILE" >/dev/null
  echo "  (role existed — trust policy updated in place)"
else
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document file://"$TRUST_FILE" >/dev/null
  echo "  ✓ Created."
fi
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name sns-subscribe-access \
  --policy-document file://"$POLICY_FILE" >/dev/null
ROLE_ARN="arn:aws:iam::${AWS_ACCT}:role/${ROLE}"
echo "  ✓ Role ARN: $ROLE_ARN"

# ── 3. Print Snowsight SQL ─────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Run this in Snowsight:"
echo "═══════════════════════════════════════════════════════════════════════"
cat <<SQL_BLOCK

USE ROLE ACCOUNTADMIN;

CREATE OR REPLACE NOTIFICATION INTEGRATION ${INT_NAME}
  TYPE = QUEUE
  ENABLED = TRUE
  NOTIFICATION_PROVIDER = AWS_SNS
  AWS_SNS_TOPIC_ARN = '${TOPIC_ARN}'
  AWS_SNS_ROLE_ARN  = '${ROLE_ARN}'
  DIRECTION = INBOUND;

-- Reveal Snowflake's SNS principal (you'll paste it back below):
DESC NOTIFICATION INTEGRATION ${INT_NAME};
-- Find SF_AWS_IAM_USER_ARN in the output and copy its value.

SQL_BLOCK
echo "═══════════════════════════════════════════════════════════════════════"
echo
read -r -p "SF_AWS_IAM_USER_ARN: " SF_PRINCIPAL
[ -z "$SF_PRINCIPAL" ] && { echo "  ⚠ No principal — aborting trust wiring."; exit 1; }

# ── 4. Update IAM role trust policy with Snowflake's principal ─────────
echo ">>> Updating $ROLE trust policy to allow $SF_PRINCIPAL ..."
cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "$SF_PRINCIPAL"},
    "Action": "sts:AssumeRole"
  }]
}
EOF
aws iam update-assume-role-policy --role-name "$ROLE" \
  --policy-document file://"$TRUST_FILE" >/dev/null
echo "  ✓ Trust updated."

# ── 5. Update SNS topic policy: allow the Snowflake principal Subscribe
echo ">>> Adding Subscribe permission to SNS topic policy ..."
TOPIC_POLICY=$(mktemp -t sf_notif_topic_pol.XXXX).json
cat > "$TOPIC_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowSnowflakeSubscribe",
    "Effect": "Allow",
    "Principal": {"AWS": "$SF_PRINCIPAL"},
    "Action": ["sns:Subscribe", "sns:Receive"],
    "Resource": "$TOPIC_ARN"
  }, {
    "Sid": "AllowAccountOwner",
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${AWS_ACCT}:root"},
    "Action": "SNS:*",
    "Resource": "$TOPIC_ARN"
  }]
}
EOF
aws sns set-topic-attributes --topic-arn "$TOPIC_ARN" \
  --attribute-name Policy --attribute-value "$(cat "$TOPIC_POLICY")" \
  --region "$AWS_REGION"
echo "  ✓ SNS topic policy updated."

echo
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Done. Next steps:"
echo "═══════════════════════════════════════════════════════════════════════"
echo
echo "1. Configure S3 → SNS event notification on your data bucket:"
echo "      aws s3api put-bucket-notification-configuration \\"
echo "        --bucket <YOUR_DATA_BUCKET> \\"
echo "        --notification-configuration '{\"TopicConfigurations\":[{\"TopicArn\":\"$TOPIC_ARN\",\"Events\":[\"s3:ObjectCreated:*\"]}]}'"
echo
echo "2. Create your AUTO_INGEST snowpipe in Snowsight:"
echo "      CREATE OR REPLACE PIPE my_pipe"
echo "        AUTO_INGEST = TRUE"
echo "        INTEGRATION = '$INT_NAME'"
echo "      AS COPY INTO my_table FROM @my_stage"
echo "         FILE_FORMAT = (TYPE = CSV);"
echo
echo "3. Re-run the workspace demo — capability scan now flips Snowpipe"
echo "   AUTO_INGEST from ❌ to ✅, and the snowflake_snowpipe_load_sensor"
echo "   add-on will offer to scaffold."
echo
echo "Resources created:"
echo "  • SNS topic:   $TOPIC_ARN"
echo "  • IAM role:    $ROLE_ARN"
echo "  • Snowflake integration: $INT_NAME"
