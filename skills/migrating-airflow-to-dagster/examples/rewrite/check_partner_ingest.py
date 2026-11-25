"""Run the rewrite of `partner_ingest` against a local stand-in for the partner API (not the real partner) and compare its summary with what the Airflow DAG's own `reshape` code gives for the same rows.
    python3 check_partner_ingest.py     (needs dagster and requests)"""
import http.server
import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).parent
ROWS = [{"customer_id": "a", "amount": "10.5"}, {"customer_id": "b", "amount": 4}, {"customer_id": "a", "amount": 1.25}, {"amount": 99}]
state = {"polls": 0}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer FAKE-TOKEN-FOR-THE-TEST"
        if self.path == "/export/status":
            state["polls"] += 1
            body = {"status": "ready" if state["polls"] >= 2 else "pending"}        # pending once, then ready
        else:
            body = {"rows": ROWS}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(PARTNER_API_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}", PARTNER_API_PASSWORD="FAKE-TOKEN-FOR-THE-TEST")
sys.path.insert(0, str(HERE / "project" / "src"))
import dagster as dg  # noqa: E402
from rw_pkg.defs import partner_ingest as rw  # noqa: E402

rw.POLL_SECONDS = 0.05
res = dg.materialize([rw.partner_export_rows, rw.customer_summary, rw.summary_present])
assert res.success, "the rewrite failed"
value = res.asset_value(dg.AssetKey(["partner_ingest", "customer_summary"]))

# what the Airflow DAG's own reshape code computes for the same rows (copied from airflow_src/partner_ingest.py)
out = {}
for r in ROWS:
    if r.get("customer_id"):
        out[r["customer_id"]] = out.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
expected = {"customers": len(out), "total": round(sum(out.values()), 2), "by_customer": out}
assert value == expected, (value, expected)
assert state["polls"] == 2, "the wait loop should have polled until the status was ready"
print("ok: the rewrite waited for ready (2 polls), fetched the rows and produced", value)
