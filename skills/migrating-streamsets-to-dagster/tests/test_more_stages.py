"""JavaScript, HTTP, Jython-origin, Local Lookup and date-conversion stages, each run against real (local) endpoints."""
import http.server
import json
import threading

import pytest

from streamsets_to_dagster.runtime import Record, Run, fx, jython_origin, local_lookup, parse_date
import streamsets_to_dagster.runtime as rt


def collect(run, lane="out"):
    got = []
    rt.Stage  # noqa: B018
    run.add(rt.Stage("sink", lambda recs: got.extend(recs) or {}, inputs=[lane]))
    return got


def feed(run, records, lane="in"):
    run.add(rt.Stage("src", gen=lambda: iter([{lane: records}]), outputs=[lane]))


def test_javascript_evaluator_builds_new_records_from_batches_with_state():
    run = Run(None, "t", {})
    script = """
    for (var i = 0; i < records.length; i++) {
      var r = sdcFunctions.createRecord(records[i].sourceId + ':new');
      r.value = {n: records[i].value.n * state.factor, src: records[i].sourceId};
      r.attributes['k'] = 'v';
      output.write(r);
    }
    state.seen = (state.seen || 0) + records.length;
    """
    rt.js_processor(run, "JS_01", script, "state.factor = 3;", inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"n": 1}), Record({"n": 2})])
    run.execute()
    assert [r.value["n"] for r in got] == [3, 6] and got[0].attrs == {"k": "v"} and got[0].value["src"] == "JS_01:0"


def test_javascript_errors_and_missing_java_fail_loudly():
    run = Run(None, "t", {})
    rt.js_processor(run, "JS_01", "var x = Java.type('java.lang.String');", "", inputs=["in"], outputs=["out"])
    collect(run)
    feed(run, [Record({})])
    with pytest.raises(Exception, match="Java"):
        run.execute()


class _H(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"echo": self.path, "auth": self.headers.get("Authorization")}).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _H.seen.append((self.path, self.headers.get("Content-Type"), body.decode()))
        self.send_response(500 if self.path == "/fail" else 200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _H.seen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_http_processor_gets_json_into_a_field(server):
    run = Run(None, "t", {})
    rt.http_processor(run, "H_01", lambda r=None, f=None: f"{server}/q?id={r.value['id']}", "GET", None, lambda r=None, f=None: {}, lambda: ("u", "p"),
                      "/response", inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"id": 7})])
    run.execute()
    assert got[0].value["response"]["echo"] == "/q?id=7" and got[0].value["response"]["auth"].startswith("Basic ")


def test_http_target_sends_an_array_per_batch_and_errors_the_records_on_failure(server):
    run = Run(None, "t", {})
    rt.http_target(run, "T_01", lambda r=None, f=None: f"{server}/in", "POST", lambda r=None, f=None: {}, lambda: None, "ARRAY_OBJECTS", True, inputs=["in"])
    feed(run, [Record({"a": 1}), Record({"a": 2})])
    stats = run.execute()
    assert _H.seen == [("/in", "application/json", '[{"a": 1}, {"a": 2}]')] and not stats["error_records"]

    run2 = Run(None, "t", {})
    rt.http_target(run2, "T_02", lambda r=None, f=None: f"{server}/fail", "POST", lambda r=None, f=None: {}, lambda: None, "MULTIPLE_OBJECTS", False, inputs=["in"])
    feed(run2, [Record({"a": 1}), Record({"a": 2})])
    assert run2.execute()["error_records"] == {"T_02": 2}


def test_jython_origin_runs_one_poll_with_offsets():
    script = ("global sdc\nimport time\noffset = int(sdc.lastOffsets.get('') if sdc.lastOffsets.containsKey('') else sdc.userParams['START'])\n"
              "while True:\n    if sdc.isStopped():\n        break\n    b = sdc.createBatch()\n    r = sdc.createRecord('x')\n    r.value = {'at': offset}\n"
              "    offset += int(sdc.userParams['STEP'])\n    b.add(r)\n    b.process('', str(offset))\n")
    run = Run(None, "t", {})
    rt.jython_origin(run, "J_01", script, {"START": "100", "STEP": "10"}, outputs=["out"])
    got = collect(run)
    run.execute()
    assert [r.value for r in got] == [{"at": 100}] and run.offsets_out["J_01"] == {"": "110"}
    run2 = Run(None, "t", {}, run.offsets_out)
    rt.jython_origin(run2, "J_01", script, {"START": "100", "STEP": "10"}, outputs=["out"])
    got2 = collect(run2)
    run2.execute()
    assert [r.value for r in got2] == [{"at": 110}]


def test_local_lookup_sets_the_field_or_null():
    run = Run(None, "t", {})
    local_lookup(run, "L_01", {"a": "area"}, [(lambda r: r.value["k"], "/word")], inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"k": "a"}), Record({"k": "zz"})])
    run.execute()
    assert [r.value["word"] for r in got] == ["area", None]


def test_java_date_patterns_and_el_time_functions():
    assert parse_date("10/Oct/2000:13:55:36 -0700", "dd/MMM/y:H:m:s Z").year == 2000
    assert fx.date_time_to_ms(parse_date("2020-01-02 00:00:00", "yyyy-MM-dd HH:mm:ss")) == 1577923200000
    assert fx.extract_string_from_date(parse_date("2020-01-02", "yyyy-MM-dd"), "yyyy/MM/dd") == "2020/01/02"


def test_javascript_events_reach_the_event_lane():
    run = Run(None, "t", {})
    rt.js_processor(run, "JS_01", "for (var i=0;i<records.length;i++){output.write(records[i]);}\n"
                    "var e = sdc.createEvent('interval processed', 1); e.value = {watermark: 5}; sdc.toEvent(e);",
                    "", inputs=["in"], outputs=["out"], events=["ev"])
    got, evs = collect(run), []
    run.add(rt.Stage("evsink", lambda recs: evs.extend(recs) or {}, inputs=["ev"]))
    feed(run, [Record({"a": 1})])
    run.execute()
    assert len(got) == 1 and evs[0].value == {"watermark": 5} and evs[0].event == "interval processed"
