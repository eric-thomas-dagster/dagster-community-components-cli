#!/usr/bin/env bash
# Bank file batch-ingestion demo — file-level visibility without blowing
# past Dagster's partition-count ceiling. Sensor is a REAL registry
# component (S3MonitorSensorComponent, partition_mode: business_key_batch)
# -- not hand-rolled. Only the decrypt/transform/re-encrypt/stage asset
# stays custom Python, since that logic is inherently business-specific.
#
# WHAT THIS DEMONSTRATES
#   ~100 banks drop files every few minutes; a naive design registers
#   each new file as its own Dagster dynamic partition on a shared
#   asset. That blows past Dagster's documented partition-count
#   guidance (~100k OSS-recommended, ~250k Dagster+ soft ceiling —
#   https://github.com/dagster-io/dagster/discussions/17809) within
#   months at that file volume, with no built-in way to prune
#   time-window partition keys the way dynamic partitions can be.
#
#   The fix: ONE shared asset, partitioned on (bank, batch_day) — a
#   static dimension x a daily time-window dimension — re-materialized
#   once per sensor tick that finds new files for that bank. Dagster
#   caps the number of PARTITION KEYS an asset can have, not the
#   number of materialization EVENTS per key — so file-level
#   visibility comes from each tick's own materialization metadata +
#   row-level lineage columns in the staging table, not from a
#   partition per file. See bank_file_batch_ingestion.md for the full
#   math, the credit-cost comparison, and the re-run story.
#
# WHY A REAL S3 ENDPOINT (moto), NOT A PLAIN LOCAL DIRECTORY
#   S3MonitorSensorComponent talks to a real S3 API (boto3) — it can't
#   watch a plain local directory. moto's standalone server mode
#   (`moto[server]` -> `moto_server`) gives a real, local, S3-API-
#   compatible endpoint with zero Docker and zero AWS credentials
#   (fake key/secret values moto doesn't validate) — lighter than the
#   Minio+Docker setup this repo's other `s3_pipeline` demo uses, and
#   enough to exercise the UNMODIFIED component for real.
#
#   "Bank file drops" are simulated as S3 objects with Fernet-
#   "encrypted" bodies (Fernet is a stand-in for real envelope
#   encryption, NOT a security recommendation). "Snowflake staging" is
#   a local DuckDB table with the same lineage columns a real
#   warehouse table would need.
#
# COST while running
#   $0 — 100% local (moto, not real AWS), no network calls out, no
#   cloud credentials.

set -euo pipefail
PROJECT_DIR="${1:-bank-file-batch-ingestion-demo}"
MOTO_PORT=5111
BUCKET=bank-files
PREFIX=incoming/
# Pinned to the exact commit that adds S3MonitorSensorComponent's
# business_key_batch mode -- not yet on PyPI. Bump this (or switch to a
# plain PyPI version pin) once a release ships with it.
DCC_COMMIT_SHA="4bf176a2f68071fbb9aa84608999bf504f0f9444"
# Deliberately NOT trap-killed on script exit -- this backgrounded process
# needs to keep running AFTER this setup script finishes, for `dg dev`/
# `dg launch` to talk to it. Matches this repo's `setup_s3_pipeline_demo.sh`
# precedent (a detached `docker run -d` Minio container that outlives its
# own setup script too) -- a "Cleanup" line at the end tells the user how
# to stop it when they're done with the demo.
echo ">>> Starting moto's standalone S3 server (uvx, no Docker, no real AWS) on :$MOTO_PORT"
uvx --from 'moto[server]' moto_server -p "$MOTO_PORT" >/tmp/moto_server.log 2>&1 &
disown
for _ in $(seq 1 20); do
  curl -sf "http://localhost:$MOTO_PORT" >/dev/null 2>&1 && break
  sleep 0.5
done

export AWS_ACCESS_KEY_ID=testing
export AWS_SECRET_ACCESS_KEY=testing
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL="http://localhost:$MOTO_PORT"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q duckdb cryptography boto3
uv add -q "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/${DCC_COMMIT_SHA}.zip"
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

cat > ".env" <<ENVEOF
AWS_ACCESS_KEY_ID=testing
AWS_SECRET_ACCESS_KEY=testing
AWS_DEFAULT_REGION=us-east-1
AWS_ENDPOINT_URL=http://localhost:$MOTO_PORT
ENVEOF

echo ">>> Creating the bucket in moto"
uv run python3 -c "
import boto3
boto3.client('s3').create_bucket(Bucket='$BUCKET')
print('created s3://$BUCKET')
"

echo ">>> Writing the sensor — a REAL component (S3MonitorSensorComponent), not hand-rolled"
mkdir -p "src/$PKG/defs/bank_file_sensor"
cat > "src/$PKG/defs/bank_file_sensor/defs.yaml" <<YAMLEOF
type: dagster_community_components.S3MonitorSensorComponent
attributes:
  sensor_name: bank_file_sensor
  bucket_name: $BUCKET
  prefix: "$PREFIX"
  key_pattern: '.*\.enc$'
  job_name: bank_ingestion_job
  minimum_interval_seconds: 20
  partition_mode: business_key_batch
  business_key_dimension_name: bank
  business_keys:
    - bank_alpha
    - bank_beta
    - bank_gamma
    - bank_delta
    - bank_epsilon
    - bank_zeta
  batch_dimension_name: batch_day
  op_name: bank_batch_staged
YAMLEOF

echo ">>> Writing the partitioned asset — decrypt -> transform -> re-encrypt -> stage, one callable"
mkdir -p "src/$PKG/defs/bank_file_ingestion"
cat > "src/$PKG/defs/bank_file_ingestion/__init__.py" <<'PYEOF'
"""Bank file batch-ingestion -- file-level visibility without a
per-file partition blowup. The SENSOR is `S3MonitorSensorComponent`
(partition_mode: business_key_batch, see ../bank_file_sensor/defs.yaml)
-- a real registry component, not hand-rolled. This asset is the one
piece that stays custom Python: decrypt -> transform -> re-encrypt ->
stage is inherently business-specific logic, not a generic pattern a
registry component should own.

~100 banks drop files every few minutes. A naive design registers each
new file as its own Dagster dynamic partition on a shared asset --
that blows past Dagster's documented partition-count guidance
(~100k OSS-recommended, ~250k Dagster+ soft ceiling; see
https://github.com/dagster-io/dagster/discussions/17809) in months at
that volume, with no built-in way to prune time-window partition keys.

The fix demonstrated here: ONE shared asset partitioned on
(bank, batch_day) -- a static dimension x a daily time-window
dimension -- re-materialized once per sensor tick that finds new
files for that bank. Dagster caps the number of PARTITION KEYS an
asset can have, not the number of materialization EVENTS per
partition key -- so file-level visibility comes from each tick's own
materialization metadata + row-level lineage columns, not from
spinning up a new partition per file.

At real scale (100 banks): 100 banks x 365 days/year = 36,500
partitions/year -- ~7 years before the 250k ceiling, with zero pruning
maintenance (unlike dynamic partitions, daily partition keys aren't
individually stored/registered; nothing to prune). Hourly batching
would be 100 x 24 x 365 = 876,000/year -- past the limit in under 4
months -- which is why this demo batches by DAY, not by sensor tick.
See bank_file_batch_ingestion.md for the credit-cost comparison too.
"""

import json
from pathlib import Path

import dagster as dg

BANK_CODES = [
    "bank_alpha",
    "bank_beta",
    "bank_gamma",
    "bank_delta",
    "bank_epsilon",
    "bank_zeta",
]

INBOUND_KEY_PATH = Path("secrets/inbound.key")
INTERNAL_KEY_PATH = Path("secrets/internal.key")
DB_PATH = Path("data/bank_staging.duckdb")

# The (bank, batch_day) shape that keeps the partition count bounded.
# end_offset=1 is required, not cosmetic: with Dagster's default (0), a
# daily partition set only ever includes COMPLETED days -- "today" isn't
# a valid partition key until tomorrow, so a same-day sensor tick would
# raise trying to target it. This demo (like the real use case) needs to
# re-materialize TODAY's partition intraday, so the window must include
# the in-progress day.
bank_batch_partitions = dg.MultiPartitionsDefinition(
    {
        "bank": dg.StaticPartitionsDefinition(BANK_CODES),
        "batch_day": dg.DailyPartitionsDefinition(start_date="2025-01-01", end_offset=1),
    }
)


class BankFile(dg.Config):
    # Mirrors S3MonitorSensorComponent's per-object config_block shape
    # exactly (bucket/key/size/etag/last_modified/prefix/region) --
    # that's what `business_key_batch` mode puts in run_config.
    bucket: str
    key: str
    size: int
    etag: str
    last_modified: str
    prefix: str
    region: str


class BankBatchConfig(dg.Config):
    # Exactly the files THIS sensor tick found for this bank -- not the
    # whole day's accumulated list. This is the run's permanent record of
    # what it processed: Dagster persists run_config with the run, so
    # "what did run <run_id> ingest" is answerable forever from the run
    # itself, and re-running that run_id replays exactly this file list.
    files: list[BankFile] = []


@dg.asset(
    name="bank_batch_staged",
    partitions_def=bank_batch_partitions,
    group_name="bank_ingestion",
    kinds={"python", "duckdb"},
    description=(
        "Decrypt -> transform -> re-encrypt -> stage, in one callable per "
        "(bank, batch_day) partition. One op, not three -- file-level "
        "visibility comes from this materialization's metadata and the "
        "row-level lineage columns written to the staging table, not from "
        "a separate partition per file."
    ),
)
def bank_batch_staged(context: dg.AssetExecutionContext, config: BankBatchConfig) -> dg.MaterializeResult:
    mpk = context.partition_key  # a MultiPartitionKey (str subclass)
    bank = mpk.keys_by_dimension["bank"]
    batch_day = mpk.keys_by_dimension["batch_day"]

    import boto3
    from cryptography.fernet import Fernet

    s3 = boto3.client("s3")  # respects AWS_ENDPOINT_URL env -- routes to moto, not real AWS
    inbound_fernet = Fernet(INBOUND_KEY_PATH.read_bytes())
    internal_fernet = Fernet(INTERNAL_KEY_PATH.read_bytes())

    import random
    import time

    import duckdb

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # DuckDB allows exactly one writer on a file at a time (unlike a real
    # warehouse) -- and the sensor's own design point is that MULTIPLE banks'
    # runs legitimately land in the SAME tick and get scheduled concurrently
    # by Dagster's executor. Retry-with-jitter on the lock conflict rather
    # than letting the run fail; a real Snowflake/warehouse destination
    # doesn't have this limitation at all, so this retry loop is purely a
    # demo-local-DuckDB concern, not something the real pattern needs.
    con = None
    for attempt in range(10):
        try:
            con = duckdb.connect(str(DB_PATH))
            break
        except duckdb.IOException:
            if attempt == 9:
                raise
            time.sleep(0.2 * (attempt + 1) + random.uniform(0, 0.2))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bank_transactions_staging (
            bank VARCHAR,
            batch_day VARCHAR,
            source_file VARCHAR,
            dagster_run_id VARCHAR,
            account_id VARCHAR,
            amount DOUBLE,
            currency VARCHAR,
            transacted_at VARCHAR,
            payload_reencrypted BLOB
        )
        """
    )

    files_processed: list[str] = []
    total_bytes = 0
    total_rows = 0

    for f in config.files:
        raw = s3.get_object(Bucket=f.bucket, Key=f.key)["Body"].read()
        total_bytes += len(raw)

        # decrypt (inbound key -- simulates a bank-provided PGP/KMS key)
        decrypted = inbound_fernet.decrypt(raw)
        payload = json.loads(decrypted.decode())

        for txn in payload.get("transactions", []):
            # transform -- normalize account id + currency casing, coerce amount
            account_id = str(txn.get("account_id", "")).upper()
            amount = float(txn.get("amount", 0.0))
            currency = str(txn.get("currency", "USD")).upper()
            transacted_at = str(txn.get("transacted_at", ""))

            # re-encrypt (internal key -- simulates the at-rest key for the
            # staging layer, distinct from the inbound key)
            row_payload = json.dumps(
                {
                    "account_id": account_id,
                    "amount": amount,
                    "currency": currency,
                    "transacted_at": transacted_at,
                }
            ).encode()
            reencrypted = internal_fernet.encrypt(row_payload)

            con.execute(
                "INSERT INTO bank_transactions_staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    bank,
                    batch_day,
                    f.key,
                    context.run_id,
                    account_id,
                    amount,
                    currency,
                    transacted_at,
                    reencrypted,
                ],
            )
            total_rows += 1

        files_processed.append(f.key)

    con.close()

    return dg.MaterializeResult(
        metadata={
            "bank": bank,
            "batch_day": batch_day,
            "file_count": len(files_processed),
            "row_count": total_rows,
            "bytes_processed": total_bytes,
            "files_processed": dg.MetadataValue.json(files_processed),
            "dagster_run_id": context.run_id,
        }
    )


bank_ingestion_job = dg.define_asset_job(
    "bank_ingestion_job",
    selection=[bank_batch_staged],
)

defs = dg.Definitions(
    assets=[bank_batch_staged],
    jobs=[bank_ingestion_job],
)
PYEOF

echo ">>> Pre-creating encryption keys"
uv run python3 -c "
from pathlib import Path
from cryptography.fernet import Fernet
Path('secrets').mkdir(exist_ok=True)
for p in ['secrets/inbound.key', 'secrets/internal.key']:
    path = Path(p)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
"

echo ">>> Writing the file-drop seeder (writes to moto-backed S3, not a local directory)"
cat > "seed_bank_files.py" <<PYEOF
#!/usr/bin/env python3
"""Seeds fake encrypted bank-transaction files into s3://<bucket>/incoming/<bank>/ --
simulates a bank's SFTP/S3 drop landing in the bucket the bank_file_sensor component
watches. Run it again any time (while \`dg dev\` is running, or between \`dg launch\`
calls) to simulate a new wave of files arriving. Sets the same moto-pointing AWS env
vars \`dg dev\`/\`dg launch\` get from .env as its OWN defaults (setdefault -- a real
override in the calling shell still wins), so it works standalone too, not just when
launched from a shell that already sourced .env.

Usage: uv run python seed_bank_files.py [files_per_bank] [rows_per_file]
"""
import json
import os
import random
import sys
import uuid
from pathlib import Path

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ENDPOINT_URL", "http://localhost:$MOTO_PORT")

import boto3
from cryptography.fernet import Fernet

BANK_CODES = [
    "bank_alpha",
    "bank_beta",
    "bank_gamma",
    "bank_delta",
    "bank_epsilon",
    "bank_zeta",
]
BUCKET = "bank-files"
PREFIX = "incoming/"
INBOUND_KEY_PATH = Path("secrets/inbound.key")


def main() -> None:
    files_per_bank = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows_per_file = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    fernet = Fernet(INBOUND_KEY_PATH.read_bytes())
    s3 = boto3.client("s3")  # respects AWS_ENDPOINT_URL env -- routes to moto

    seeded: list[str] = []
    for bank in BANK_CODES:
        for _ in range(files_per_bank):
            transactions = [
                {
                    "account_id": f"acct-{random.randint(1000, 9999)}",
                    "amount": round(random.uniform(-500, 2500), 2),
                    "currency": random.choice(["usd", "USD", "eur"]),
                    "transacted_at": "2026-11-25T00:00:00Z",
                }
                for _ in range(rows_per_file)
            ]
            payload = json.dumps({"bank": bank, "transactions": transactions}).encode()
            encrypted = fernet.encrypt(payload)
            key = f"{PREFIX}{bank}/{uuid.uuid4().hex}.enc"
            s3.put_object(Bucket=BUCKET, Key=key, Body=encrypted)
            seeded.append(key)

    print(f"Seeded {files_per_bank} file(s) x {len(BANK_CODES)} banks ({len(seeded)} total) into s3://{BUCKET}/{PREFIX}")
    print(f"Example key: {seeded[0]}")


if __name__ == "__main__":
    main()
PYEOF

echo ">>> Seeding the first batch of fake bank files"
uv run python seed_bank_files.py 3 8

TODAY="$(date -u +%Y-%m-%d)"

cat <<MSG

>>> Setup complete.

See the pattern fire (open UI):
    cd $PROJECT_DIR && uv run dg dev
        -> http://localhost:3000 -- the sensor starts RUNNING by default
           and picks up the seeded files within ~20s (one run per bank,
           six total — check the Runs tab).
        -> seed more while it's running to see a SECOND materialization
           land on the SAME (bank, day) partition instead of a new one:
           uv run python seed_bank_files.py 2 5

Or headless -- launch one (bank, day) partition directly with an explicit
file list (this is also the re-run recipe: point it at a prior run's
exact file list to reprocess only those files — check the Runs tab for
the real file list a given run_id actually processed):
    cd $PROJECT_DIR
    uv run dg launch --assets bank_batch_staged \\
        --partition "bank_alpha|$TODAY" \\
        --config-json '{"ops": {"bank_batch_staged": {"config": {"files": []}}}}'

Inspect what landed (lineage columns: bank, batch_day, source_file, dagster_run_id):
    uv run python -c "import duckdb; print(duckdb.connect('data/bank_staging.duckdb').execute('select bank, batch_day, count(*) rows, count(distinct source_file) files, count(distinct dagster_run_id) runs from bank_transactions_staging group by 1,2 order by 1').fetchall())"

Moto's S3 server keeps running in the background at http://localhost:$MOTO_PORT
after this script exits (deliberately -- dg dev/dg launch need it available
later, same as this repo's Minio-based s3_pipeline demo leaves its Docker
container running). AWS_ENDPOINT_URL is written to .env in $PROJECT_DIR, so
dg dev/dg launch pick it up automatically from a fresh shell too.

Cleanup when you're done with the demo:
    lsof -ti:$MOTO_PORT | xargs kill

See examples/bank_file_batch_ingestion.md for the full walkthrough, the
partition-count math + credit-cost comparison at real (100-bank) scale,
the re-run story, and how to swap the local DuckDB staging table for a
real Snowflake destination.
MSG
