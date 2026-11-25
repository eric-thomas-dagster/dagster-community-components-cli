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
    files: dict = {}
    call, _notes = emit(Pipeline("p", "p"), st, files)
    import types
    scr = types.SimpleNamespace()
    for rel, src in files.items():                      # JavaScript evaluators come back as transpiled Python modules
        mod = types.ModuleType(rel)
        exec(compile(src.replace("from ..jsrt import", "from streamsets_to_dagster.jsrt import"), rel, "exec"), mod.__dict__)
        setattr(scr, rel.split("/")[-1][:-3], mod)
    run = Run(None, "t", {"C": 1})
    got = {o: [] for o in outputs}
    ns = {"rt": rt, "fx": fx, "run": run, "C": {}, "scr": scr}
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


# ---- stages whose behaviour was only documented (verified on Data Collector 6.4.1) -------------------------------------------------------------

def theirs_svc(sdc, cls, conf, records, service_conf, outputs=("o2",)):
    raw = "\n".join(json.dumps(r) for r in records)
    stages = [sdc.stage("RawDataDSource", "Origin", {"rawData": raw, "stopAfterFirstBatch": True}, outputs=["o1"]),
              sdc.stage(cls, "Stage", conf, inputs=["o1"], outputs=list(outputs), service_conf=service_conf), sdc.stage("NullDTarget", "Trash", inputs=["o2"])]
    out = sdc.preview(stages)["Stage"]
    keep = lambda a: {k: v for k, v in a.items() if not k.startswith("Info[")}      # noqa: E731
    return {lane: [(r["value"], keep(r["attributes"])) for r in recs] for lane, recs in out["output"].items()}, [r["error"] for r in out["errors"]]


def test_field_merger_semantics(sdc):
    recs = [{"a": 1, "b": 2, "m": {"x": 9, "y": 1}, "t": {"x": 1, "z": 5}, "l": [1, 2], "l2": [3]}]
    for conf in ({"mergeMapping": [{"fromField": "/m", "toField": "/t"}], "overwriteExisting": True},
                 {"mergeMapping": [{"fromField": "/m", "toField": "/t"}], "overwriteExisting": False},
                 {"mergeMapping": [{"fromField": "/l", "toField": "/t"}]},
                 {"mergeMapping": [{"fromField": "/l", "toField": "/l2"}]},
                 {"mergeMapping": [{"fromField": "/m", "toField": "/m2"}]},
                 {"mergeMapping": [{"fromField": "/a", "toField": "/new"}]},
                 {"mergeMapping": [{"fromField": "/l", "toField": "/l2"}, {"fromField": "/m", "toField": "/t"}], "overwriteExisting": True}):
        same(sdc, "FieldMergerDProcessor", conf, recs)


def test_xml_parser_output_shape(sdc):
    xml = "<root><msg id='1'><t>Hi</t><n>3</n></msg><msg id='2'><t>Yo</t></msg></root>"
    base = {"configs.fieldPathToParse": "/xml", "configs.parsedFieldPath": "/doc"}
    same(sdc, "XmlParserDProcessor", base, [{"xml": xml, "keep": 1}])
    for how in ("FIRST_ONLY", "ALL_AS_LIST", "SPLIT_INTO_MULTIPLE_RECORDS"):
        same(sdc, "XmlParserDProcessor", {**base, "configs.xmlRecordElement": "msg", "configs.multipleValuesBehavior": how}, [{"xml": xml, "keep": 1}])
    same(sdc, "XmlParserDProcessor", base, [{"xml": "<r><m id='1'>text</m><m>plain</m><k a='x'/></r>"}])


def test_field_replacer_6x_semantics(sdc):
    if "conf.rules" not in {c["name"] for c in sdc.defs()["FieldReplacerDProcessor"]["configDefinitions"]}:
        pytest.skip("older Field Replacer schema")
    recs = [{"a": "x", "n": 3, "e": None, "s": "q"}]
    for conf in ({"conf.rules": [{"fields": "/a", "setToNull": True}]},
                 {"conf.rules": [{"fields": "/e", "setToNull": False, "replacement": "filled"}]},
                 {"conf.rules": [{"fields": "/nope", "setToNull": False, "replacement": "z"}], "conf.nonExistingFieldHandling": "ADD_FIELD"},
                 {"conf.rules": [{"fields": "/nope", "setToNull": False, "replacement": "z"}], "conf.nonExistingFieldHandling": "CONTINUE"},
                 {"conf.rules": [{"fields": "/nope", "setToNull": False, "replacement": "z"}], "conf.nonExistingFieldHandling": "TO_ERROR"},
                 {"conf.rules": [{"fields": "/s", "setToNull": False, "replacement": "${record:value('/n') + 1}"}]},
                 {"conf.rules": [{"fields": "/n", "setToNull": False, "replacement": "${str:toUpper(record:value('/a'))}"}]}):
        same(sdc, "FieldReplacerDProcessor", conf, recs)


def test_log_parser_field_names(sdc):
    common = '127.0.0.1 - frank [10/Oct/2000:13:55:36 -0700] "GET /apache_pb.gif HTTP/1.0" 200 2326'
    combined = common + ' "http://ref.example/" "Mozilla/5.0"'
    for label, line, svc in (("COMMON", common, {"logParserServiceConfig.logMode": "COMMON_LOG_FORMAT"}), ("COMBINED", combined, {"logParserServiceConfig.logMode": "COMBINED_LOG_FORMAT"}),
                             ("GROK", common, {"logParserServiceConfig.logMode": "GROK", "logParserServiceConfig.grokPatternList": ["%{COMMONAPACHELOG}"]}),
                             ("GROKC", combined, {"logParserServiceConfig.logMode": "GROK", "logParserServiceConfig.grokPatternList": ["%{COMBINEDAPACHELOG}"]})):
        a, aerr = theirs_svc(sdc, "LogParserDProcessor", {"fieldPathToParse": "/text", "parsedFieldPath": "/"}, [{"text": line}], svc)
        b, berr = ours(sdc, "LogParserDProcessor", {"fieldPathToParse": "/text", "parsedFieldPath": "/", **svc}, [{"text": line}])
        print("\nLOG", label, "\n  SDC :", json.dumps(a), aerr, "\n  ours:", json.dumps(b), berr)
        assert {k: sorted(v, key=str) for k, v in a.items()} == {k: sorted(v, key=str) for k, v in b.items()} or json.loads(json.dumps(a, sort_keys=True)) == json.loads(json.dumps(b, sort_keys=True)), label       # Java map order is not meaningful


def test_local_lookup(sdc):
    same(sdc, "LocalLookupDProcessor", {"conf.values": [{"key": "a", "value": "area"}, {"key": "b", "value": "bee"}], "conf.mode": "RECORD",
                                        "conf.lookups": [{"keyExpr": "${record:value('/a')}", "outputFieldPath": "/word"}]}, [{"a": "a"}, {"a": "zz"}])


def script_conf(sdc, cls, script, params=None, mode="BATCH", init=""):
    return {"script": script, "initScript": init, "destroyScript": "", "processingMode": mode, "scriptRecordType": "NATIVE_OBJECTS",
            "userParams": [{"key": k, "value": v} for k, v in (params or {}).items()]}


JS_CASES = {
    "copy_and_modify": "for (var i = 0; i < records.length; i++) { records[i].value.t = records[i].value.n * 2; records[i].value.u = records[i].value.a + '!'; output.write(records[i]); }",
    "types_of_results": "var v = records[0].value; v.i = v.n + 1; v.d = v.f * 2; v.dv = v.n / 2; v.s = String(v.n); v.b = v.n > 3; v.m = Math.floor(v.f); v.p = parseInt('42'); output.write(records[0]);",
    "new_records_and_attributes": "var r = sdcFunctions.createRecord('x'); r.value = {a: records[0].value.a, n: records[0].value.n}; r.attributes['k'] = 'v'; output.write(r);",
    "user_params": "records[0].value.p = sdc.userParams['GREETING'] + ' ' + records[0].value.a; output.write(records[0]);",
    "error_write": "for (var i = 0; i < records.length; i++) { if (records[i].value.n > 3) { error.write(records[i], 'too big'); } else { output.write(records[i]); } }",
    "nested_and_null": "var v = records[0].value; v.o.k2 = 'x'; v.nul = null; v.l.add(4); output.write(records[0]);",
    "java_collections": "var v = records[0].value; v.sz = v.l.size(); v.first = v.l.get(0); v.has = v.o.containsKey('k'); v.got = v.o.get('k'); v.o.put('new', 1); v.isEmpty = v.l.isEmpty(); v.keys = v.o.keySet().size(); output.write(records[0]);",
}


@pytest.mark.parametrize("name", sorted(JS_CASES))
def test_javascript_evaluator_matches(sdc, name):
    conf = script_conf(sdc, "JavaScriptDProcessor", JS_CASES[name], {"GREETING": "hi"})
    a, aerr = theirs(sdc, "JavaScriptDProcessor", conf, IN)
    b, berr = ours(sdc, "JavaScriptDProcessor", conf, IN)
    assert ordr(a) == ordr(b), f"\nData Collector: {json.dumps(a, default=str)[:700]}\nConverted     : {json.dumps(b, default=str)[:700]}"
    assert sum(berr.values()) == len(aerr), f"error records: Data Collector {aerr}, converted {berr}"


JY_CASES = {
    "copy_and_modify": "for record in records:\n    record.value['t'] = record.value['n'] * 2\n    output.write(record)\n",
    "user_params": "for record in records:\n    record.value['p'] = sdc.userParams['GREETING'] + ' ' + record.value['a']\n    output.write(record)\n",
    "error_write": "for record in records:\n    if record.value['n'] > 3:\n        error.write(record, 'too big')\n    else:\n        output.write(record)\n",
    "types": "for record in records:\n    v = record.value\n    v['i'] = v['n'] + 1\n    v['d'] = v['f'] * 2\n    v['dv'] = v['n'] / 2\n    v['s'] = str(v['n'])\n    output.write(record)\n",
}


@pytest.mark.parametrize("name", sorted(JY_CASES))
def test_jython_evaluator_matches(sdc, name):
    if "JythonDProcessor" not in sdc.defs():
        pytest.skip("no Jython library installed")
    conf = script_conf(sdc, "JythonDProcessor", JY_CASES[name], {"GREETING": "hi"})
    a, aerr = theirs(sdc, "JythonDProcessor", conf, IN)
    b, berr = ours(sdc, "JythonDProcessor", conf, IN)
    assert ordr(a) == ordr(b), f"\nData Collector: {json.dumps(a, default=str)[:700]}\nConverted     : {json.dumps(b, default=str)[:700]}"
    assert sum(berr.values()) == len(aerr), f"error records: Data Collector {aerr}, converted {berr}"
