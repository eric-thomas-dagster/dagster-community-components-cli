"""Encrypt and Decrypt Fields against a real Data Collector (needs the crypto stage library; Data Collector 6.x run from a Control Hub deployment): its ciphertext is
readable here and ours is readable there. See tests/test_sdc_parity.py for how to point SDC_PARITY_URL at an engine."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
URL = os.environ.get("SDC_PARITY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set SDC_PARITY_URL to a running Data Collector")

import streamsets_to_dagster.runtime as rt   # noqa: E402

KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(scope="module")
def sdc():
    from sdc_client import Sdc
    c = Sdc(URL)
    if "FieldEncryptDProcessor" not in c.defs():
        pytest.skip("this Data Collector has no crypto stage library")
    return c


def run_stage(sdc, records, stage_cls, conf, pre=()):
    import sdc_client
    cap = []
    orig = sdc_client.plain
    sdc_client.plain = lambda rec: (cap.append(rec) or orig(rec))
    try:
        stages = [sdc.stage("RawDataDSource", "Origin", {"rawData": "\n".join(json.dumps(r) for r in records), "stopAfterFirstBatch": True}, outputs=["o1"]),
                  *pre,
                  sdc.stage(stage_cls, "Stage", conf, inputs=[pre[-1]["outputLanes"][0]] if pre else ["o1"], outputs=["o2"]),
                  sdc.stage("NullDTarget", "Trash", inputs=["o2"])]
        sdc.preview(stages)
    finally:
        sdc_client.plain = orig
    return [r for r in cap if r["header"].get("stagesPath", "").endswith(":Stage")]


def test_data_collector_ciphertext_is_readable_here(sdc):
    rec = {"ssn": "123-45-6789", "n": 42, "big": 12345678901, "f": 2.5, "b": True, "neg": -7}
    out = run_stage(sdc, [rec], "FieldEncryptDProcessor", {"conf.mode": "ENCRYPT", "conf.primaryKeyProvider": "USER", "conf.key": KEY, "conf.keyId": "k1", "conf.dataKeyCaching": False,
                                                          "conf.fieldPaths": ["/" + k for k in rec], "conf.context": [{"key": "purpose", "value": "pii"}]})
    fields = out[-1]["value"]["value"]
    assert all(v["type"] == "BYTE_ARRAY" for v in fields.values())
    got = {k: rt.decrypt_value(base64.b64decode(v["value"]), raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"}) for k, v in fields.items()}
    assert got == rec and type(got["n"]) is int and type(got["f"]) is float and got["b"] is True
    with pytest.raises(ValueError, match="context"):
        rt.decrypt_value(base64.b64decode(fields["ssn"]["value"]), raw_key_b64=KEY, key_id="k1", context={"purpose": "other"})


def test_our_ciphertext_is_readable_by_data_collector(sdc):
    mine = {"s": rt.encrypt_value("hello", raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"}), "n": rt.encrypt_value(42, raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"}),
            "f": rt.encrypt_value(2.5, raw_key_b64=KEY, key_id="k1", context={"purpose": "pii"})}
    to_bytes = sdc.stage("GroovyDProcessor", "ToBytes", {"script": "for (record in sdc.records) { record.value.each { k, v -> record.value[k] = java.util.Base64.getDecoder().decode(v) }\n"
                                                                    "  sdc.output.write(record) }", "processingMode": "BATCH"}, inputs=["o1"], outputs=["o1b"])
    out = run_stage(sdc, [{k: base64.b64encode(v).decode() for k, v in mine.items()}], "FieldEncryptDProcessor",
                    {"conf.mode": "DECRYPT", "conf.primaryKeyProvider": "USER", "conf.key": KEY, "conf.keyId": "k1", "conf.dataKeyCaching": False, "conf.fieldPaths": ["/s", "/n", "/f"],
                     "conf.context": [{"key": "purpose", "value": "pii"}]}, pre=[to_bytes])
    vals = out[-1]["value"]["value"]
    assert (vals["s"]["type"], vals["s"]["value"]) == ("STRING", "hello") and vals["n"]["type"] == "INTEGER" and vals["n"]["value"] == "42" and vals["f"]["type"] == "DOUBLE"
