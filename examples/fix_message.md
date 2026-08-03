# FIX Message Parser — synthetic trading messages → flat DataFrame

**Validated end-to-end** (pure Python). 30 synthetic FIX 4.4 messages → 30 flat rows with resolved enums and tag lookups, filtered to orders + executions only.

```
fix_messages         ← synthetic_data_generator (fix_messages, 30 msgs)
       │
       └── fix_flat   ← fix_message_parser (msg_type_filter: [D, 8])
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | `schema_type: fix_messages` emits proper FIX 4.4 wire format with `8=FIX.4.4`, `9=<body_length>`, `10=<checksum>` envelopes. Mix of NewOrderSingle (D) + ExecutionReport (8). Pipe-rendered to make the column legible in pandas; canonical SOH (`\x01`) is also auto-detected. |
| `fix_message_parser` | Resolves common FIX tag IDs to friendly columns (symbol, side, ord_type, time_in_force, ord_status, exec_type, etc.) with proper enum decoding. Preserves the full raw tag dict in `tags_raw` for ad-hoc analysis. |

## Live output

```
30 rows × 31 cols
By msg type:
  ExecutionReport    20
  NewOrderSingle     10
```

**NewOrderSingle sample (D):**

| cl_ord_id | symbol | side | ord_type | time_in_force | order_qty | price |
|---|---|---|---|---|---|---|
| CLORD00000001 | GOOGL | buy | market | ioc | 4,084 | — |
| CLORD00000004 | TSLA | sell | limit | day | 921 | 285.40 |

**ExecutionReport sample (8):**

| order_id | symbol | side | ord_status | order_qty | last_qty | last_px | cum_qty | leaves_qty |
|---|---|---|---|---|---|---|---|---|
| ORD00000002 | SPY | sell | filled | 3,870 | 3,870 | 405.36 | 3,870 | 0 |
| ORD00000005 | NVDA | buy | partially_filled | 8,200 | 4,100 | 487.12 | 4,100 | 4,100 |

## Supported message types

| Code | Name | Notes |
|---|---|---|
| `D` | NewOrderSingle | Buy / sell orders |
| `F` | OrderCancelRequest | |
| `G` | OrderCancelReplaceRequest | |
| **`8`** | **ExecutionReport** | Heavy-volume — fills, partial fills, rejects |
| `9` | OrderCancelReject | |
| `3` | Reject | Session-level |
| `0` | Heartbeat | |
| `A` | Logon | |
| `5` | Logout | |
| `W` | MarketDataSnapshotFullRefresh | |

## Resolved enums

The parser decodes FIX numeric codes to human-readable values:

| Field | Codes → values |
|---|---|
| `side` | 1→buy, 2→sell, 3→buy_minus, 4→sell_plus, 5→sell_short, 6→sell_short_exempt |
| `ord_type` | 1→market, 2→limit, 3→stop, 4→stop_limit |
| `time_in_force` | 0→day, 1→gtc, 3→ioc, 4→fok, 5→gtx, 6→gtd |
| `ord_status` | new / partially_filled / filled / canceled / replaced / rejected / etc. |
| `exec_type` | new / partial_fill / fill / canceled / trade / trade_correct / etc. |

## Auto-detected delimiter

Canonical FIX uses SOH (`\x01`) between tags. Trading-system log files often re-render that as `|` for human reading. The parser auto-detects both — no config needed.

## `msg_type_filter`

Heavy-volume FIX firehose typically wants only specific message types. The component accepts a list of MsgType codes (string or int — YAML's `8` is coerced to `"8"`):

```yaml
attributes:
  msg_type_filter: [D, 8]   # orders + executions only
```

## Why FIX matters

FIX is the global protocol for electronic trading — equities, fixed income, FX, derivatives. Every buyside firm, broker, exchange, and ECN speaks it. Trading-ops pipelines ingest FIX firehoses for compliance reporting, P&L attribution, execution analytics, regulatory TCA.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_fix_message_demo.sh | bash
cd fix-message-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
