"""JDBC lookup, Dev Simulator, Groovy plumbing, and the smaller processors (Base64, replace-if-null, pivot, dedup, JSON/log parsers)."""
import json
import os
import shutil
import stat
import textwrap

import pytest

import streamsets_to_dagster.runtime as rt
from streamsets_to_dagster.runtime import Record, Run


def collect(run, lane="out", name="sink"):
    got = []
    run.add(rt.Stage(name, lambda recs: got.extend(recs) or {}, inputs=[lane]))
    return got


def feed(run, records, lane="in"):
    run.add(rt.Stage("src", gen=lambda: iter([{lane: records}]), outputs=[lane]))


def test_jdbc_lookup_all_behaviours(tmp_path):
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path / 'l.db'}"
    with sa.create_engine(url).begin() as c:
        c.execute(sa.text("CREATE TABLE city (id INTEGER, name TEXT)"))
        c.execute(sa.text("INSERT INTO city VALUES (1,'Oslo'),(1,'Bergen'),(2,'Lima')"))
    q = lambda r: f"SELECT name FROM city WHERE id = {r.value['id']} ORDER BY name"       # noqa: E731

    def run_with(multiple, missing, recs):
        run = Run(None, "t", {})
        rt.jdbc_lookup(run, "L_01", lambda: url, q, [("name", "/city")], multiple, missing, inputs=["in"], outputs=["out"])
        got = collect(run)
        feed(run, recs)
        return got, run.execute()
    got, _ = run_with("FIRST_ONLY", "PASS_RECORD_ON", [Record({"id": 1}), Record({"id": 9})])
    assert [r.value for r in got] == [{"id": 1, "city": "Bergen"}, {"id": 9}]
    got, _ = run_with("SPLIT_INTO_MULTIPLE_RECORDS", "PASS_RECORD_ON", [Record({"id": 1})])
    assert [r.value["city"] for r in got] == ["Bergen", "Oslo"]
    got, _ = run_with("ALL_AS_LIST", "PASS_RECORD_ON", [Record({"id": 1})])
    assert got[0].value["city"] == ["Bergen", "Oslo"]
    got, stats = run_with("FIRST_ONLY", "SEND_TO_ERROR", [Record({"id": 9})])
    assert got == [] and stats["error_records"] == {"L_01": 1}


def test_dev_simulator_replays_files_routes_by_discriminator_and_stamps_events(tmp_path):
    d = tmp_path / "sim"
    d.mkdir()
    (d / "online_1.csv").write_text("kind,delay_from_start_ms,item\nPerson,0,a\nPerson,1500,b\nOrder,10,c\n")
    run = Run(None, "t", {})
    rt.dev_simulator_origin(run, "D_01", str(d), "*online*.csv", [("Person", "pl"), ("Order", "ol")], "/kind", "/delay_from_start_ms", "/EventTimestamp",
                            delimiter=",", quote="'", outputs=["pl", "ol"])
    people, orders = collect(run, "pl", "s1"), collect(run, "ol", "s2")
    run.execute()
    assert [r.value["item"] for r in people] == ["a", "b"] and [r.value["item"] for r in orders] == ["c"]
    assert people[1].value["EventTimestamp"] - people[0].value["EventTimestamp"] == 1500


def test_small_processors():
    import base64
    run = Run(None, "t", {})
    rt.base64_field(run, "B_01", "ENCODE", "/a", "/b", False, inputs=["in"], outputs=["m"])
    rt.base64_field(run, "B_02", "DECODE", "/b", "/c", False, inputs=["m"], outputs=["m2"])
    rt.replace_if_null(run, "R_01", [(["/n"], "dflt")], inputs=["m2"], outputs=["m3"])
    rt.list_pivot(run, "P_01", "/items", True, None, inputs=["m3"], outputs=["m4"])
    rt.dedup(run, "D_01", 100, ["/items"], inputs=["m4"], outputs=["u", "d"])
    uniq, dups = collect(run, "u", "s1"), collect(run, "d", "s2")
    feed(run, [Record({"a": b"hi", "n": None, "keep": 1, "items": [1, 2, 1]})])      # Base64 takes byte arrays only
    run.execute()
    assert [r.value["items"] for r in uniq] == [1, 2] and [r.value["items"] for r in dups] == [1]
    v = uniq[0].value
    assert v["b"] == base64.b64encode(b"hi") and v["c"] == b"hi" and v["n"] == "dflt" and v["keep"] == 1


def test_json_and_log_parsers():
    run = Run(None, "t", {})
    rt.json_parser(run, "J_01", "/raw", "/doc", inputs=["in"], outputs=["m"])
    rt.log_parser(run, "L_01", "/line", "/", "COMMONAPACHELOG", inputs=["m"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"raw": '{"a": [1]}', "line": '1.2.3.4 - - [10/Oct/2000:13:55:36 -0700] "GET /x HTTP/1.1" 200 5'})])
    run.execute()
    assert got[0].value["doc"] == {"a": [1]} and got[0].value["clientip"] == "1.2.3.4" and got[0].value["response"] == "200"


FAKE_GROOVY = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys
    runner, inp, out = sys.argv[1:4]
    cfg = json.load(open(inp))
    assert open(runner).read().strip(), "runner script must be passed"
    state = cfg["state"] or {}
    if cfg.get("init"):
        state["inited"] = state.get("inited", 0) + 1
    state["batches"] = state.get("batches", 0) + 1
    recs = [{"value": {**r["value"], "seen_by": open(cfg["script"]).read().strip(), "n": state["batches"]}, "attributes": r["attributes"]} for r in cfg["records"]]
    json.dump({"out": recs, "err": [[recs[0], "boom"]] if cfg["records"] and cfg["records"][0]["value"].get("bad") else [], "ev": [], "state": state}, open(out, "w"))
''')


def fake_groovy(tmp_path, monkeypatch):
    exe = tmp_path / "groovy"
    exe.write_text(FAKE_GROOVY)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SDC_GROOVY", str(exe))


def test_groovy_processor_protocol_state_init_and_errors(tmp_path, monkeypatch):
    fake_groovy(tmp_path, monkeypatch)
    run = Run(None, "t", {})
    rt.groovy_processor(run, "G_01", "// the script", "// init", "BATCH", inputs=["in"], outputs=["out"])
    got = collect(run)
    run.add(rt.Stage("src", gen=lambda: iter([{"in": [Record({"a": 1})]}, {"in": [Record({"bad": 1}), Record({"a": 2})]}]), outputs=["in"]))
    stats = run.execute()
    assert [r.value["n"] for r in got] == [1, 2, 2] and got[0].value["seen_by"] == "// the script" and stats["error_records"] == {"G_01": 1}


def test_groovy_processor_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SDC_GROOVY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    run = Run(None, "t", {})
    rt.groovy_processor(run, "G_01", "x", "", "BATCH", inputs=["in"], outputs=["out"])
    collect(run)
    feed(run, [Record({})])
    with pytest.raises(rt.SdcCaveat, match="needs the `groovy` command"):
        run.execute()


@pytest.mark.skipif(not (os.environ.get("SDC_GROOVY") or shutil.which("groovy")), reason="needs a JDK and Apache Groovy")
def test_the_real_groovy_script_from_the_aws_sample_matches_the_hand_ported_one(fix):
    """Runs the export's own Groovy script on the JVM and compares it with the Python port in tests/overrides/forward_fill.py."""
    from streamsets_to_dagster.loader import load
    stage = load(fix).pipelines["AWS Marketplace Data"].stage("GroovyEvaluator_01")
    recs = lambda: [Record({"id": "A", "x": "1", "y": "2"}, {"first_field": "id"}), Record({"id": "", "x": "", "y": "9"}, {"first_field": "id"}),   # noqa: E731
                    Record({"id": "B", "x": "5", "y": ""}, {"first_field": "id"}), Record({"id": "", "x": "", "y": ""}, {"first_field": "id"})]
    run = Run(None, "t", {})
    rt.groovy_processor(run, "G_01", stage.conf["script"], stage.conf.get("initScript") or "", "BATCH", inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, recs())
    run.execute()
    import importlib.util
    spec = importlib.util.spec_from_file_location("ff", os.path.join(os.path.dirname(__file__), "overrides", "forward_fill.py"))
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    run2 = Run(None, "t", {})
    rt.override(run2, "G_01", ff.process, inputs=["in"], outputs=["out"])
    want = collect(run2)
    feed(run2, recs())
    run2.execute()
    assert [r.value for r in got] == [r.value for r in want]


def test_flatten_entire_record():
    run = Run(None, "t", {})
    rt.field_flattener(run, "F_01", ["/"], ".", "/", True, inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"a": {"b": 1, "c": [10, {"d": 2}]}, "e": 3})])
    run.execute()
    assert got[0].value == {"a.b": 1, "a.c.0": 10, "a.c.1.d": 2, "e": 3}


CONTACTS = ("<contacts><contact><name type='maiden'>NAME1</name><phone>(111)111-1111</phone><phone>(222)222-2222</phone></contact>"
            "<contact><name type='maiden'>NAME2</name><phone>(333)333-3333</phone><phone>(444)444-4444</phone></contact></contacts>")


def flatten_xml(delimiter, ignore_attrs=False, keep=True):
    run = Run(None, "t", {})
    rt.xml_flatten(run, "X_01", "/xml", delimiter, ".", "#", ignore_attrs, keep, False, None, inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"xml": CONTACTS, "id": 7})])
    run.execute()
    return got


def test_xml_flattener_matches_the_documented_examples():
    two = flatten_xml("contact")
    assert len(two) == 2
    assert {k: v for k, v in two[0].value.items() if k not in ("xml", "id")} == {
        "contact.name": "NAME1", "contact.name#type": "maiden", "contact.phone(0)": "(111)111-1111", "contact.phone(1)": "(222)222-2222"}
    assert two[1].value["contact.phone(1)"] == "(444)444-4444" and two[1].value["id"] == 7
    one = flatten_xml(None)[0].value
    assert one["contacts.contact(0).name#type"] == "maiden" and one["contacts.contact(1).phone(0)"] == "(333)333-3333" and "contacts.contact(0).phone(1)" in one
    assert "contact.name#type" not in flatten_xml("contact", ignore_attrs=True)[0].value
    assert set(flatten_xml("contact", keep=False)[0].value) == {"contact.name", "contact.name#type", "contact.phone(0)", "contact.phone(1)"}
