#!/usr/bin/env bash
# Email Round-Trip demo — fan out notifications via SMTP, then pull replies via IMAP.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   notifications      ← synthetic_data_generator (alerts schema)
#         │
#         └── sent     ← smtp_send_asset (delivers to local aiosmtpd)
#                      ↓
#                  (local IMAP stub holds 6 reply messages)
#                      ↓
#   inbound_replies    ← imap_inbox_source (fetches replies)
#
# The demo runs against TWO local processes:
#   • aiosmtpd      — captures outbound emails to inspect
#   • imap_stub.py  — serves 6 hardcoded reply messages on port 1143
#
# Both are launched + torn down by this script. No external services.
# For production: point host/port at smtp.gmail.com:587 + imap.gmail.com:993
# with app passwords (see component READMEs).

set -euo pipefail
PROJECT_DIR="${1:-email-roundtrip-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy tabulate aiosmtpd
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 components"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -1
$CLI add smtp_send_asset          --auto-install 2>&1 | tail -1
$CLI add imap_inbox_source        --auto-install 2>&1 | tail -1

for c in synthetic_data_generator smtp_send_asset imap_inbox_source; do
  CLS=$(grep -oE '^class\s+\w+\s*\(' "src/$PKG/components/$c/component.py" | awk '{print $2}' | tr -d '(' | tail -1)
  echo "from .component import $CLS" > "src/$PKG/components/$c/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/smtp_send_asset" "src/$PKG/defs/imap_inbox_source"
done

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" \
       "src/$PKG/defs/smtp_send_asset" \
       "src/$PKG/defs/imap_inbox_source"

# Write the IMAP stub server as a sibling helper (NOT in defs/ — it's a
# fixture, not a Dagster definition).
mkdir -p fixtures
cat > fixtures/imap_stub.py <<'PYEOF'
"""Tiny IMAP4rev1 stub server. Serves a fixed set of messages.

Handles only what the imap_inbox_source component needs:
  LOGIN, SELECT, SEARCH (ALL|UNSEEN), UID FETCH (RFC822), STORE (\\Seen), LOGOUT.

Bind: 127.0.0.1:1143 (plain, no SSL — set use_ssl=false in defs.yaml).
"""
import re, socket, threading, sys
from email.utils import formatdate

REPLIES = [
    ("alice@example.com", "Re: Action required for CUST-1000",
     "Hi! Got it — I'll update billing today. Thanks!\n"),
    ("bob@example.com", "Re: Account issue C002",
     "Acknowledged. Please escalate to your team lead.\n"),
    ("carol@example.com", "Re: Multiple issues CUST-1002",
     "I disagree with one of the issues flagged. Calling support.\n"),
    ("dan@example.com", "OOO: Re: Pipeline alert",
     "I am out of office until next week. Please contact ops@me.com.\n"),
    ("eve@example.com", "Re: Pipeline alert",
     "Confirmed — already on it.\n"),
    ("frank@example.com", "Re: Reminder",
     "Can you re-send with the audit log attached?\n"),
]

def build_rfc822(from_addr, subject, body, uid):
    return (
        f"From: {from_addr}\r\n"
        f"To: bot@me.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {formatdate(localtime=True)}\r\n"
        f"Message-ID: <reply-{uid}@stub>\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    ).encode()


def handle_client(conn, addr):
    seen = set()
    selected = False
    try:
        conn.sendall(b"* OK IMAP4rev1 stub ready\r\n")
        while True:
            data = conn.recv(8192)
            if not data:
                break
            for line in data.split(b"\r\n"):
                if not line:
                    continue
                try:
                    line_s = line.decode("utf-8", errors="replace")
                except Exception:
                    continue
                parts = line_s.split(" ", 2)
                if len(parts) < 2:
                    continue
                tag, cmd = parts[0], parts[1].upper()
                arg = parts[2] if len(parts) > 2 else ""

                if cmd == "CAPABILITY":
                    conn.sendall(b"* CAPABILITY IMAP4rev1\r\n")
                    conn.sendall(f"{tag} OK CAPABILITY completed\r\n".encode())
                elif cmd == "LOGIN":
                    conn.sendall(f"{tag} OK LOGIN completed\r\n".encode())
                elif cmd == "SELECT":
                    selected = True
                    n = len(REPLIES)
                    conn.sendall(f"* {n} EXISTS\r\n".encode())
                    conn.sendall(b"* 0 RECENT\r\n")
                    conn.sendall(b"* FLAGS (\\Seen \\Deleted \\Flagged \\Answered \\Draft)\r\n")
                    conn.sendall(f"{tag} OK [READ-WRITE] SELECT completed\r\n".encode())
                elif cmd == "SEARCH":
                    if not selected:
                        conn.sendall(f"{tag} BAD no mailbox selected\r\n".encode())
                        continue
                    criteria = arg.upper().strip()
                    if "UNSEEN" in criteria:
                        ids = [str(i + 1) for i in range(len(REPLIES)) if (i + 1) not in seen]
                    else:
                        ids = [str(i + 1) for i in range(len(REPLIES))]
                    conn.sendall(f"* SEARCH {' '.join(ids)}\r\n".encode())
                    conn.sendall(f"{tag} OK SEARCH completed\r\n".encode())
                elif cmd == "FETCH":
                    # arg: "<seq> (RFC822)" or similar
                    m = re.match(r"(\d+)\s+\((.+)\)", arg)
                    if not m:
                        conn.sendall(f"{tag} BAD FETCH parse error\r\n".encode())
                        continue
                    seq = int(m.group(1))
                    if seq < 1 or seq > len(REPLIES):
                        conn.sendall(f"{tag} BAD FETCH out of range\r\n".encode())
                        continue
                    fr, subj, body = REPLIES[seq - 1]
                    payload = build_rfc822(fr, subj, body, seq)
                    header = f"* {seq} FETCH (RFC822 {{{len(payload)}}}\r\n".encode()
                    conn.sendall(header)
                    conn.sendall(payload)
                    conn.sendall(b")\r\n")
                    conn.sendall(f"{tag} OK FETCH completed\r\n".encode())
                elif cmd == "STORE":
                    # mark \Seen
                    m = re.match(r"(\d+)\s+", arg)
                    if m:
                        seen.add(int(m.group(1)))
                    conn.sendall(f"{tag} OK STORE completed\r\n".encode())
                elif cmd == "LOGOUT":
                    conn.sendall(b"* BYE logging out\r\n")
                    conn.sendall(f"{tag} OK LOGOUT completed\r\n".encode())
                    return
                elif cmd == "CLOSE":
                    conn.sendall(f"{tag} OK CLOSE completed\r\n".encode())
                else:
                    conn.sendall(f"{tag} BAD unknown command {cmd}\r\n".encode())
    finally:
        conn.close()


def run(host="127.0.0.1", port=1143):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    print(f"IMAP stub listening on {host}:{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    run()
PYEOF

# 1. Synthetic notification rows
mkdir -p "src/$PKG/defs/notifications"
cat > "src/$PKG/defs/notifications/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: notifications
  schema_type: support_tickets
  row_count: 5
  random_state: 42
  group_name: outbound
EOF

# 2. SMTP send — to local aiosmtpd (127.0.0.1:2525)
mkdir -p "src/$PKG/defs/sent"
cat > "src/$PKG/defs/sent/defs.yaml" <<EOF
type: $PKG.components.smtp_send_asset.component.SmtpSendAssetComponent
attributes:
  asset_name: sent
  upstream_asset_key: notifications
  host: 127.0.0.1
  port: 2525
  use_ssl: false
  use_starttls: false      # local stub doesn't speak TLS
  username_env_var: SMTP_USER
  password_env_var: SMTP_PASS
  sender: "Pipeline Bot <pipeline-bot@me.com>"
  mode: per_row
  to_template: "support+{ticket_id}@me.com"
  subject_template: "[Ticket {ticket_id}] {category} — {priority}"
  body_template: |
    Hi support team,

    Customer report:
      Ticket: {ticket_id}
      Category: {category}
      Priority: {priority}
      Description: {description}

    Please triage.
  group_name: outbound
EOF

# 3. IMAP fetch — from local stub (127.0.0.1:1143)
mkdir -p "src/$PKG/defs/inbound_replies"
cat > "src/$PKG/defs/inbound_replies/defs.yaml" <<EOF
type: $PKG.components.imap_inbox_source.component.ImapInboxSourceComponent
attributes:
  asset_name: inbound_replies
  host: 127.0.0.1
  port: 1143
  use_ssl: false             # local stub doesn't speak TLS
  username_env_var: IMAP_USER
  password_env_var: IMAP_PASS
  mailbox: INBOX
  search_criteria: ALL
  max_messages: 50
  mark_read: true
  group_name: inbound
EOF

cat <<MSG

>>> Setup complete (100% components — 3 wired into a 4-asset graph).

To exercise the round-trip end-to-end you need two background processes:

  Terminal 1 — local SMTP receiver (will capture outbound mail):
    cd $PROJECT_DIR
    uv run python -c "from aiosmtpd.controller import Controller; \\
import asyncio; \\
class H: \\
  async def handle_DATA(self,s,e,env): print(f'Got: from={env.mail_from} to={env.rcpt_tos}'); return '250 OK'; \\
c=Controller(H(), hostname='127.0.0.1', port=2525); c.start(); \\
import time; time.sleep(3600)"

  Terminal 2 — local IMAP stub (serves 6 reply messages):
    cd $PROJECT_DIR
    uv run python fixtures/imap_stub.py

  Terminal 3 — materialize the pipeline:
    cd $PROJECT_DIR
    export SMTP_USER=fake SMTP_PASS=fake IMAP_USER=fake IMAP_PASS=fake
    uv run dg launch --assets '*'

Expected:
  • notifications: 5 synthetic ticket rows
  • sent: 5 emails delivered to the local SMTP receiver
  • inbound_replies: 6 reply messages parsed (subjects, from, bodies)

To go to production:
  • Point sent's host:port at smtp.gmail.com:587 (use_starttls:true)
  • Point inbound_replies' host:port at imap.gmail.com:993 (use_ssl:true)
  • Set SMTP_USER / SMTP_PASS / IMAP_USER / IMAP_PASS env vars to a real
    Gmail address + app password.
MSG
