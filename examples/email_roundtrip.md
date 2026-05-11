# Email Round-Trip — fan out via SMTP, pull replies via IMAP

**Validated end-to-end** against local SMTP + IMAP servers. 5 synthetic support tickets → 5 outbound notification emails (`smtp_send_asset`) + 6 reply emails pulled from a mailbox (`imap_inbox_source`).

```
notifications      ← synthetic_data_generator (support_tickets, 5 rows)
       │
       └── sent     ← smtp_send_asset (delivers to local aiosmtpd)
                    ↓
              (mailbox holds 6 reply messages)
                    ↓
inbound_replies      ← imap_inbox_source (fetches replies)
```

## Components covered (3)

| Component | What it does |
|---|---|
| [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | 5 synthetic support-ticket rows (`support_tickets` schema) |
| [`smtp_send_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/smtp_send_asset) | Send one email per upstream row via SMTP. Templates `{column}` placeholders into to/cc/subject/body. Multipart/alternative for text + HTML. |
| [`imap_inbox_source`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/imap_inbox_source) | Fetch from any IMAP-compliant mailbox. Auto-decodes RFC-2047 headers, multipart bodies, quoted-printable. |

## Live output

**`sent` summary**:

```
host       port  mode     messages_sent  messages_failed  dry_run
127.0.0.1  2525  per_row              5                0    False
```

**`inbound_replies` sample (6 rows)**:

| subject | from | sent_at | body_text |
|---|---|---|---|
| Re: Reminder | frank@example.com | 2026-05-11 19:34:53 | "Can you re-send with the audit log attached?" |
| Re: Pipeline alert | eve@example.com | 2026-05-11 19:34:53 | "Confirmed — already on it." |
| OOO: Re: Pipeline alert | dan@example.com | 2026-05-11 19:34:53 | "I am out of office until next week." |
| Re: Multiple issues CUST-1002 | carol@example.com | 2026-05-11 19:34:53 | "I disagree with one of the issues flagged." |
| Re: Account issue C002 | bob@example.com | 2026-05-11 19:34:53 | "Acknowledged. Please escalate to your team lead." |

## How the demo runs offline

Two background processes serve as fakes:

1. **aiosmtpd** (Python) — captures outbound emails on `127.0.0.1:2525`, prints them to stdout for inspection. No AUTH required.
2. **`fixtures/imap_stub.py`** — a minimal pure-Python IMAP4rev1 server on `127.0.0.1:1143`. Speaks just enough of the protocol (LOGIN / SELECT / SEARCH / FETCH / STORE / LOGOUT) to serve 6 hardcoded reply messages.

Together they make the demo fully runnable offline — no external service / API key / mailbox provider needed.

## Going to production

To switch from the local stubs to real mailboxes, change only the `host` / `port` / TLS settings in the two `defs.yaml` files and set real env vars:

**For Gmail**:
```yaml
# sent
host: smtp.gmail.com
port: 587
use_starttls: true
username_env_var: SMTP_USER          # your gmail address
password_env_var: SMTP_APP_PASSWORD  # NOT your gmail password — an app password
```

```yaml
# inbound_replies
host: imap.gmail.com
port: 993
use_ssl: true
username_env_var: IMAP_USER
password_env_var: IMAP_APP_PASSWORD
search_criteria: 'UNSEEN'
mark_read: true
```

**For Outlook/O365**:
```yaml
host: smtp.office365.com  # SMTP side
host: outlook.office365.com  # IMAP side
```

Note: Microsoft disabled IMAP basic auth by default in 2022. Your tenant admin has to opt in, OR use a future `outlook_inbox_source` component over Microsoft Graph.

**For self-hosted Postfix + Dovecot**: works directly.

## SMTP component handles auth-less local relays

Originally the component called `server.login()` unconditionally, which broke against aiosmtpd / mailpit / similar dev relays that don't advertise AUTH. Fixed during this demo: the component now checks `server.esmtp_features` for `'auth'` after EHLO and only authenticates when the server expects it.

## Run it

```bash
./setup_email_roundtrip_demo.sh
cd email-roundtrip-demo

# Terminal 1: SMTP receiver
uv run python -c "
from aiosmtpd.controller import Controller
import time
class H:
  async def handle_DATA(self,s,e,env): print(f'Got: from={env.mail_from} to={env.rcpt_tos}'); return '250 OK'
c=Controller(H(), hostname='127.0.0.1', port=2525); c.start()
time.sleep(3600)
"

# Terminal 2: IMAP stub
uv run python fixtures/imap_stub.py

# Terminal 3: materialize
export SMTP_USER=fake SMTP_PASS=fake IMAP_USER=fake IMAP_PASS=fake
uv run dg launch --assets '*'
```

Expected: 5 messages logged by aiosmtpd, 6 rows in `inbound_replies` with parsed subject / from / body.
