"""Parity with a real Data Collector: the same records through the same stage configuration in Data Collector (REST preview) and in the converted runtime.

Skipped unless SDC_PARITY_URL points at a running Data Collector (docker run -d --ulimit nofile=65536:65536 -p 18632:18630 -e SDC_CONF_http_authentication=form
streamsets/datacollector:3.18.1 needs no activation; 6.x needs an activation).  A case is a stage class, a configuration (property names as in a pipeline export) and input records.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
URL = os.environ.get("SDC_PARITY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set SDC_PARITY_URL to a running Data Collector")

import streamsets_to_dagster.runtime as rt                                   # noqa: E402
from streamsets_to_dagster.convert import _Stub, emit                        # noqa: E402
from streamsets_to_dagster.model import Pipeline, Stage                      # noqa: E402
from streamsets_to_dagster.runtime import Record, Run, fx                    # noqa: E402

IN = [{"a": "x", "n": 3, "f": 2.5, "s": " Hello World ", "t": True, "l": [1, 2, 3], "o": {"k": "v", "d": {"deep": 1}}, "z": 0, "e": ""},
      {"a": "y", "n": 4, "f": -1.5, "s": "foo-bar", "t": False, "l": [], "o": {"k": "w"}, "z": 5, "e": "q"}]


@pytest.fixture(scope="module")
def sdc():
    from sdc_client import Sdc
    return Sdc(URL)


def theirs(sdc, cls, conf, records, outputs=("o2",), service_conf=None):
    raw = "\n".join(json.dumps(r) for r in records)
    lanes = [f"o{i}" for i in range(2, 2 + len(outputs))] if len(outputs) > 1 else ["o2"]
    stages = [sdc.stage("RawDataDSource", "Origin", {"rawData": raw, "stopAfterFirstBatch": True}, outputs=["o1"]),
              sdc.stage(cls, "Stage", conf, inputs=["o1"], outputs=list(outputs)),
              *[sdc.stage("NullDTarget", f"Trash{i}", inputs=[o]) for i, o in enumerate(outputs)]]
    out = sdc.preview(stages)["Stage"]
    keep = lambda a: {k: v for k, v in a.items() if not k.startswith("Info[")}      # noqa: E731 - Data Collector's own bookkeeping attributes (e.g. the dedup hash)
    return {lane: [(r["value"], keep(r["attributes"])) for r in recs] for lane, recs in out["output"].items()}, [r["error"] for r in out["errors"]]


def ours(sdc, cls, conf, records, outputs=("o2",)):
    cfg = {c["name"]: c.get("defaultValue") for c in sdc.defs()[cls]["configDefinitions"]}
    cfg.update(conf)
    st = Stage("Stage", cls, "x_" + cls, "lib", "processor", ["o1"], list(outputs), [], cfg)
    call, _notes = emit(Pipeline("p", "p"), st)
    run = Run(None, "t", {"C": 1})
    got = {o: [] for o in outputs}
    ns = {"rt": rt, "fx": fx, "run": run, "C": {}}
    eval(call, ns)
    run.add(rt.Stage("src", gen=lambda: iter([{"o1": [Record(json.loads(json.dumps(r)), {}) for r in records]}]), outputs=["o1"]))
    for o in outputs:
        run.add(rt.Stage(f"sink_{o}", lambda recs, o=o: got[o].extend((r.value, r.attrs) for r in recs) or {}, inputs=[o]))
    stats = run.execute()
    return got, stats["error_records"]


def ordr(x):
    """Compare field order as well as content."""
    if isinstance(x, dict):
        return [(k, ordr(v)) for k, v in x.items()]
    if isinstance(x, (list, tuple)):
        return [ordr(v) for v in x]
    return x


def same(sdc, cls, conf, records=IN, outputs=("o2",)):
    a, aerr = theirs(sdc, cls, conf, records, outputs)
    try:
        b, berr = ours(sdc, cls, conf, records, outputs)
    except _Stub as e:
        pytest.skip(f"not translated: {e}")
    assert ordr(b) == ordr(a), f"\nData Collector: {json.dumps(a, default=str)[:900]}\nConverted     : {json.dumps(b, default=str)[:900]}"
    assert sum(berr.values()) == len(aerr), f"error records: Data Collector {aerr}, converted {berr}"


def test_expression_arithmetic_strings_and_attributes(sdc):
    same(sdc, "ExpressionDProcessor", {
        "expressionProcessorConfigs": [
            {"fieldToSet": "/plus", "expression": "${record:value('/n') + 1}"},
            {"fieldToSet": "/concat", "expression": "${str:concat(record:value('/a'), '-z')}"},
            {"fieldToSet": "/upper", "expression": "${str:toUpper(str:trim(record:value('/s')))}"},
            {"fieldToSet": "/missing", "expression": "${record:value('/nope')}"},
            {"fieldToSet": "/cmp", "expression": "${record:value('/n') > 3}"},
            {"fieldToSet": "/tern", "expression": "${record:value('/t') ? 'yes' : 'no'}"},
            {"fieldToSet": "/sub", "expression": "${str:substring(record:value('/s'), 1, 4)}"},
            {"fieldToSet": "/mul", "expression": "${record:value('/f') * 2}"},
            {"fieldToSet": "/exists", "expression": "${record:exists('/o/k')}"},
        ],
        "headerAttributeConfigs": [{"attributeToSet": "h", "headerAttributeExpression": "${record:value('/a')}"}]})


STR = [{"a": "12", "b": "3.5", "c": "true", "d": "abc", "w": " pad "}, {"a": "7", "b": "-1", "c": "false", "d": "xyz", "w": "ok"}]


def test_expression_functions_and_comparisons(sdc):
    same(sdc, "ExpressionDProcessor", {"expressionProcessorConfigs": [
        {"fieldToSet": "/ra", "expression": "${str:replaceAll(record:value('/s'), 'o', '0')}"},
        {"fieldToSet": "/m", "expression": "${str:matches(record:value('/s'), '.*World.*')}"},
        {"fieldToSet": "/m2", "expression": "${str:matches(record:value('/a'), 'x')}"},
        {"fieldToSet": "/cap", "expression": "${str:regExCapture(record:value('/s'), ' (\\\\w+) (\\\\w+) ', 2)}"},
        {"fieldToSet": "/cap2", "expression": "${str:regExCapture(record:value('/a'), '(x)', 1)}"},
        {"fieldToSet": "/has", "expression": "${str:contains(record:value('/s'), 'World')}"},
        {"fieldToSet": "/idx", "expression": "${str:indexOf(record:value('/s'), 'W')}"},
        {"fieldToSet": "/len", "expression": "${str:length(record:value('/s'))}"},
        {"fieldToSet": "/abs", "expression": "${math:abs(record:value('/f'))}"},
        {"fieldToSet": "/ceil", "expression": "${math:ceil(record:value('/f'))}"},
        {"fieldToSet": "/round", "expression": "${math:round(record:value('/f'))}"},
        {"fieldToSet": "/eqs", "expression": "${record:value('/n') == '3'}"},
        {"fieldToSet": "/andor", "expression": "${record:value('/n') > 3 || record:value('/a') == 'x' && record:value('/t')}"},
        {"fieldToSet": "/notempty", "expression": "${not empty record:value('/e')}"},
        {"fieldToSet": "/vod", "expression": "${record:valueOrDefault('/nope', 'dflt')}"},
        {"fieldToSet": "/div", "expression": "${record:value('/n') / 2}"},
        {"fieldToSet": "/mod", "expression": "${record:value('/n') % 2}"},
        {"fieldToSet": "/intdiv", "expression": "${record:value('/n') div 2}"},
        {"fieldToSet": "/nullcat", "expression": "${str:concat(record:value('/nope'), 'x')}"},
        {"fieldToSet": "/strnum", "expression": "${record:value('/a') + 1}"},
    ]})


def test_field_remover_keep_remove_nested(sdc):
    same(sdc, "FieldFilterDProcessor", {"filterOperation": "KEEP", "fields": ["/a", "/o/k"]})
    same(sdc, "FieldFilterDProcessor", {"filterOperation": "REMOVE", "fields": ["/a", "/o/d"]})


def test_field_renamer(sdc):
    same(sdc, "FieldRenamerDProcessor", {"renameMapping": [{"fromFieldExpression": "/a", "toFieldExpression": "/alpha"}, {"fromFieldExpression": "/o/k", "toFieldExpression": "/o/key"}]})


def test_field_type_converter(sdc):
    same(sdc, "FieldTypeConverterDProcessor", {"convertBy": "BY_FIELD", "fieldTypeConverterConfigs": [
        {"fields": ["/a"], "targetType": "INTEGER"}, {"fields": ["/b"], "targetType": "DOUBLE"}, {"fields": ["/c"], "targetType": "BOOLEAN"}, {"fields": ["/d"], "targetType": "STRING"}]}, STR)


def test_field_type_converter_long_decimal_and_failure(sdc):
    same(sdc, "FieldTypeConverterDProcessor", {"convertBy": "BY_FIELD", "fieldTypeConverterConfigs": [
        {"fields": ["/a"], "targetType": "LONG"}, {"fields": ["/d"], "targetType": "INTEGER"}]}, STR)


def test_splitter(sdc):
    same(sdc, "SplitterDProcessor", {"fieldPath": "/s", "separator": "-", "fieldPathsForSplits": ["/p1", "/p2"], "tooManySplitsAction": "TO_LAST_FIELD", "originalFieldAction": "REMOVE"})
    same(sdc, "SplitterDProcessor", {"fieldPath": "/s", "separator": "o", "fieldPathsForSplits": ["/p1", "/p2"], "tooManySplitsAction": "TO_LAST_FIELD", "originalFieldAction": "KEEP"})


def test_selector_two_lanes_and_default(sdc):
    from sdc_client import Sdc  # noqa: F401
    a, _ = theirs(sdc, "SelectorDProcessor", {"lanePredicates": [{"outputLane": "o2", "predicate": "${record:value('/n') > 3}"}, {"outputLane": "o3", "predicate": "default"}]}, IN, outputs=("o2", "o3"))
    b, _ = ours(sdc, "SelectorDProcessor", {"lanePredicates": [{"outputLane": "o2", "predicate": "${record:value('/n') > 3}"}, {"outputLane": "o3", "predicate": "default"}]}, IN, outputs=("o2", "o3"))
    assert ordr(a) == ordr(b), f"\nData Collector: {a}\nConverted     : {b}"


def test_flattener(sdc):
    same(sdc, "FieldFlattenerDProcessor", {"config.flattenType": "ENTIRE_RECORD", "config.nameSeparator": "."})
    same(sdc, "FieldFlattenerDProcessor", {"config.flattenType": "SPECIFIC_FIELDS", "config.fields": ["/o"], "config.flattenTargetField": "/", "config.removeFlattenedField": True, "config.nameSeparator": "."})
    same(sdc, "FieldFlattenerDProcessor", {"config.flattenType": "SPECIFIC_FIELDS", "config.fields": ["/o"], "config.flattenTargetField": "/out", "config.removeFlattenedField": True, "config.nameSeparator": "."})


def test_masker(sdc):
    recs = [{"cc": "4111111111111111", "name": "Alexander", "zip": "12345"}, {"cc": "short", "name": "Al", "zip": "123"}]
    same(sdc, "FieldMaskDProcessor", {"fieldMaskConfigs": [{"fields": ["/cc"], "maskType": "REGEX", "regex": "(.*)([0-9]{4})", "groupsToShow": "2"}]}, recs)
    same(sdc, "FieldMaskDProcessor", {"fieldMaskConfigs": [{"fields": ["/name"], "maskType": "VARIABLE_LENGTH"}]}, recs)
    same(sdc, "FieldMaskDProcessor", {"fieldMaskConfigs": [{"fields": ["/name"], "maskType": "FIXED_LENGTH"}]}, recs)
    same(sdc, "FieldMaskDProcessor", {"fieldMaskConfigs": [{"fields": ["/zip"], "maskType": "CUSTOM", "mask": "###-xx"}]}, recs)


def test_hasher_in_place(sdc):
    same(sdc, "FieldHasherDProcessor", {"hasherConfig.inPlaceFieldHasherConfigs": [{"sourceFieldsToHash": ["/a", "/n"], "hashType": "SHA256"}, {"sourceFieldsToHash": ["/s"], "hashType": "MD5"}]})


def test_field_order_and_dedup(sdc):
    same(sdc, "FieldOrderDProcessor", {"config.fields": ["/n", "/a"], "config.outputType": "LIST_MAP", "config.missingFieldAction": "TO_ERROR", "config.extraFieldAction": "DISCARD"})
    dup = [{"k": 1, "v": "a"}, {"k": 1, "v": "a"}, {"k": 2, "v": "a"}, {"k": 1, "v": "b"}]
    a, _ = theirs(sdc, "DeDupDProcessor", {"compareFields": "ALL_FIELDS"}, dup, outputs=("o2", "o3"))
    b, _ = ours(sdc, "DeDupDProcessor", {"compareFields": "ALL_FIELDS"}, dup, outputs=("o2", "o3"))
    assert ordr(a) == ordr(b), f"\nData Collector: {a}\nConverted     : {b}"


def test_base64_and_json_parser(sdc):
    conf = ({"fieldsToEncode": [{"originFieldPath": "/a", "resultFieldPath": "/enc"}], "urlSafe": False} if "fieldsToEncode" in sdc.defs()["Base64EncodingDProcessor"]["configDefinitions"][1]["name"]
            or any(k["name"] == "fieldsToEncode" for k in sdc.defs()["Base64EncodingDProcessor"]["configDefinitions"])
            else {"originFieldPath": "/a", "resultFieldPath": "/enc", "urlSafe": False})
    a, aerr = theirs(sdc, "Base64EncodingDProcessor", conf, [{"a": "hello"}])
    b, berr = ours(sdc, "Base64EncodingDProcessor", conf, [{"a": "hello"}])
    assert a == b == {"o2": []} and len(aerr) == 1 and sum(berr.values()) == 1          # a string field is a record error in both
    same(sdc, "JsonParserDProcessor", {"fieldPathToParse": "/raw", "parsedFieldPath": "/doc"}, [{"raw": '{"x": [1, 2], "y": {"z": "q"}}'}])


def test_list_pivot(sdc):
    same(sdc, "ListPivotDProcessor", {"listPath": "/l", "copyFields": True, "newPath": "", "replaceListField": True, "saveOriginalFieldName": False})
