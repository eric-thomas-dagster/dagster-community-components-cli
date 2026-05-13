# SAP Event Mesh → Dagster pipeline blueprint

Real-time-ish: subscribe to **SAP Event Mesh** queues and trigger Dagster runs per event. S/4HANA business events, SuccessFactors events, custom app events — all show up here.

## Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │ S/4HANA + SuccessFactors + custom apps               │
   │   publish events to Event Mesh topics                │
   └──────────────────────────┬───────────────────────────┘
                              │ AMQP 1.0
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ SAP Event Mesh (BTP)                                 │
   │   subscriptions land messages in queues you own      │
   └──────────────────────────┬───────────────────────────┘
                              │ REST GET /queues/<q>/messages
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ sap_event_mesh_sensor (Dagster sensor)               │
   │   polls queue; consumes or peeks                     │
   │   → RunRequest per message (with dynamic partition)  │
   └──────────────────────────┬───────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ Downstream pipeline (per-event partition)            │
   └──────────────────────────────────────────────────────┘
```

## Why REST polling instead of AMQP-WebSocket?

Event Mesh supports both. AMQP-WebSocket gives sub-second latency but needs a persistent connection — a poor fit for Dagster Daemon which is short-lived/poll-driven. REST polling gives bounded latency (interval-bounded) with vastly simpler operational shape:

- No long-lived connection to maintain
- Survives restarts / pod evictions cleanly
- Queues buffer events between polls (Event Mesh persists messages)
- Standard Dagster sensor pattern — runs on schedule, returns RunRequests

For sub-second latency requirements, build a separate AMQP consumer service that pushes into Dagster via `bin/kick_off_run.sh`. The sensor pattern is right for batch-friendly latency (~30s).

## Setup

### 1. Provision Event Mesh in BTP

1. **BTP Cockpit** → Subscriptions → Event Mesh → Configure → **Create Service Instance**
2. Bind a **service key** — copy `messaging_host`, `clientid`, `clientsecret`, `tokenendpoint`
3. In the Event Mesh UI: create **queues** + **subscriptions** to the topics you care about

### 2. Subscribe to S/4HANA event topics

In Event Mesh UI → Subscriptions → Subscribe queue `orders.received` to topic `sap.s4.beh.salesorder.v1.SalesOrder.Created.v1`.

S/4HANA publishes events via SAP Event Enablement. The topic naming convention is `sap.s4.beh.<object>.v1.<EventName>.v1`.

### 3. Configure the OAuth token resource

```yaml
# resources/em_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: em_token
  token_endpoint: https://my-tenant.authentication.eu10.hana.ondemand.com/oauth/token
  grant_type: client_credentials
  client_id_env_var: SAP_EM_CLIENT_ID
  client_secret_env_var: SAP_EM_CLIENT_SECRET
  auth_in: basic
```

### 4. Configure the sensor

```yaml
# defs/em_orders_sensor/defs.yaml
type: dagster_community_components.SapEventMeshSensorComponent
attributes:
  sensor_name: sap_em_orders_sensor
  messaging_host: https://my-tenant.messaging.eu10.hana.ondemand.com
  queue_name: orders.received
  oauth_token_resource_key: em_token

  # Production pattern: register a dynamic partition per message
  asset_selection: [process_order_event]
  dynamic_partition_name: order_events
  partition_key_template: "{message_id}"

  batch_size: 10
  minimum_interval_seconds: 30
  default_status: running
```

### 5. Build the downstream asset

```yaml
# defs/process_order_event/defs.yaml
type: dagster_community_components.SomeProcessingComponent  # your own logic
attributes:
  asset_name: process_order_event
  partition_type: dynamic
  dynamic_partition_name: order_events
  # ... fetch the event payload from Event Mesh (peek mode) or persist payloads upstream
```

## Production pattern: events → dynamic partitions

The **right shape** for event-driven Dagster:

1. Sensor consumes the queue (pops messages) → registers a dynamic partition per `message_id`
2. The downstream asset's `partition_type: dynamic` means each partition is **re-runnable**
3. Re-running a partition triggers re-processing of that specific event
4. Asset history retains the full event log

This matches the dynamic-partition-over-storage pattern used in the streaming-blueprint examples ([`eh_capture_pipeline.md`](eh_capture_pipeline.md), [`pubsub_gcs_pipeline.md`](pubsub_gcs_pipeline.md)) — Dagster doesn't process the queue directly; the queue's messages become **the boundary** between the streaming world and Dagster's batch-y world.

## peek vs consume

- `peek: false` (default) — REST endpoint pops the message off the queue. Once read, gone. Good for "fire and forget" event handling.
- `peek: true` — read-only; messages stay in queue. Useful for observability (count + sample) without consuming the stream.

## Common S/4HANA Cloud event topics

| Topic | Fires when |
|---|---|
| `sap.s4.beh.businesspartner.v1.BusinessPartner.Created.v1` | New customer/vendor |
| `sap.s4.beh.businesspartner.v1.BusinessPartner.Changed.v1` | Customer/vendor updated |
| `sap.s4.beh.salesorder.v1.SalesOrder.Created.v1` | New sales order |
| `sap.s4.beh.salesorder.v1.SalesOrder.ShippingBlock.Released.v1` | Order shipping unblocked |
| `sap.s4.beh.purchaseorder.v1.PurchaseOrder.Released.v1` | PO released |
| `sap.s4.beh.workflow.v1.WorkflowTaskCreated.v1` | Workflow task created |
| `sap.s4.beh.fixedasset.v1.FixedAsset.Posted.v1` | Asset posting |

Full catalog: **SAP Business Accelerator Hub → Events → SAP S/4HANA Cloud, Public Edition**.

## Templating events into partition keys / tags

Beyond `{message_id}`, top-level fields of the event payload are substitutable:

```yaml
partition_key_template: "{business_partner_id}_{message_id}"   # dedup by entity-event combo
tags_template:
  sap.event_type: "{type}"
  sap.tenant: "{tenant_id}"
  business_partner: "{business_partner_id}"
```

The substitution context is built from the event body's top-level fields. Nested fields aren't supported — flatten them client-side or use `peek: true` and parse in-asset.

## Trade-offs & gotchas

- **Queue depth.** If polling is slower than ingest, queues grow unboundedly. Monitor via Event Mesh UI; alert on backlog > threshold. Tune `batch_size` + `minimum_interval_seconds`.
- **Once-and-only-once delivery.** REST consume = at-most-once (if the sensor crashes after pop but before RunRequest emit, the message is lost). Use peek + dedup downstream for stronger semantics. Or use the storage-layer pattern (Event Mesh → durable storage → Dagster reads files).
- **Token rotation.** XSUAA tokens last ~12 hours. The OAuth resource auto-refreshes, no manual intervention.
- **Subscription scope.** Each Event Mesh service instance is scoped to a BTP subaccount. Multi-subaccount setups need multiple resource + sensor pairs.
- **Cost.** Event Mesh is billed by message volume on BTP. High-volume streams (>1M msg/day) get pricey — consider whether you really need event-driven vs. batch ingestion.

## See also

- [`oauth_token_resource`](https://dagster-component-ui.vercel.app/c/oauth_token_resource) — paired token manager
- [`sap_cpi_pipeline.md`](sap_cpi_pipeline.md) — companion sensor for CPI iFlow runs
- [`eh_capture_pipeline.md`](eh_capture_pipeline.md) — Azure cousin (Event Hubs Capture → Dagster)
- [`pubsub_gcs_pipeline.md`](pubsub_gcs_pipeline.md) — GCP cousin
- [SAP Event Mesh docs](https://help.sap.com/docs/SAP_EM)
