# DynamoDB Local — reader + writer + resource round-trip
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Docker-local end-to-end for the DynamoDB component set using AWS's official `amazon/dynamodb-local` emulator. No AWS account, no credentials, no cost — every read/write goes to a container on `:8000`.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dynamodb_local_demo.sh \
  -o setup_dynamodb_local_demo.sh
bash setup_dynamodb_local_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + Docker. Cost: $0.

## What gets validated

| Component | Role |
|---|---|
| `dynamodb_resource` | Shared boto3 client factory |
| `dynamodb_reader` | Scan a table → DataFrame |
| `dynamodb_writer` | DataFrame → BatchWriteItem into a target table |

## The trick — no component change needed

The `dynamodb_reader` and `dynamodb_writer` components use plain boto3, and boto3 respects the `AWS_ENDPOINT_URL_DYNAMODB` env var to redirect every call to a local endpoint. So the setup:

```bash
export AWS_ACCESS_KEY_ID=local
export AWS_SECRET_ACCESS_KEY=local
export AWS_REGION=us-east-1
export AWS_ENDPOINT_URL_DYNAMODB=http://127.0.0.1:8010
```

…transparently retargets every reader/writer at `amazon/dynamodb-local` — no `endpoint_url` field required, no fork of the component. When you're ready for real AWS, unset the endpoint override and swap dummy creds for real ones.

## The chain

```
DynamoDB Local container (amazon/dynamodb-local)
   ├─ table: orders            ← 5 seed items (4 active, 1 cancelled)
   └─ table: orders_processed  ← empty (target for round-trip write)

┌──────────────────────────┐
│ active_orders            │
│ (dynamodb_reader Scan)   │
│ → DataFrame of 5 rows    │
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│ active_orders_written    │
│ (dynamodb_writer         │
│  BatchWriteItem into     │
│  orders_processed)       │
└──────────────────────────┘
```

## Docker-image note

`amazon/dynamodb-local:latest` is the official AWS-published emulator. ~450 MB, first-boot <5 seconds. Setup script binds host-port `8010` → container-port `8000` (host `:8000` is often taken — override with `DYNAMODB_HOST_PORT=…`). The readiness probe verifies the emulator answers on `/`, not just any process on the port.

The emulator implements the DynamoDB API surface — Scan / Query / PutItem / BatchWriteItem / etc. — but is in-memory by default (data is lost when the container stops). For persistence across restarts, add `-v $(pwd)/data:/home/dynamodblocal/data` and `-command "-sharedDb -dbPath /home/dynamodblocal/data"` to the `docker run`.

## Teardown

```bash
docker stop dagster_dynamodb_demo && docker rm dagster_dynamodb_demo
```

## Retargeting at real AWS

Once the demo works, swap to a real AWS account by:

1. `unset AWS_ENDPOINT_URL_DYNAMODB`
2. `export AWS_ACCESS_KEY_ID=<real>` / `AWS_SECRET_ACCESS_KEY=<real>` (or use an IAM role)
3. Create the `orders` + `orders_processed` tables in the target region

The same `defs.yaml` files work unchanged — the emulator's whole value proposition is API surface compatibility.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dynamodb_local_demo.sh \
  -o setup_dynamodb_local_demo.sh
bash setup_dynamodb_local_demo.sh
```

## See also

- [mongodb](mongodb.md), [couchdb](couchdb.md), [neo4j](neo4j.md), [cassandra](cassandra.md) — same round-trip pattern for other NoSQL stores
