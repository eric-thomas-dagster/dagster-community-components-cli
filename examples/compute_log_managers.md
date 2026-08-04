# Compute Log Managers — Splunk + OTel Collector, live in Docker
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

End-to-end walkthrough validating the three compute log managers shipped in `dagster-community-components` v0.10.18+:

- **`SplunkComputeLogManager`** — Dagster → Splunk HEC (direct, one hop)
- **`OtlpComputeLogManager`** — Dagster → OTel Collector → Splunk HEC (or Datadog, Honeycomb, …)
- **`TeeComputeLogManager`** — composes both, fan-out writes

The demo runs Splunk Free + OTel Collector contrib in Docker on your laptop, scaffolds a Dagster project pointing at both via Tee, materializes a chatty asset, and verifies via Splunk's search API that **events land via both paths in parallel**.

**Live-validated** — running the demo end-to-end materializes the asset and Splunk's search API returns 22 events on each path (`source=dagster` for direct HEC, `source=otel-collector` for the OTel route). The validation step in the script auto-checks this and prints ✓ / ✗.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_compute_log_managers_demo.sh -o setup_compute_log_managers_demo.sh
chmod +x setup_compute_log_managers_demo.sh
./setup_compute_log_managers_demo.sh
```

What it does:

1. Brings up Splunk Free in Docker (`splunk/splunk:latest` — first pull is ~1.5GB, takes 2-3 min to start)
2. Brings up OTel Collector contrib (`otel/opentelemetry-collector-contrib:latest`) configured to forward OTLP/HTTP → Splunk HEC
3. Scaffolds a Dagster project via `uvx create-dagster`
4. Writes `dagster.yaml` with `TeeComputeLogManager(SplunkComputeLogManager + OtlpComputeLogManager)`
5. Drops in a tiny `chatty_asset` that prints 10 lines to stdout
6. Materializes via `dg launch --assets chatty_asset`
7. Waits ~15s for Splunk to ingest + index, then queries Splunk's search API to confirm events landed on **both** paths

## Expected output (success)

```
──────────────────────────────────────────────────────────────────────
  Validation results
──────────────────────────────────────────────────────────────────────
  Direct HEC (source=dagster)           : 22 events
  OTel Collector (source=otel-collector): 22 events

  ✓ BOTH paths working — Tee fan-out validated end-to-end.
```

## What's running where

```
┌────────────────────────────────────────────────────────────────────┐
│  Host (your laptop)                                                 │
│                                                                     │
│   uv run dg launch --assets chatty_asset                            │
│     ↓ asset materializes, op stdout captured to local file          │
│                                                                     │
│   TeeComputeLogManager.upload_to_cloud_storage()                    │
│     ├─ SplunkComputeLogManager._upload_file_obj()                   │
│     │     ↓ POST to https://localhost:18088/services/collector      │
│     └─ OtlpComputeLogManager._upload_file_obj()                     │
│           ↓ POST to http://localhost:14318/v1/logs                  │
│                                                                     │
└────┬─────────────────────────────────────────────────────┬──────────┘
     │                                                     │
     │ HEC (HTTPS, 18088)                                  │ OTLP (HTTP, 14318)
     ▼                                                     ▼
 ┌──────────┐                                       ┌─────────────────┐
 │  Splunk  │                                       │  OTel Collector │
 │  (8088)  │◀─────────  HEC (HTTPS, splunk:8088) ──│  (4318 internal)│
 │  index=  │                                       │                 │
 │  dagster │                                       │  splunk_hec     │
 └──────────┘                                       │  exporter       │
                                                    └─────────────────┘
```

Both containers are on a custom Docker network (`clm-demo-net`) so the OTel Collector resolves the Splunk container by name (`splunk:8088`). The host reaches each via the published ports (`localhost:18088` for Splunk HEC, `localhost:14318` for OTLP).

## What to look at in Splunk Web

Open `http://localhost:18000` — login `admin` / `clm-demo-password-1234`.

```spl
// All events from the demo run
index=dagster "clm-demo-*"

// Split by ingest path
index=dagster "clm-demo-*" | stats count by source

// Inspect structured fields the CLMs ship
index=dagster "clm-demo-*" | table dagster_run_id, dagster_step_key, dagster_io_type, source, _raw

// Compare the two paths side-by-side for the same line
index=dagster "hello line 0" | table source, _raw
```

The direct HEC path uses `source=dagster`; the OTel path uses `source=otel-collector` (set by the Collector's `splunk_hec` exporter). Beyond that, both paths carry the same `dagster_run_id` / `dagster_step_key` / `dagster_io_type` structured fields — just under slightly different names (the OTLP path preserves OTel attribute notation with dots, so it ends up as `dagster.run_id` / `dagster.step_key` / etc. on the Splunk side). Customers can normalize via Splunk's field-extraction.

## Verifying the Dagster UI deep-link

The `SplunkComputeLogManager` writes a "View in Splunk →" deep-link via `display_path_for_type`. To see it:

```bash
cd compute-log-managers-demo
export DAGSTER_HOME="$(pwd)/.dagster_home"
uv run dg dev
```

Open `http://localhost:3000`, click `chatty_asset`, then the **Logs** tab. The link points at the Splunk Web search URL pre-filtered to this run + step + io_type — clicking opens the corresponding Splunk search.

## Anatomy of `dagster.yaml`

The demo's `.dagster_home/dagster.yaml`:

```yaml
compute_logs:
  module: dagster_community_components.compute_log_managers.tee
  class: TeeComputeLogManager
  config:
    local_dir: /tmp/clm-demo-local
    display_manager_index: 0       # show Splunk's URL in the UI
    fail_on_partial_upload: false
    managers:
      - module: dagster_community_components.compute_log_managers.splunk
        class: SplunkComputeLogManager
        config:
          hec_url: https://localhost:18088/services/collector
          hec_token: "clm-demo-hec-token-do-not-use-in-prod"
          splunk_web_url: http://localhost:18000
          index: dagster
          source: dagster                # distinguishes from OTel path
          verify_ssl: false              # self-signed dev Splunk cert
          local_dir: /tmp/clm-demo-local
      - module: dagster_community_components.compute_log_managers.otlp
        class: OtlpComputeLogManager
        config:
          otlp_endpoint: http://localhost:14318
          service_name: dagster
          location_label: clm-demo
          local_dir: /tmp/clm-demo-local
```

For a Dagster+ customer the equivalent is the same `Tee` wrapper, with `dagster_cloud.storage.compute_logs.CloudComputeLogManager` in place of one of the inner CLMs.

### About `local_dir`

`local_dir` is where Dagster captures op stdout/stderr to disk *during* execution — the CLMs read from this path at step finish and ship to each destination. After upload the local file isn't load-bearing; the destinations (Splunk, OTel Collector → Splunk, Dagster+, …) are the systems of record.

The demo sets `local_dir: /tmp/clm-demo-local` explicitly because the demo runs on a host machine (not a container) and the teardown script wants a predictable path to `rm -rf`. **You don't have to set it that way in production.**

If you omit `local_dir`, every CLM falls back to the system temp directory (`/tmp` on Linux containers). That default works fine for:

| Deployment | Need to set `local_dir`? |
|---|---|
| Dagster+ Serverless | No — ephemeral container's `/tmp` lives long enough for capture → upload |
| Dagster+ Hybrid | No — same: user-code container's `/tmp` |
| OSS in K8s (default `emptyDir`) | No — `/tmp` is on the `emptyDir` already |
| Local `dg dev` | No — `/tmp` on macOS / Linux |
| OSS where you want captures to survive a mid-step container restart | Yes — point at a mounted persistent volume |
| Audit / cleanup policy reasons | Yes — separate compute logs from generic `/tmp` |

For Tee specifically: the value is shared with every inner manager (Tee patches each inner's `_local_manager` at construction). The demo's inner managers each set `local_dir: /tmp/clm-demo-local` for visual consistency, but Tee overrides them anyway — only the Tee-level value matters.

## Variations

### Only Splunk (no OTel Collector)

Drop the `tee` wrapper and the OTLP entry:

```yaml
compute_logs:
  module: dagster_community_components.compute_log_managers.splunk
  class: SplunkComputeLogManager
  config:
    hec_url: https://localhost:18088/services/collector
    hec_token: "clm-demo-hec-token-do-not-use-in-prod"
    splunk_web_url: http://localhost:18000
    verify_ssl: false
```

Skip the OTel Collector container in the setup script. The demo collapses to one hop.

### Only OTLP (point at Honeycomb / Datadog / etc.)

Same idea — drop the Splunk inner from Tee, change `otlp_endpoint` + add vendor-specific headers:

```yaml
compute_logs:
  module: dagster_community_components.compute_log_managers.otlp
  class: OtlpComputeLogManager
  config:
    otlp_endpoint: https://api.honeycomb.io
    otlp_headers:
      x-honeycomb-team: "{env: HONEYCOMB_API_KEY}"
      x-honeycomb-dataset: dagster_compute_logs
    display_url_template: "https://ui.honeycomb.io/acme/datasets/dagster_compute_logs?query=dagster.run_id%3D{run_id}"
```

The same `OtlpComputeLogManager` works against any OTLP backend by changing the endpoint + headers.

### Splunk + Dagster+

Replace the OTLP entry in `Tee.managers` with Dagster+'s CloudComputeLogManager:

```yaml
managers:
  - module: dagster_community_components.compute_log_managers.splunk
    class: SplunkComputeLogManager
    config: {...}
  - module: dagster_cloud.storage.compute_logs
    class: CloudComputeLogManager
    config: {}
```

Compute logs end up in both Splunk (for compliance / SIEM workflows) and Dagster+ (for the inline UI viewer).

## Teardown

```bash
docker rm -f clm-demo-splunk clm-demo-otel-collector
docker network rm clm-demo-net
rm -rf /tmp/clm-demo-local /tmp/compute-log-managers-demo
```

## See also

- [`compute_log_managers/splunk/`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/compute_log_managers/splunk) — SplunkComputeLogManager source + README
- [`compute_log_managers/otlp/`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/compute_log_managers/otlp) — OtlpComputeLogManager source + README
- [`compute_log_managers/tee/`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/compute_log_managers/tee) — TeeComputeLogManager source + README
- [Dagster's CloudStorageComputeLogManager](https://docs.dagster.io/_apidocs/libraries/dagster-aws#dagster_aws.s3.S3ComputeLogManager) — the abstract base class these managers extend
- [Splunk HEC docs](https://docs.splunk.com/Documentation/Splunk/latest/Data/UsetheHTTPEventCollector)
- [OTLP/HTTP logs spec](https://opentelemetry.io/docs/specs/otlp/#otlphttp-logs-binary-protocol)
