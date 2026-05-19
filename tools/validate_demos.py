"""Smart demo validator.

For each setup_*.sh:
  1. Scaffold the project
  2. Inspect what kind of demo it is (assets, op-jobs, partitioned, sensor, ...)
  3. Set up env vars / sidecar resources as needed (DATABASE_URL, Dagster+ token, ...)
  4. Run the right `dg launch` invocation
  5. Record PASS / FAIL / SKIP with a reason

Output: /tmp/validate_demos_results/SMART.tsv
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLES_DIR = Path("/Users/ericthomas/dagster_components/dagster-community-components-cli/examples")
RESULTS = Path("/tmp/validate_demos_results")
RESULTS.mkdir(parents=True, exist_ok=True)
SUMMARY = RESULTS / "SMART.tsv"

DAGSTER_PLUS_CONFIG = Path.home() / ".config/dagster_cloud/config.yaml"


def _read_token_endpoint() -> tuple[str, str] | None:
    if not DAGSTER_PLUS_CONFIG.exists():
        return None
    txt = DAGSTER_PLUS_CONFIG.read_text()
    org = re.search(r"organization:\s*(\S+)", txt)
    dep = re.search(r"deployment:\s*(\S+)", txt)
    tok = re.search(r"api_token:\s*(\S+)", txt)
    if not (org and dep and tok):
        return None
    endpoint = f"https://{org.group(1)}.dagster.cloud/{dep.group(1)}/graphql"
    return tok.group(1), endpoint


def _get_project_dir(setup_script: Path) -> str:
    txt = setup_script.read_text()
    m = re.search(r'PROJECT_DIR="\$\{1:-([a-z0-9_-]+)\}"', txt)
    return m.group(1) if m else f"{setup_script.stem.replace('setup_', '').replace('_demo', '')}-demo"


def _scaffold(setup_script: Path, project_dir: str, log_file: Path, env_overrides: dict) -> bool:
    proj = Path("/tmp") / project_dir
    if proj.exists():
        shutil.rmtree(proj, ignore_errors=True)
    private = Path("/private/tmp") / project_dir
    if private.exists():
        shutil.rmtree(private, ignore_errors=True)

    env = {**os.environ, **env_overrides}
    with open(log_file, "w") as f:
        result = subprocess.run(
            ["bash", str(setup_script)],
            cwd="/tmp", env=env, stdout=f, stderr=subprocess.STDOUT,
        )
    return result.returncode == 0 and proj.exists()


def _list_defs(project_dir: Path, log_file: Path) -> dict | None:
    """Parse `dg list defs --json` output."""
    try:
        result = subprocess.run(
            ["uv", "run", "dg", "list", "defs", "--json"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=120,
        )
        # dg list defs may fail to parse if the project is broken
        if result.returncode != 0:
            with open(log_file, "a") as f:
                f.write(f"\n[smart_validate] dg list defs failed: {result.stderr}\n")
            return None
        return json.loads(result.stdout)
    except Exception as exc:
        with open(log_file, "a") as f:
            f.write(f"\n[smart_validate] list defs exception: {exc}\n")
        return None


def _scan_partition_type(project_dir: Path) -> str | None:
    """Scan the project's defs/*.yaml for partition_type."""
    for yaml_file in (project_dir / "src").rglob("defs.yaml"):
        txt = yaml_file.read_text()
        m = re.search(r"partition_type:\s*(\w+)", txt)
        if m:
            return m.group(1)
    return None


def _pick_partition_key(partition_type: str, project_dir: Path) -> str:
    """Pick a sensible partition key for a launch."""
    if partition_type == "daily":
        # Use a date inside the configured range. Look up partition_start.
        for yaml_file in (project_dir / "src").rglob("defs.yaml"):
            m = re.search(r'partition_start:\s*"([0-9-]+)"', yaml_file.read_text())
            if m:
                return m.group(1)
        # Fallback: yesterday
        return (dt.date.today() - dt.timedelta(days=1)).isoformat()
    if partition_type == "static":
        # Use first partition value. Support both list-on-one-line ([1, 2]) and
        # multiline (\n  - 1\n  - 2) YAML formats.
        for yaml_file in (project_dir / "src").rglob("defs.yaml"):
            txt = yaml_file.read_text()
            # Inline form: partition_values: [1, 2, 3] or ["a", "b"]
            m = re.search(r'partition_values:\s*\[\s*"?([^",\]]+)"?', txt)
            if m:
                return m.group(1).strip()
            # Multiline form
            m = re.search(r"partition_values:\s*\n(?:\s+-\s+\"?([\w-]+)\"?)", txt)
            if m:
                return m.group(1)
        return "default"
    return "2026-04-01"


def _source_env_demo(project_dir: Path) -> dict[str, str]:
    """Many demos generate a `.env.demo` with localhost connection URLs
    (DB2_URL, ELASTICSEARCH_URL, MQTT_BROKER, etc.) and tell the user to
    `source .env.demo` before launching. The validator never sources it,
    causing KeyError failures. This reads the file and returns the exports
    as a dict so they can be folded into the env for `dg launch`.
    """
    env_file = project_dir / ".env.demo"
    if not env_file.exists():
        return {}
    extracted = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        # Match `export KEY=value` or `export KEY='value'` or `export KEY="value"`.
        m = re.match(r"^export\s+([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            val = val[1:-1]
        extracted[key] = val
    return extracted


def _launch(project_dir: Path, args: list[str], env_overrides: dict, log_file: Path) -> bool:
    env = {**os.environ, **_source_env_demo(project_dir), **env_overrides}
    cmd = ["uv", "run", "dg", "launch"] + args
    with open(log_file, "a") as f:
        f.write(f"\n[smart_validate] cmd: {' '.join(cmd)}\n")
        result = subprocess.run(
            cmd, cwd=str(project_dir), env=env,
            stdout=f, stderr=subprocess.STDOUT, timeout=600,
        )
    if result.returncode != 0:
        return False
    # Confirm RUN_SUCCESS
    return "RUN_SUCCESS" in log_file.read_text()


def _record(name: str, status: str, reason: str = ""):
    with open(SUMMARY, "a") as f:
        f.write(f"{name}\t{status}\t{reason}\n")


def validate(setup_script: Path) -> tuple[str, str, str]:
    name = setup_script.stem.replace("setup_", "").replace("_demo", "")
    project_dir = _get_project_dir(setup_script)
    log_file = RESULTS / f"{name}.log"

    # Sensor-driven demos can't be exercised by `dg launch`.
    if name in {"rss_sensor"}:
        # rss_sensor's pipeline materializes assets from a sensor-driven RunRequest.
        # We can still launch the assets directly even without the sensor firing.
        pass  # fall through

    if name == "sensor_gapfill":
        # synthetic_data_generator-driven; the assets materialize fine without the sensor.
        pass  # fall through

    if name == "partitions":
        # This demo intentionally showcases multiple partition shapes
        # (daily + static + dynamic etc.) in one project. `dg launch --assets *`
        # can't materialize a mixed-partition graph in one call. The demo
        # itself works — it just needs per-shape launches that are out of
        # scope for the smart-launch harness.
        return name, "SKIP", "intentional mixed-partition demo; needs per-shape launch"

    # Per-demo env setup
    env_overrides: dict[str, str] = {}

    if name == "cars_sql":
        env_overrides["DATABASE_URL"] = "sqlite:////tmp/cars_demo.db"
    elif name == "movies_sql":
        env_overrides["DATABASE_URL"] = "sqlite:////tmp/movies_demo.db"
        # The demo expects a source SQLite DB at a known location — script writes it itself.
    elif name in {"dagster_plus_audit", "dagster_plus_security_lake"}:
        creds = _read_token_endpoint()
        if not creds:
            return name, "SKIP", "no Dagster+ creds in ~/.config/dagster_cloud/config.yaml"
        token, endpoint = creds
        env_overrides["DAGSTER_PLUS_USER_TOKEN"] = token
        env_overrides["DAGSTER_PLUS_ENDPOINT_URL"] = endpoint

    # 1. Scaffold
    if not _scaffold(setup_script, project_dir, log_file, env_overrides):
        return name, "FAIL_SETUP", str(log_file)

    proj_path = Path("/tmp") / project_dir

    # 2. Inspect what's in the project
    defs = _list_defs(proj_path, log_file)
    if defs is None:
        return name, "FAIL_INSPECT", "dg list defs failed (check log)"

    asset_count = len(defs.get("assets", []))
    job_count = len(defs.get("jobs", []))
    sensor_count = len(defs.get("sensors", []))
    partition_type = _scan_partition_type(proj_path)

    # 3. Build launch invocation
    launch_args: list[str] = []
    reason = ""

    if partition_type:
        key = _pick_partition_key(partition_type, proj_path)
        launch_args = ["--assets", "*", "--partition", key]
        reason = f"partitioned ({partition_type}, key={key})"
    elif asset_count > 0:
        launch_args = ["--assets", "*"]
        reason = f"{asset_count} assets, {job_count} jobs"
    elif job_count > 0:
        # Op-job-only — launch each job
        # Most demos have one user job; pick the first non-system job.
        user_jobs = [j["name"] for j in defs.get("jobs", []) if not j["name"].startswith("__")]
        if not user_jobs:
            return name, "FAIL_NOLAUNCHABLE", "no assets, no user jobs"
        launch_args = ["--job", user_jobs[0]]
        reason = f"op-job: {user_jobs[0]}"
    else:
        return name, "FAIL_NOLAUNCHABLE", "no assets, no jobs, no sensors"

    # 4. Launch
    ok = _launch(proj_path, launch_args, env_overrides, log_file)

    # 4b. Fallback: if we passed --partition but the assets aren't all partitioned with
    # a shared partitions_def, dg launch raises CheckError. Retry without --partition.
    # Symptoms (anywhere in the log):
    #   "Provided '--partition' option, but none of the assets are partitioned"
    #   "There is no PartitionsDefinition shared by all the provided assets"
    if not ok and "--partition" in launch_args:
        log_text = log_file.read_text()
        if (
            "none of the assets are partitioned" in log_text
            or "no PartitionsDefinition shared by all" in log_text
        ):
            retry_args = [a for a in launch_args if a not in ("--partition", launch_args[launch_args.index("--partition") + 1])] if "--partition" in launch_args else launch_args
            # Cleaner reconstruction:
            retry_args = []
            skip_next = False
            for a in launch_args:
                if skip_next:
                    skip_next = False
                    continue
                if a == "--partition":
                    skip_next = True
                    continue
                retry_args.append(a)
            with open(log_file, "a") as f:
                f.write("\n[smart_validate] retry without --partition (mixed-partition asset graph)\n")
            ok = _launch(proj_path, retry_args, env_overrides, log_file)
            reason = f"{reason} (retried unpartitioned)"

    return name, ("OK" if ok else "FAIL_MATERIALIZE"), reason


_AUTH_PATTERNS = re.compile(
    r"(API_KEY|api_key|API_TOKEN|api_token|_TOKEN[= ]|SECRET=|secret_key|"
    r"access_key|ACCESS_KEY|SERVICE_ACCOUNT|client_id|CLIENT_ID|OAUTH|oauth|"
    r"SMTP_USER|SMTP_PASS|SMTP_HOST|IMAP_|"
    r"DAGSTER_PLUS|DAGSTER_CLOUD|"
    r"read -p|read -r|"
    r"snowflake|SNOWFLAKE|bigquery|BIGQUERY|databricks|DATABRICKS|"
    r"aws s3|s3://|gs://|GOOGLE_APPLICATION_CREDENTIALS|azure|AZURE)"
)


def _strip_docs(text: str) -> str:
    """Strip bash comments and `cat <<MSG ... MSG` doc heredocs so auth-pattern
    detection runs against actual logic, not the trailing retargeting prose."""
    text = re.sub(r"cat\s*<<\s*['\"]?MSG['\"]?.*?^MSG\s*$", "", text, flags=re.DOTALL | re.MULTILINE)
    text = re.sub(r"cat\s*<<\s*['\"]?EOF['\"]?.*?^EOF\s*$", "", text, flags=re.DOTALL | re.MULTILINE)
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _needs_auth(setup_script: Path) -> bool:
    """Heuristic: does this setup script need real credentials?

    Returns True if the script references SaaS API keys, cloud credentials,
    or prompts the user for secrets. Returns False for fully-local demos
    (incl. Docker-based ones — those need Docker but not auth).
    """
    return bool(_AUTH_PATTERNS.search(_strip_docs(setup_script.read_text())))


def main():
    open(SUMMARY, "w").close()
    scripts = sorted(EXAMPLES_DIR.glob("setup_*_demo.sh"))
    args = sys.argv[1:]
    no_auth_only = False
    if args and args[0] == "--no-auth":
        no_auth_only = True
        args = args[1:]
    target_demos = args if args else None
    if no_auth_only:
        scripts = [s for s in scripts if not _needs_auth(s)]
        print(f"[no-auth filter] {len(scripts)} demos selected\n", flush=True)

    passed = failed = skipped = 0
    for s in scripts:
        n = s.stem.replace("setup_", "").replace("_demo", "")
        if target_demos and n not in target_demos:
            continue

        print(f"RUN  {n}", flush=True)
        name, status, reason = validate(s)
        _record(name, status, reason)

        line = f"{status:18s} {name}"
        if reason:
            line += f" — {reason}"
        print(line, flush=True)

        if status == "OK":
            passed += 1
        elif status == "SKIP":
            skipped += 1
        else:
            failed += 1

    print()
    print(f"PASSED:  {passed}")
    print(f"FAILED:  {failed}")
    print(f"SKIPPED: {skipped}")


if __name__ == "__main__":
    main()
