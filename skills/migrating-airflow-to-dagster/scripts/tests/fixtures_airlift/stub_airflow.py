"""A stand-in for Airflow's /api/v1 REST API: two DAGs, three tasks, no runs. Enough for dagster-airlift to build its definitions."""
import http.server, json, threading, re
DAGS = {"orders": ["extract", "load"], "reports": ["render"]}
def dag(d): return {"dag_id": d, "is_paused": False, "is_active": True, "fileloc": f"/opt/airflow/dags/{d}.py", "file_token": f"tok_{d}", "owners": ["airflow"], "description": None, "tags": [], "timetable_description": "At 00:00", "schedule_interval": None, "next_dagrun": None}
def task(d, t, up=()): return {"task_id": t, "downstream_task_ids": [], "dag_id": d, "operator_name": "PythonOperator", "class_ref": {"class_name": "PythonOperator"}, "retries": 0, "owner": "airflow"}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]; out = None
        if p == "/api/v1/dags": out = {"dags": [dag(d) for d in DAGS], "total_entries": len(DAGS)}
        elif (m := re.fullmatch(r"/api/v1/dags/(\w+)/tasks", p)): out = {"tasks": [task(m[1], t) for t in DAGS[m[1]]]}
        elif p == "/api/v1/datasets": out = {"datasets": [], "total_entries": 0}
        elif p == "/api/v1/variables": out = {"variables": [], "total_entries": 0}
        if out is None: self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(out).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"dag_runs": [], "task_instances": [], "total_entries": 0}).encode())
    def log_message(self, *a): pass
def serve():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start(); return s
