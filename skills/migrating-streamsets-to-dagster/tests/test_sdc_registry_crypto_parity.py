"""The registry's FieldEncryptionComponent (compatibility: streamsets) against a real Data Collector: engine ciphertext is read by the component, and the component's is read by the engine."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
URL = os.environ.get("SDC_PARITY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set SDC_PARITY_URL to a running Data Collector")
pd = pytest.importorskip("pandas")
dg = pytest.importorskip("dagster")
comp = pytest.importorskip("dagster_community_components")

from test_sdc_crypto_parity import KEY, run_stage, sdc   # noqa: E402,F401


def _component(mode, fields, upstream, name, monkeypatch):
    monkeypatch.setenv("SDC_PARITY_KEY", KEY)
    return comp.FieldEncryptionComponent(asset_name=name, upstream_asset_key=upstream, mode=mode, fields=fields, key_source="raw_key", raw_key_env_var="SDC_PARITY_KEY",
                                         key_id="k1", compatibility="streamsets", encryption_context={"purpose": "pii"})


def _run(component, df, up):
    @dg.asset(name=up)
    def _src() -> pd.DataFrame:
        return df
    r = dg.materialize([_src, *component.build_defs(context=None).assets])
    assert r.success
    return r.asset_value(dg.AssetKey(component.asset_name))


def test_component_reads_what_the_engine_encrypted(sdc, monkeypatch):
    rec = {"ssn": "123-45-6789", "n": 42, "big": 12345678901, "f": 2.5, "b": True}
    out = run_stage(sdc, [rec], "FieldEncryptDProcessor", {"conf.mode": "ENCRYPT", "conf.primaryKeyProvider": "USER", "conf.key": KEY, "conf.keyId": "k1", "conf.dataKeyCaching": False,
                                                          "conf.fieldPaths": ["/" + k for k in rec], "conf.context": [{"key": "purpose", "value": "pii"}]})
    fields = out[-1]["value"]["value"]
    df = pd.DataFrame({k: [base64.b64decode(v["value"])] for k, v in fields.items()})
    got = _run(_component("decrypt", list(rec), "engine_ct", "dec", monkeypatch), df, "engine_ct")
    assert {k: got[k].iloc[0] for k in rec} == rec
    assert got["n"].dtype.kind == "i" and got["f"].dtype.kind == "f" and bool(got["b"].iloc[0]) is True      # types survive (pandas widens to numpy scalars)


def test_engine_reads_what_the_component_encrypted(sdc, monkeypatch):
    df = pd.DataFrame({"s": ["hello"], "n": [42], "f": [2.5]})
    enc = _run(_component("encrypt", ["s", "n", "f"], "plain", "enc", monkeypatch), df, "plain")
    to_bytes = sdc.stage("GroovyDProcessor", "ToBytes", {"script": "for (record in sdc.records) { record.value.each { k, v -> record.value[k] = java.util.Base64.getDecoder().decode(v) }\n"
                                                                    "  sdc.output.write(record) }", "processingMode": "BATCH"}, inputs=["o1"], outputs=["o1b"])
    row = {k: base64.b64encode(enc[k].iloc[0]).decode() for k in ("s", "n", "f")}
    out = run_stage(sdc, [row], "FieldEncryptDProcessor",
                    {"conf.mode": "DECRYPT", "conf.primaryKeyProvider": "USER", "conf.key": KEY, "conf.keyId": "k1", "conf.dataKeyCaching": False, "conf.fieldPaths": ["/s", "/n", "/f"],
                     "conf.context": [{"key": "purpose", "value": "pii"}]}, pre=[to_bytes])
    v = out[-1]["value"]["value"]
    assert (v["s"]["type"], v["s"]["value"]) == ("STRING", "hello") and v["n"]["type"] == "INTEGER" and v["n"]["value"] == "42" and v["f"]["type"] == "DOUBLE"


def test_field_hasher_hashes_native_bytes_and_the_registry_masker_does_not(sdc, monkeypatch):
    """Data Collector hashes a number's native bytes (int: 4 little-endian bytes), a string's UTF-8; `data_masking` hashes the text, so hashes of numeric columns do not match."""
    import hashlib
    import struct
    rec = {"n": 42, "f": 2.5, "b": True, "s": "42"}
    out = run_stage(sdc, [rec], "FieldHasherDProcessor", {"hasherConfig.inPlaceFieldHasherConfigs": [{"sourceFieldsToHash": ["/n", "/f", "/b", "/s"], "hashType": "MD5"}]})
    engine = {k: v["value"] for k, v in out[-1]["value"]["value"].items()}
    md5 = lambda b: hashlib.md5(b).hexdigest()      # noqa: E731
    assert engine == {"n": md5(struct.pack("<i", 42)), "f": md5(struct.pack("<d", 2.5)), "b": md5(b"\x01"), "s": md5(b"42")}        # the engine: native bytes
    masker = comp.DataMaskingComponent(asset_name="m", upstream_asset_key="src", rules=[{"column": c, "method": "hash", "algorithm": "md5"} for c in rec])
    got = _run(masker, pd.DataFrame({k: [v] for k, v in rec.items()}), "src")
    ours = {k: got[k].iloc[0] for k in rec}
    assert ours["s"] == engine["s"]                                                  # strings agree
    mismatched = sorted(k for k in ("n", "f", "b") if ours[k] != engine[k])
    pytest.xfail(f"registry data_masking hashes the text of non-string fields; mismatches on {mismatched} (0.10.188)") if mismatched else None
