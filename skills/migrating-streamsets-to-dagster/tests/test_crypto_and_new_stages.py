import base64
import gzip
import json
import sys
import types

import pytest

import streamsets_to_dagster.runtime as rt
from streamsets_to_dagster.runtime import Record, Run, fx

KEY = base64.b64encode(bytes(range(32))).decode()


def collect(run, lane="out"):
    got = []
    run.add(rt.Stage("sink", lambda recs: got.extend(recs) or {}, inputs=[lane]))
    return got


def feed(run, records, lane="in"):
    run.add(rt.Stage("src", gen=lambda: iter([{lane: records}]), outputs=[lane]))


def test_encrypt_decrypt_round_trips_types_and_enforces_the_context():
    import decimal
    for v in ("secret", 42, 12345678901, 2.5, True, b"\x00\x01", decimal.Decimal("1.10")):
        blob = rt.encrypt_value(v, raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"})
        assert isinstance(blob, bytes) and blob != str(v).encode()
        assert rt.decrypt_value(blob, raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"}) == v
    blob = rt.encrypt_value("x", raw_key_b64=KEY, context={"purpose": "pii"})
    with pytest.raises(ValueError, match="context"):
        rt.decrypt_value(blob, raw_key_b64=KEY, context={"purpose": "other"})
    wrong = base64.b64encode(bytes(range(1, 33))).decode()
    with pytest.raises(Exception):
        rt.decrypt_value(blob, raw_key_b64=wrong)
    with pytest.raises(TypeError, match="dates and times"):
        rt.encrypt_value(__import__("datetime").date(2020, 1, 2), raw_key_b64=KEY)
    with pytest.raises(rt.SdcCaveat, match="16, 24 or 32"):
        rt.encrypt_value("x", raw_key_b64=base64.b64encode(b"short").decode())


def test_encrypting_a_field_in_a_pipeline_then_decrypting_it():
    run = Run(None, "t", {})
    rt.crypto_fields(run, "E_01", "ENCRYPT", ["/ssn"], key_fn=lambda: KEY, context={"a": "b"}, inputs=["in"], outputs=["mid"])
    mid = []
    run.add(rt.Stage("tap", lambda recs: mid.extend(r.copy() if False else Record(dict(r.value)) for r in recs) or {"m2": recs}, inputs=["mid"], outputs=["m2"]))
    rt.crypto_fields(run, "D_01", "DECRYPT", ["/ssn"], key_fn=lambda: KEY, context={"a": "b"}, inputs=["m2"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"ssn": "123-45-6789", "name": "ann"})])
    run.execute()
    assert isinstance(mid[0].value["ssn"], bytes) and mid[0].value["name"] == "ann"
    assert got[0].value == {"ssn": "123-45-6789", "name": "ann"}


def test_field_hasher_in_place():
    import hashlib
    run = Run(None, "t", {})
    rt.field_hasher(run, "H_01", [(["/a", "/m"], "SHA256"), (["/b"], "MD5")], inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"a": "x", "b": 7, "m": {"k": "y"}, "c": "keep", "n": None})])
    run.execute()
    v = got[0].value
    import struct
    assert v["a"] == hashlib.sha256(b"x").hexdigest() and v["b"] == hashlib.md5(struct.pack("<i", 7)).hexdigest()      # integers hash as 4 little-endian bytes (Data Collector 3.18.1)
    assert v["m"] == {"k": hashlib.sha256(b"y").hexdigest()} and v["c"] == "keep"


def test_field_mask_variants():
    run = Run(None, "t", {})
    rt.field_mask(run, "M_01", [(["/cc"], "REGEX", "(.*)([0-9]{4})", [2]), (["/v"], "VARIABLE_LENGTH", "", []), (["/f"], "FIXED_LENGTH", "", [])],
                  inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"cc": "4111111111111111", "v": "abc", "f": "zz"}), Record({"cc": "short", "v": "q", "f": "z"})])
    run.execute()
    assert got[0].value == {"cc": "xxxxxxxxxxxx1111", "v": "xxx", "f": "x" * 10}
    assert got[1].value["cc"] == "short"                                  # no match: left as it is (Data Collector 3.18.1)


def test_log_parsing_and_file_tail_reads_only_new_lines(tmp_path):
    log = tmp_path / "a.log"
    line = '127.0.0.1 - - [10/Oct/2000:13:55:36 -0700] "GET /a.gif HTTP/1.0" 200 2326\n'
    log.write_text(line + "garbage\n")
    run = Run(None, "t", {})
    rt.file_tail_origin(run, "T_01", [str(log)], "LOG", "COMMON_LOG_FORMAT", 100, outputs=["out"])
    got = collect(run)
    stats = run.execute()
    assert got[0].value["remoteHost"] == "127.0.0.1" and got[0].value["status"] == "200" and got[0].attrs["offset"] == "0" and stats["error_records"] == {"T_01": 1}
    with open(log, "a") as fh:
        fh.write(line.replace("200", "404"))
    run2 = Run(None, "t", {}, run.offsets_out)
    rt.file_tail_origin(run2, "T_01", [str(log)], "LOG", "COMMON_LOG_FORMAT", 100, outputs=["out"])
    got2 = collect(run2)
    run2.execute()
    assert [r.value["status"] for r in got2] == ["404"]
    g = rt.parse_log('1.2.3.4 - bob [10/Oct/2000:13:55:36 -0700] "GET /x HTTP/1.1" 200 -', "COMMONAPACHELOG")
    assert g["clientip"] == "1.2.3.4" and g["verb"] == "GET" and g["httpversion"] == "1.1" and "bytes" not in g


def test_spooldir_reads_gzipped_logs(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with gzip.open(d / "a.log.gz", "wt") as fh:
        fh.write('1.2.3.4 - - [10/Oct/2000:13:55:36 -0700] "GET /x HTTP/1.1" 200 5\n')
    run = Run(None, "t", {})
    rt.spooldir_origin(run, "S_01", str(d), "*.gz", "LOG", header=False, delimiter=",", outputs=["out"], events=[], log_mode="COMMONAPACHELOG")
    got = collect(run)
    run.execute()
    assert got[0].value["response"] == "200"


def test_raw_data_origin_reads_concatenated_json():
    run = Run(None, "t", {})
    rt.raw_data_origin(run, "R_01", '{"a": 1}\n{"a": 2}', "JSON", outputs=["out"])
    got = collect(run)
    run.execute()
    assert [r.value["a"] for r in got] == [1, 2]


def test_geolocation_looks_up_ips_in_a_mmdb(tmp_path):
    from mmdb_writer import MMDBWriter
    w = MMDBWriter(ip_version=4, database_type="GeoIP2-City", languages=["en"])
    w.insert_network(__import__("netaddr").IPSet(["81.2.0.0/16"]), {"location": {"latitude": 51.5, "longitude": -0.1}, "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                                                                   "city": {"names": {"en": "London"}}})
    db = tmp_path / "c.mmdb"
    w.to_db_file(str(db))
    run = Run(None, "t", {})
    rt.geolocation(run, "G_01", str(db), [("LATITUDE", "/ip", "/lat"), ("COUNTRY_NAME", "/ip", "/country"), ("CITY_NAME", "/ip", "/city")], "TO_ERROR", inputs=["in"], outputs=["out"])
    got = collect(run)
    feed(run, [Record({"ip": "81.2.69.160"}), Record({"ip": "10.0.0.1"})])
    stats = run.execute()
    assert got[0].value == {"ip": "81.2.69.160", "lat": 51.5, "country": "United Kingdom", "city": "London"} and stats["error_records"] == {"G_01": 1}


def fake_kafka(monkeypatch, messages):
    commits, produced = [], []

    class Msg:
        def __init__(self, i, v):
            self.i, self.v = i, v
        value = lambda self: self.v          # noqa: E731
        error = lambda self: None            # noqa: E731
        topic = lambda self: "t"             # noqa: E731
        partition = lambda self: 0           # noqa: E731
        offset = lambda self: self.i         # noqa: E731

    class Consumer:
        def __init__(self, conf):
            self.conf, self.queue = conf, [Msg(i, m) for i, m in enumerate(messages)]

        def subscribe(self, t):
            pass

        def consume(self, num_messages, timeout):
            out, self.queue = self.queue[:num_messages], self.queue[num_messages:]
            return out

        def commit(self, asynchronous=False):
            commits.append(self.conf["group.id"])

        def close(self):
            pass

    class Producer:
        def __init__(self, conf):
            pass

        def produce(self, topic, value):
            produced.append((topic, json.loads(value)))

        def flush(self):
            pass
    mod = types.ModuleType("confluent_kafka")
    mod.Consumer, mod.Producer = Consumer, Producer
    monkeypatch.setitem(sys.modules, "confluent_kafka", mod)
    return commits, produced


def test_kafka_consumer_drains_commits_after_success_and_producer_sends(monkeypatch):
    commits, produced = fake_kafka(monkeypatch, [b'{"a": 1}', b'{"a": 2}', b"not json"])
    run = Run(None, "t", {})
    rt.kafka_origin(run, "K_01", "b:9092", "g1", ["t"], "EARLIEST", 2, 10, outputs=["l"])
    rt.kafka_target(run, "P_01", "b:9092", lambda r: "out-topic", inputs=["l"])
    stats = run.execute()
    assert produced == [("out-topic", {"a": 1}), ("out-topic", {"a": 2})] and commits == ["g1"] and stats["error_records"] == {"K_01": 1}

    commits.clear()
    run2 = Run(None, "t", {})
    rt.kafka_origin(run2, "K_01", "b:9092", "g1", ["t"], "EARLIEST", 2, 10, outputs=["l"])
    boom = rt.Stage("boom", lambda recs: (_ for _ in ()).throw(RuntimeError("downstream failed")), inputs=["l"])
    run2.add(boom)
    with pytest.raises(RuntimeError):
        run2.execute()
    assert commits == []                                                      # nothing committed when the run fails


def test_time_zone_offset_function():
    import datetime
    assert fx.date_time_zone_offset(datetime.datetime(2020, 7, 1, tzinfo=datetime.timezone.utc), "America/New_York") == -4 * 3600 * 1000


def test_encrypt_decrypt_stage_translates_and_round_trips():
    import json
    from streamsets_to_dagster.convert import emit
    from streamsets_to_dagster.model import Pipeline, Stage
    def build(mode, conf_extra=None):
        conf = {"conf.mode": mode, "conf.primaryKeyProvider": "USER", "conf.key": "${K}", "conf.keyId": "k1", "conf.fieldPaths": ["/ssn", "/n"], "conf.context": [{"key": "purpose", "value": "pii"}]}
        st = Stage("E_01", "FieldEncryptDProcessor", "x_FieldEncryptDProcessor", "crypto-lib", "processor", ["in"], ["out"], [], {**conf, **(conf_extra or {})})
        return emit(Pipeline("p", "p"), st)
    (call, notes), (call2, _) = build("ENCRYPT"), build("DECRYPT")
    assert any("StreamSets-compatible" in x for x in notes)
    run = Run(None, "t", {"K": KEY})
    mid = []
    ns = {"rt": rt, "fx": fx, "run": run, "C": {"K": KEY}}
    eval(call, ns)
    run.add(rt.Stage("tap", lambda recs: mid.extend(Record(dict(r.value)) for r in recs) or {"m2": recs}, inputs=["out"], outputs=["m2"]))
    eval(call2.replace("inputs=['in'], outputs=['out']", "inputs=['m2'], outputs=['out2']").replace("'E_01'", "'E_02'"), ns)
    got = collect(run, "out2")
    feed(run, [Record({"ssn": "123-45-6789", "n": 42})])
    run.execute()
    assert isinstance(mid[0].value["ssn"], bytes) and got[0].value == {"ssn": "123-45-6789", "n": 42}
