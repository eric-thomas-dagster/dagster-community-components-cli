#!/usr/bin/env bash
# Set up a Snowflake EXTERNAL VOLUME backed by cloud-storage, so the
# snowflake_iceberg_table component can materialize live.
#
# Auto-detects which cloud CLI is available (aws / gcloud / az) and walks
# through the full setup:
#   1. Create the bucket / container
#   2. Create the role / service account / managed identity with bucket access
#   3. Print the Snowsight SQL to create the EXTERNAL VOLUME
#   4. Wait for you to run that SQL + paste back Snowflake's STORAGE principal
#   5. Finish wiring trust with that principal
#
# Idempotent: re-running with the same bucket name reuses existing resources
# and just re-prints the SQL + re-applies the trust policy.

set -eo pipefail

if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════
  This script is interactive — it can't run via `curl | bash`.

  Download first, then run from a terminal:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_iceberg_volume.sh -o setup_snowflake_iceberg_volume.sh
    chmod +x setup_snowflake_iceberg_volume.sh
    ./setup_snowflake_iceberg_volume.sh
════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  exit 1
fi

echo "═══════════════════════════════════════════════════════════════════════"
echo "  Snowflake Iceberg EXTERNAL VOLUME setup"
echo "═══════════════════════════════════════════════════════════════════════"
echo "Creates the cloud-side bucket + IAM trust so Snowflake can read+write"
echo "Iceberg tables. Walks through both sides of the trust dance (cloud"
echo "issues role/SA, Snowflake hands you a principal, you allow it back)."
echo

# ── Detect which cloud CLI is available ────────────────────────────────
HAS_AWS=$(command -v aws    >/dev/null 2>&1 && echo "yes" || echo "no")
HAS_GCP=$(command -v gcloud >/dev/null 2>&1 && echo "yes" || echo "no")
HAS_AZ=$( command -v az     >/dev/null 2>&1 && echo "yes" || echo "no")

echo "Detected CLIs:  aws=$HAS_AWS  gcloud=$HAS_GCP  az=$HAS_AZ"
echo

# Pick which cloud to use. If only one CLI is available, default to it.
# If none, offer to install AWS CLI (easiest path on macOS).
DEFAULT_CLOUD=""
if   [ "$HAS_AWS" = "yes" ]; then DEFAULT_CLOUD="aws"
elif [ "$HAS_GCP" = "yes" ]; then DEFAULT_CLOUD="gcp"
elif [ "$HAS_AZ"  = "yes" ]; then DEFAULT_CLOUD="azure"
fi

if [ -z "$DEFAULT_CLOUD" ]; then
  echo "No cloud CLI found. AWS is the easiest path — install AWS CLI?"
  echo "  (macOS: 'brew install awscli'  /  Linux: curl-installer from AWS docs)"
  read -r -p "Install AWS CLI via Homebrew now? [Y/n] " ans
  case "${ans:-y}" in
    y|Y|yes)
      if ! command -v brew >/dev/null 2>&1; then
        echo "  ⚠ Homebrew not found. Install from https://brew.sh and re-run."
        exit 1
      fi
      brew install awscli
      ;;
    *)
      echo "  Install one of aws / gcloud / az and re-run. Exiting."
      exit 1
      ;;
  esac
  HAS_AWS=$(command -v aws >/dev/null 2>&1 && echo "yes" || echo "no")
  DEFAULT_CLOUD="aws"
fi

echo "Cloud provider for the external volume:"
echo "  [1] AWS S3       $([ "$HAS_AWS" = "yes" ] && echo "(aws CLI ready)" || echo "(aws CLI MISSING)")"
echo "  [2] GCP GCS      $([ "$HAS_GCP" = "yes" ] && echo "(gcloud CLI ready)" || echo "(gcloud CLI MISSING)")"
echo "  [3] Azure Blob   $([ "$HAS_AZ"  = "yes" ] && echo "(az CLI ready)"     || echo "(az CLI MISSING)")"
DEFAULT_CHOICE=$([ "$DEFAULT_CLOUD" = "aws" ] && echo 1 || ([ "$DEFAULT_CLOUD" = "gcp" ] && echo 2 || echo 3))
read -r -p "Choice [$DEFAULT_CHOICE]: " CLOUD_CHOICE
CLOUD_CHOICE="${CLOUD_CHOICE:-$DEFAULT_CHOICE}"

case "$CLOUD_CHOICE" in
  1|aws|AWS)     CLOUD="aws"; [ "$HAS_AWS" = "yes" ] || { echo "aws CLI not found."; exit 1; } ;;
  2|gcp|GCP)     CLOUD="gcp"; [ "$HAS_GCP" = "yes" ] || { echo "gcloud CLI not found."; exit 1; } ;;
  3|az|azure)    CLOUD="azure"; [ "$HAS_AZ"  = "yes" ] || { echo "az CLI not found."; exit 1; } ;;
  *) echo "Invalid choice."; exit 1 ;;
esac

# ── Pre-flight: confirm CLI is authenticated ───────────────────────────
echo
echo ">>> Pre-flight: confirming you're authenticated to $CLOUD ..."
case "$CLOUD" in
  aws)
    if ! aws sts get-caller-identity --no-paginate >/dev/null 2>&1; then
      echo "  ⚠ aws CLI not authenticated. Run 'aws configure' (paste your access"
      echo "    key + secret + region) or 'aws sso login' then re-run this script."
      exit 1
    fi
    AWS_IDENTITY=$(aws sts get-caller-identity --query 'Arn' --output text)
    echo "  ✓ Authenticated as: $AWS_IDENTITY"
    ;;
  gcp)
    if ! gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null | grep -q .; then
      echo "  ⚠ gcloud CLI not authenticated. Run 'gcloud auth login' and re-run."
      exit 1
    fi
    GCP_PROJECT=$(gcloud config get-value project 2>/dev/null)
    echo "  ✓ Authenticated; default project: ${GCP_PROJECT:-NOT SET}"
    ;;
  azure)
    if ! az account show >/dev/null 2>&1; then
      echo "  ⚠ az CLI not authenticated. Run 'az login' and re-run."
      exit 1
    fi
    AZ_SUB=$(az account show --query 'name' -o tsv)
    echo "  ✓ Authenticated; subscription: $AZ_SUB"
    ;;
esac

# ── Common params ──────────────────────────────────────────────────────
echo
read -r -p "Volume name in Snowflake [DAGSTER_DEMO_VOLUME]: " VOLUME_NAME
VOLUME_NAME="${VOLUME_NAME:-DAGSTER_DEMO_VOLUME}"

EXTERNAL_ID="$(echo "$VOLUME_NAME" | tr '[:upper:]' '[:lower:]')-external-id"
SUFFIX=$(date +%s | tail -c 5)

# ===========================================================================
# AWS PATH — S3 bucket + IAM role
# ===========================================================================
if [ "$CLOUD" = "aws" ]; then
  AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
  read -r -p "AWS region [$AWS_REGION]: " REGION_IN
  AWS_REGION="${REGION_IN:-$AWS_REGION}"

  DEFAULT_BUCKET="dagster-iceberg-$(whoami | tr -d ' ' | tr '[:upper:]' '[:lower:]')-$SUFFIX"
  read -r -p "S3 bucket name [$DEFAULT_BUCKET]: " BUCKET
  BUCKET="${BUCKET:-$DEFAULT_BUCKET}"

  ROLE_NAME="snowflake-iceberg-role-$SUFFIX"
  read -r -p "IAM role name [$ROLE_NAME]: " ROLE_IN
  ROLE_NAME="${ROLE_IN:-$ROLE_NAME}"

  AWS_ACCT=$(aws sts get-caller-identity --query Account --output text)

  echo
  echo ">>> Creating S3 bucket s3://$BUCKET/ in $AWS_REGION ..."
  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "  (bucket already exists — reusing)"
  else
    if [ "$AWS_REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$BUCKET" --region us-east-1 >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" \
        --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
    fi
    echo "  ✓ Created."
  fi

  TRUST_FILE=$(mktemp -t sf_trust.XXXX).json
  POLICY_FILE=$(mktemp -t sf_policy.XXXX).json

  # Placeholder trust: the user's own account can assume (gets replaced in
  # phase 5 with Snowflake's principal once Snowflake hands us one).
  cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${AWS_ACCT}:root"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$EXTERNAL_ID"}}
  }]
}
EOF
  cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:GetObjectVersion"],
     "Resource": "arn:aws:s3:::${BUCKET}/*"},
    {"Effect": "Allow",
     "Action": ["s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::${BUCKET}/*"},
    {"Effect": "Allow",
     "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": "arn:aws:s3:::${BUCKET}"}
  ]
}
EOF

  echo ">>> Creating IAM role $ROLE_NAME ..."
  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "  (role already exists — updating trust policy in place)"
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
      --policy-document file://"$TRUST_FILE" >/dev/null
  else
    aws iam create-role --role-name "$ROLE_NAME" \
      --assume-role-policy-document file://"$TRUST_FILE" >/dev/null
    echo "  ✓ Created."
  fi

  echo ">>> Attaching S3 access policy ..."
  aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name iceberg-s3-access \
    --policy-document file://"$POLICY_FILE" >/dev/null
  echo "  ✓ Attached."

  ROLE_ARN="arn:aws:iam::${AWS_ACCT}:role/${ROLE_NAME}"
  STORAGE_BASE_URL="s3://${BUCKET}/"
  PROVIDER_BLOCK=$(cat <<EOF
      STORAGE_PROVIDER = 'S3'
      STORAGE_BASE_URL = '${STORAGE_BASE_URL}'
      STORAGE_AWS_ROLE_ARN = '${ROLE_ARN}'
      STORAGE_AWS_EXTERNAL_ID = '${EXTERNAL_ID}'
EOF
)
  PRINCIPAL_FIELD="STORAGE_AWS_IAM_USER_ARN"
fi

# ===========================================================================
# GCP PATH — GCS bucket + Service Account
# ===========================================================================
if [ "$CLOUD" = "gcp" ]; then
  GCP_PROJECT_CURRENT=$(gcloud config get-value project 2>/dev/null)
  read -r -p "GCP project [$GCP_PROJECT_CURRENT]: " PROJ_IN
  GCP_PROJECT="${PROJ_IN:-$GCP_PROJECT_CURRENT}"
  if [ -z "$GCP_PROJECT" ]; then echo "  ⚠ project required."; exit 1; fi

  GCP_REGION="us-central1"
  read -r -p "GCS bucket region [$GCP_REGION]: " REGION_IN
  GCP_REGION="${REGION_IN:-$GCP_REGION}"

  DEFAULT_BUCKET="dagster-iceberg-$(whoami | tr -d ' ' | tr '[:upper:]' '[:lower:]')-$SUFFIX"
  read -r -p "GCS bucket name [$DEFAULT_BUCKET]: " BUCKET
  BUCKET="${BUCKET:-$DEFAULT_BUCKET}"

  echo
  echo ">>> Creating GCS bucket gs://$BUCKET/ in $GCP_PROJECT ..."
  if gcloud storage buckets describe "gs://$BUCKET" --project "$GCP_PROJECT" >/dev/null 2>&1; then
    echo "  (bucket already exists — reusing)"
  else
    gcloud storage buckets create "gs://$BUCKET" \
      --project "$GCP_PROJECT" --location "$GCP_REGION" >/dev/null
    echo "  ✓ Created."
  fi

  STORAGE_BASE_URL="gcs://${BUCKET}/"
  PROVIDER_BLOCK=$(cat <<EOF
      STORAGE_PROVIDER = 'GCS'
      STORAGE_BASE_URL = '${STORAGE_BASE_URL}'
EOF
)
  PRINCIPAL_FIELD="STORAGE_GCP_SERVICE_ACCOUNT"
fi

# ===========================================================================
# AZURE PATH — Storage account + container (managed-identity trust)
# ===========================================================================
if [ "$CLOUD" = "azure" ]; then
  read -r -p "Azure resource group (existing): " AZ_RG
  read -r -p "Azure region [eastus]: " AZ_REGION
  AZ_REGION="${AZ_REGION:-eastus}"

  AZ_TENANT=$(az account show --query tenantId -o tsv)
  read -r -p "Azure tenant ID [$AZ_TENANT]: " TENANT_IN
  AZ_TENANT="${TENANT_IN:-$AZ_TENANT}"

  STORAGE_ACCOUNT="dagiceberg$(echo $SUFFIX | tr -d ' ')"
  read -r -p "Storage account name [$STORAGE_ACCOUNT]: " SA_IN
  STORAGE_ACCOUNT="${SA_IN:-$STORAGE_ACCOUNT}"

  CONTAINER="iceberg-data"

  echo
  echo ">>> Creating storage account $STORAGE_ACCOUNT + container $CONTAINER ..."
  if az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$AZ_RG" >/dev/null 2>&1; then
    echo "  (storage account already exists — reusing)"
  else
    az storage account create --name "$STORAGE_ACCOUNT" \
      --resource-group "$AZ_RG" --location "$AZ_REGION" --sku Standard_LRS >/dev/null
    echo "  ✓ Created."
  fi
  az storage container create --name "$CONTAINER" \
    --account-name "$STORAGE_ACCOUNT" --auth-mode login >/dev/null || true

  STORAGE_BASE_URL="azure://${STORAGE_ACCOUNT}.blob.core.windows.net/${CONTAINER}/"
  PROVIDER_BLOCK=$(cat <<EOF
      STORAGE_PROVIDER = 'AZURE'
      STORAGE_BASE_URL = '${STORAGE_BASE_URL}'
      AZURE_TENANT_ID = '${AZ_TENANT}'
EOF
)
  PRINCIPAL_FIELD="AZURE_MULTI_TENANT_APP_NAME"
fi

# ===========================================================================
# Snowsight SQL — common to all clouds
# ===========================================================================
echo
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Cloud-side resources ready. Now run this SQL in Snowsight:"
echo "═══════════════════════════════════════════════════════════════════════"
cat <<SQL_BLOCK

USE ROLE ACCOUNTADMIN;

CREATE OR REPLACE EXTERNAL VOLUME ${VOLUME_NAME}
  STORAGE_LOCATIONS = (
    (
      NAME = 'dagster-demo-iceberg'
$PROVIDER_BLOCK
    )
  );

-- Reveal Snowflake's storage principal (you'll paste it back below):
DESC EXTERNAL VOLUME ${VOLUME_NAME};
-- Look for ${PRINCIPAL_FIELD} in the output and copy its value.

SQL_BLOCK
echo "═══════════════════════════════════════════════════════════════════════"
echo
echo "Run those two statements, then come back here and paste the"
echo "${PRINCIPAL_FIELD} value from DESC EXTERNAL VOLUME's output."
echo
read -r -p "${PRINCIPAL_FIELD}: " SF_PRINCIPAL

if [ -z "$SF_PRINCIPAL" ]; then
  echo "  ⚠ No principal pasted — can't finish trust wiring. Bucket / role exist;"
  echo "    re-run this script (or update the trust policy manually) once you"
  echo "    have the principal."
  exit 1
fi

# ===========================================================================
# Phase 3 — wire trust on the cloud side
# ===========================================================================
echo
echo ">>> Wiring trust for $SF_PRINCIPAL ..."
case "$CLOUD" in
  aws)
    cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "$SF_PRINCIPAL"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$EXTERNAL_ID"}}
  }]
}
EOF
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
      --policy-document file://"$TRUST_FILE" >/dev/null
    echo "  ✓ Trust policy on $ROLE_NAME updated."
    ;;
  gcp)
    # Snowflake hands you a service account email. Grant it Storage Object Admin.
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
      --member="serviceAccount:$SF_PRINCIPAL" \
      --role="roles/storage.objectAdmin" --project "$GCP_PROJECT" >/dev/null
    echo "  ✓ Granted $SF_PRINCIPAL roles/storage.objectAdmin on gs://$BUCKET/."
    ;;
  azure)
    # Snowflake's multi-tenant app needs Storage Blob Data Contributor on the
    # container. This requires the user to first grant admin consent in the
    # Azure portal (one-time per tenant) — script prints the steps if needed.
    echo "  Azure trust wiring is manual:"
    echo "  1. In Azure portal: Microsoft Entra ID → Enterprise applications"
    echo "  2. Search for '$SF_PRINCIPAL' and grant admin consent"
    echo "  3. In the storage account → Access Control (IAM) → Add role assignment"
    echo "     • Role: 'Storage Blob Data Contributor'"
    echo "     • Assign to: the just-consented enterprise application"
    echo "  After completing these steps, the EXTERNAL VOLUME is fully wired."
    ;;
esac

# ===========================================================================
# Verify + final SQL
# ===========================================================================
echo
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Verify the volume can read/write to the cloud-side bucket"
echo "═══════════════════════════════════════════════════════════════════════"
cat <<SQL_BLOCK

-- Run this in Snowsight to confirm Snowflake can access the bucket:
SELECT SYSTEM\$VERIFY_EXTERNAL_VOLUME('${VOLUME_NAME}');

SQL_BLOCK
echo "═══════════════════════════════════════════════════════════════════════"
echo
echo "Expected output: '{\"success\": true, ...}'"
echo "If it fails: trust policy may need a minute to propagate; re-try."
echo
echo "Once verified, re-run setup_snowflake_workspace_demo.sh — the capability"
echo "scan will detect ${VOLUME_NAME} and scaffold the snowflake_iceberg_table"
echo "component pointing at your new ${CLOUD^^} storage."
echo
echo "Resources created:"
case "$CLOUD" in
  aws)
    echo "  • S3 bucket:  s3://$BUCKET/"
    echo "  • IAM role:   $ROLE_ARN"
    echo "  • External-id: $EXTERNAL_ID"
    ;;
  gcp)
    echo "  • GCS bucket: gs://$BUCKET/"
    echo "  • IAM grant on bucket: $SF_PRINCIPAL → roles/storage.objectAdmin"
    ;;
  azure)
    echo "  • Storage account: $STORAGE_ACCOUNT (in $AZ_RG)"
    echo "  • Container:       $CONTAINER"
    echo "  • Tenant:          $AZ_TENANT"
    ;;
esac
