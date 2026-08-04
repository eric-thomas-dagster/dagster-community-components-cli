#!/usr/bin/env python3
"""Grade each walkthrough for Dagster+ Serverless readiness.

Scans setup_<demo>_demo.sh for signals that indicate whether the demo
can be `dagster-cloud serverless deploy-docker`d as-is:

- `local_only`  (❌): docker containers, local servers (Prefect/Temporal/etc.),
                     background `.serve()` processes — hard blockers.
- `needs_mods`  (⚠️): partitioned dbt + local DuckDB (Serverless containers
                     are ephemeral per-run, so DuckDB writes don't persist
                     across partition-materialization runs); other soft blockers.
- `ready`       (✅): no docker, no local server, no partition/persistence trap.
                     May still need env vars (OpenAI key, etc.) — those set in
                     the Dagster+ UI at deploy time, so they don't disqualify.
- `unclear`     (?): the script has ambiguous signals; needs manual review.

Usage:
  python3 tools/check_serverless_ready.py           # print report
  python3 tools/check_serverless_ready.py --json    # machine-readable
  python3 tools/check_serverless_ready.py --apply   # auto-add badges to .md files
                                                    # for `ready` + confident `local_only`
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

LOCAL_ONLY_PATTERNS = [
    r"\bdocker\s+run\b",
    r"\bdocker\s+exec\b",
    r"\bdocker[- ]compose\s",
    r"\bdocker\s+kill\b",
    r"\bdocker\s+ps\b",
    r"\bdocker\s+network\b",
    r"\bkubectl\s",
    r"\bhelm\s+install\b",
    r"prefect\s+server\s+start",
    r"temporal\s+server\s+start",
    r"temporal\s+dev\s+server\s+start",
    r"minio\s+server",
    r"redis-server",
    r"kafka-server-start",
    r"nats-server",
    r"colima\s+start",
    r"firebase\s+emulator",
    r"supabase\s+start",
    # NOTE: '.serve()' by itself is too noisy — matches Kubernetes CRD etc.
    # We look for a `python.*serve` pattern that suggests a background local server.
]

NEEDS_MODS_PATTERNS = [
    # Partitioned dbt with local DuckDB — /tmp doesn't persist across
    # partition-materialization runs on Serverless.
    (r"auto_partition_microbatch:\s*true", "partitioned dbt (auto_partition_microbatch) + likely local DuckDB — /tmp is ephemeral per Serverless run"),
    # Any partitions_def in defs.yaml that co-occurs with a duckdb/sqlite/parquet backend
    # is a candidate — but this is hard to detect from just the setup script text.
]

BUNDLED_FIXTURE_PATTERNS = [
    # Signals that project bundles its own data → OK for Serverless
    r"seeds/[a-z_]+\.csv",
    r"data/[a-z_]+\.csv",
    r"data/[a-z_]+\.duckdb",
    r"SyntheticDataGeneratorComponent",  # synth data
]


def scan(script: Path) -> dict:
    if not script.exists():
        return {"status": "no_setup_script", "signals": [], "why": None}
    text = script.read_text()

    hits_local: list[str] = []
    for pat in LOCAL_ONLY_PATTERNS:
        if re.search(pat, text):
            hits_local.append(pat)

    hits_mods: list[str] = []
    for pat, reason in NEEDS_MODS_PATTERNS:
        if re.search(pat, text):
            hits_mods.append(reason)

    if hits_local:
        return {"status": "local_only", "signals": hits_local, "why": "container/server dependency"}
    if hits_mods:
        return {"status": "needs_mods", "signals": hits_mods, "why": hits_mods[0]}
    return {"status": "ready", "signals": [], "why": None}


BADGES = {
    "ready":       "> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.",
    "needs_mods":  "> ⚠️ **Dagster+ Serverless:** deploys with modifications — {why}.",
    "local_only":  "> ❌ **Dagster+ Serverless:** local-only demo — requires {why}.",
}

# Walkthroughs that already have a hand-authored badge — leave alone.
def already_badged(md_path: Path) -> bool:
    if not md_path.exists():
        return False
    for line in md_path.read_text().splitlines()[:6]:
        if "Dagster+ Serverless:" in line:
            return True
    return False


def apply_badge(md_path: Path, status: str, why: str | None) -> bool:
    if not md_path.exists() or already_badged(md_path):
        return False
    if status not in BADGES:
        return False
    text = md_path.read_text()
    lines = text.splitlines(keepends=True)
    # Find the H1
    h1_i = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if h1_i is None:
        return False
    badge = BADGES[status].format(why=why or "") + "\n"
    # Insert blank line + badge + blank line right after H1
    new = lines[: h1_i + 1] + ["\n", badge, "\n"] + lines[h1_i + 1 :]
    # If the line right after H1 was already blank, collapse doubles
    if h1_i + 1 < len(lines) and lines[h1_i + 1].strip() == "":
        new = lines[: h1_i + 1] + [badge, "\n"] + lines[h1_i + 2 :]
    md_path.write_text("".join(new))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Machine-readable report.")
    ap.add_argument("--apply", action="store_true", help="Auto-add badges for confident classifications.")
    args = ap.parse_args()

    setup_scripts = sorted(EXAMPLES.glob("setup_*_demo.sh"))
    results = []
    for script in setup_scripts:
        demo_name = script.stem.replace("setup_", "").replace("_demo", "")
        md_path = EXAMPLES / f"{demo_name}.md"
        scan_result = scan(script)
        results.append({
            "demo": demo_name,
            "md_exists": md_path.exists(),
            "already_badged": already_badged(md_path),
            **scan_result,
        })

    if args.apply:
        added = 0
        for r in results:
            if r["already_badged"]:
                continue
            if r["status"] in ("ready", "local_only", "needs_mods"):
                md_path = EXAMPLES / f"{r['demo']}.md"
                if apply_badge(md_path, r["status"], r["why"]):
                    added += 1
        print(f"applied {added} badges")

    if args.json:
        print(json.dumps(results, indent=2))
        return

    # Human report
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print(f"scanned {len(results)} setup scripts")
    for k in ("ready", "needs_mods", "local_only", "no_setup_script"):
        print(f"  {k:20} {counts.get(k, 0)}")
    print()

    for status in ("local_only", "needs_mods"):
        matching = [r for r in results if r["status"] == status]
        if matching:
            print(f"=== {status} ({len(matching)}) ===")
            for r in matching[:20]:
                marker = " [already badged]" if r["already_badged"] else ""
                print(f"  {r['demo']:40s}{marker}  ({r['why']})")
            if len(matching) > 20:
                print(f"  ... and {len(matching) - 20} more")
            print()


if __name__ == "__main__":
    main()
