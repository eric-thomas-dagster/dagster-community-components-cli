#!/usr/bin/env bash
# Bank file batch-ingestion demo — file-level visibility without blowing
# past Dagster's partition-count ceiling.
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
#   math and the re-run story.
#
#   "Bank file drops" are simulated as local encrypted files (Fernet —
#   a stand-in for real envelope encryption, NOT a security
#   recommendation). "Snowflake staging" is a local DuckDB table with
#   the same lineage columns a real warehouse table would need.
#
# COST while running
#   $0 — 100% local, no network calls, no cloud credentials.

set -euo pipefail
PROJECT_DIR="${1:-bank-file-batch-ingestion-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q duckdb cryptography
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing the sensor + partitioned asset"
mkdir -p "src/$PKG/defs/bank_file_ingestion"
cat > "src/$PKG/defs/bank_file_ingestion/__init__.py" <<'PYEOF'
"""Bank file batch-ingestion -- file-level visibility without a
per-file partition blowup.

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
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import dagster as dg
from cryptography.fernet import Fernet

BANK_CODES = [
    "bank_alpha",
    "bank_beta",
    "bank_gamma",
    "bank_delta",
    "bank_epsilon",
    "bank_zeta",
]

INCOMING_DIR = Path("incoming")
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


class BankBatchConfig(dg.Config):
    # Exactly the files THIS sensor tick found for this bank -- not the
    # whole day's accumulated list. This is the run's permanent record of
    # what it processed: Dagster persists run_config with the run, so
    # "what did run <run_id> ingest" is answerable forever from the run
    # itself, and re-running that run_id replays exactly this file list.
    files: list[str] = []


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

    inbound_fernet = Fernet(INBOUND_KEY_PATH.read_bytes())
    internal_fernet = Fernet(INTERNAL_KEY_PATH.read_bytes())

    import duckdb

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
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

    for rel_path in config.files:
        path = Path(rel_path)
        raw = path.read_bytes()
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
                    str(path),
                    context.run_id,
                    account_id,
                    amount,
                    currency,
                    transacted_at,
                    reencrypted,
                ],
            )
            total_rows += 1

        files_processed.append(str(path))

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


@dg.sensor(
    job=bank_ingestion_job,
    minimum_interval_seconds=20,
    default_status=dg.DefaultSensorStatus.RUNNING,
    description=(
        "Scans ./incoming/<bank>/*.enc for files not yet seen, groups "
        "them by bank, and emits ONE RunRequest per bank with new files -- "
        "targeting that bank's (bank, today) partition with exactly this "
        "tick's file list in run_config. Never one RunRequest per file."
    ),
)
def bank_file_sensor(context: dg.SensorEvaluationContext):
    state = json.loads(context.cursor) if context.cursor else {"seen": []}
    seen = set(state.get("seen", []))

    new_by_bank: dict[str, list[str]] = {}
    if INCOMING_DIR.exists():
        for bank_dir in sorted(INCOMING_DIR.iterdir()):
            if not bank_dir.is_dir() or bank_dir.name not in BANK_CODES:
                continue
            for f in sorted(bank_dir.glob("*.enc")):
                rel = f.as_posix()
                if rel not in seen:
                    new_by_bank.setdefault(bank_dir.name, []).append(rel)

    if not new_by_bank:
        return dg.SensorResult(skip_reason="No new bank files found")

    today = date.today().isoformat()
    tick_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run_requests = []
    for bank, files in new_by_bank.items():
        partition_key = dg.MultiPartitionKey({"bank": bank, "batch_day": today})
        run_requests.append(
            dg.RunRequest(
                run_key=f"{bank}-{today}-{tick_id}",
                partition_key=partition_key,
                run_config={"ops": {"bank_batch_staged": {"config": {"files": files}}}},
            )
        )
        seen.update(files)

    context.log.info(
        f"Found new files for {len(run_requests)} bank(s): "
        + ", ".join(f"{b}={len(fs)}" for b, fs in new_by_bank.items())
    )
    return dg.SensorResult(run_requests=run_requests, cursor=json.dumps({"seen": sorted(seen)}))


defs = dg.Definitions(
    assets=[bank_batch_staged],
    jobs=[bank_ingestion_job],
    sensors=[bank_file_sensor],
)
PYEOF

echo ">>> Writing the file-drop seeder"
cat > "seed_bank_files.py" <<'PYEOF'
#!/usr/bin/env python3
"""Seeds fake encrypted bank-transaction files into ./incoming/<bank>/ --
simulates a bank's SFTP/S3 drop landing in the bucket this demo's sensor
watches. Run it again any time (while `dg dev` is running, or between
`dg launch` calls) to simulate a new wave of files arriving.

Usage: uv run python seed_bank_files.py [files_per_bank] [rows_per_file]
"""
import json
import random
import sys
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

BANK_CODES = [
    "bank_alpha",
    "bank_beta",
    "bank_gamma",
    "bank_delta",
    "bank_epsilon",
    "bank_zeta",
]
INCOMING_DIR = Path("incoming")
INBOUND_KEY_PATH = Path("secrets/inbound.key")
INTERNAL_KEY_PATH = Path("secrets/internal.key")


def _get_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


def main() -> None:
    files_per_bank = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows_per_file = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    inbound_key = _get_or_create_key(INBOUND_KEY_PATH)
    _get_or_create_key(INTERNAL_KEY_PATH)  # pre-create so the asset finds it even before processing its first file
    fernet = Fernet(inbound_key)

    seeded: list[str] = []
    for bank in BANK_CODES:
        bank_dir = INCOMING_DIR / bank
        bank_dir.mkdir(parents=True, exist_ok=True)
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
            file_path = bank_dir / f"{uuid.uuid4().hex}.enc"
            file_path.write_bytes(encrypted)
            seeded.append(file_path.as_posix())

    print(f"Seeded {files_per_bank} file(s) x {len(BANK_CODES)} banks ({len(seeded)} total) into {INCOMING_DIR}/")
    print(f"Example file: {seeded[0]}")


if __name__ == "__main__":
    main()
PYEOF

echo ">>> Seeding the first batch of fake bank files"
uv run python seed_bank_files.py 3 8

TODAY="$(date +%Y-%m-%d)"
EXAMPLE_FILE="$(ls incoming/bank_alpha/*.enc | head -1)"

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
exact file list to reprocess only those files):
    cd $PROJECT_DIR
    uv run dg launch --assets bank_batch_staged \\
        --partition "bank_alpha|$TODAY" \\
        --config-json '{"ops": {"bank_batch_staged": {"config": {"files": ["$EXAMPLE_FILE"]}}}}'

Inspect what landed (lineage columns: bank, batch_day, source_file, dagster_run_id):
    uv run python -c "import duckdb; print(duckdb.connect('data/bank_staging.duckdb').execute('select bank, batch_day, count(*) rows, count(distinct source_file) files, count(distinct dagster_run_id) runs from bank_transactions_staging group by 1,2 order by 1').fetchall())"

See examples/bank_file_batch_ingestion.md for the full walkthrough, the
partition-count math at real (100-bank) scale, the re-run story, and how
to swap the local DuckDB staging table for a real Snowflake destination.
MSG
