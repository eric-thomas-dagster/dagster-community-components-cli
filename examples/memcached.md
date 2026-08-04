# Memcached — resource + flush job
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Docker-local end-to-end for the Memcached component set. Two brand-new components, live-validated against `memcached:1.6-alpine`.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_memcached_demo.sh \
  -o setup_memcached_demo.sh
bash setup_memcached_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + Docker + `nc` (netcat, for the setup script's protocol-level seeding). Cost: $0.

## What gets validated

| Component | Role |
|---|---|
| `memcached_resource` | Shared `pymemcache.Client` factory |
| `memcached_cache_flush_job` | Dagster job that runs `flush_all` (or deletes a specific key list) |

The setup script:
1. Seeds 3 keys via the text protocol
2. Executes the flush job through Dagster
3. Verifies the seeded keys are gone (Memcached returns `END` = "not found")

## The chain

```
memcached:1.6-alpine container
   ├─ demo_key_1 → "hello"    ← seeded
   ├─ demo_key_2 → "world"    ← seeded
   └─ demo_key_3 → "dagster"  ← seeded

┌──────────────────────────┐
│ memcached_flush_all      │
│ (Dagster job — one op,   │
│  runs flush_all())       │
└──────────────┬───────────┘
               │
               ▼
      (all seeded keys gone; GETs return END)
```

## Memcached vs Redis — pick your invalidation shape

| You want… | Use |
|---|---|
| Full-cache nuke (`flush_all`) | `memcached_cache_flush_job` with `keys:` empty |
| Delete a small enumerated set of keys | `memcached_cache_flush_job` with `keys: [a, b, c]` |
| Delete by glob pattern (`session:*`) | Use Redis instead — see `redis.md` — Memcached has no SCAN |
| Delete + then also warm cache | Chain: `cache_flush_job` → downstream asset that SETs from a DataFrame |

Memcached's wire protocol is deliberately spartan: `get` / `set` / `delete` / `flush_all` / a few CAS/CAS-like ops. There's no pattern-match / SCAN. If you need `session:*`-style bulk deletes, Redis is the right cache — this component enumerates keys explicitly by design.

## Docker-image note

`memcached:1.6-alpine`. ~10 MB. Instant boot (<1s). Setup script polls via `nc` sending the `version\r\n` protocol command.

Port binding is `11211` → `11211`. Override with `MEMCACHED_HOST_PORT=…` if `11211` is taken.

## Teardown

```bash
docker rm -f dagster_memcached_demo
```

## Retargeting at production Memcached

- **ElastiCache for Memcached** (AWS): swap `host` in the resource YAML. AWS ElastiCache Memcached doesn't support auth; use VPC-level security groups.
- **Google Cloud Memorystore for Memcached**: same shape, swap `host` for the Memorystore endpoint.
- **Self-hosted cluster**: point at your load-balancer / proxy endpoint; pymemcache client-side operates against a single node.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_memcached_demo.sh \
  -o setup_memcached_demo.sh
bash setup_memcached_demo.sh
```

## See also

- [`redis.md`](redis.md) — sibling walkthrough for the Redis component set (pattern-based invalidation + streams + observation)
- Component: [`memcached_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/memcached_resource)
- Component: [`memcached_cache_flush_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/memcached_cache_flush_job)
