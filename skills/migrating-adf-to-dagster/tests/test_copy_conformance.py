"""Copy-activity behavior stated in Microsoft's format and schema-mapping pages (tests/data/adf-format-*.md, adf-copy-activity-schema-and-type-mapping.md).

Each test names the documented rule it checks. The documentation's own mistakes are recorded where they occur.
"""
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from adf_to_dagster import runtime as rt
from adf_to_dagster.convert import PipeGen, _Stub, convert
from adf_to_dagster.loader import load
from adf_to_dagster.model import Factory, Pipeline

DATA = Path(__file__).parent / "data"


def ctx():
    return rt.Ctx("P", {}, {}, {}, log=lambda m: None)


def test_the_documented_rules_are_in_the_pages_we_rely_on():
    fmt = (DATA / "adf-format-delimited-text.md").read_text()
    assert "on write: \"\\r\\n\"" in fmt and "The default value is **empty string**" in fmt and "backslash" in fmt
    assert "case-sensitive manner" in (DATA / "adf-copy-activity-schema-and-type-mapping.md").read_text()


# ---- delimited text defaults ----------------------------------------------------------------------------------------------------------------

def test_copy_writes_crlf_row_delimiters_unless_the_dataset_says_otherwise(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.csv").write_text("a,b\n1,2\n")
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True}, str(tmp_path / "o"), "", "DelimitedText", {"header": True})
    assert (tmp_path / "o" / "a.csv").read_bytes() == b"a,b\r\n1,2\r\n"                       # "on write: \r\n"
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True}, str(tmp_path / "o2"), "", "DelimitedText", {"header": True, "row_delimiter": "\n"})
    assert (tmp_path / "o2" / "a.csv").read_bytes() == b"a,b\n1,2\n"


def test_an_empty_field_is_null_by_default_and_the_null_value_can_be_changed(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.csv").write_text("a,b\n1,\n2,NA\n")
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True}, str(tmp_path / "p"), "", "Parquet", {})
    df = pd.read_parquet(tmp_path / "p" / "a.parquet")
    assert df["b"].isna().tolist() == [True, False] and df["b"].iloc[1] == "NA"                  # nullValue defaults to the empty string: an empty field is null
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True, "null_value": "NA"}, str(tmp_path / "q"), "", "Parquet", {})
    q = pd.read_parquet(tmp_path / "q" / "a.parquet")["b"]
    assert q.isna().tolist() == [False, True] and q.iloc[0] == ""                              # with nullValue NA, NA is null and an empty field is an empty string
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "p"), "", "Parquet", {}, str(tmp_path / "r"), "", "DelimitedText", {"header": True, "null_value": "\\N"})
    assert (tmp_path / "r" / "a.csv").read_bytes() == b"a,b\r\n1,\\N\r\n2,NA\r\n"               # nulls are written as the sink's nullValue


def test_encodings_resolve_or_the_copy_is_a_stub():
    g = PipeGen(Factory(), Pipeline("p"))
    assert g._encoding("UTF-8") == "utf-8" and g._encoding("UTF-8 without BOM") == "utf-8" and g._encoding("ISO-8859-1") == "iso8859-1"
    assert g._encoding("UTF-16BE") == "utf-16-be" and g._encoding("WINDOWS-1252") == "cp1252" and g._encoding("SHIFT-JIS") == "shift_jis"
    with pytest.raises(_Stub):
        g._encoding("EBCDIC-NOT-A-THING")


def test_a_header_less_file_names_its_columns_prop_n_and_maps_by_ordinal(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.csv").write_text("1,Ann,x\n2,Bob,y\n")
    db = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table t (id integer, name text)"))
    import os
    os.environ["ADF_CONN_X"] = f"sqlite:///{db}"
    try:
        rt.adf_copy_csv_to_sql(ctx(), "c", str(tmp_path / "s"), "", "ADF_CONN_X", "t", header=False, mapping=rt.fx.translator_mapping(
            {"type": "TabularTranslator", "mappings": [{"source": {"type": "String", "ordinal": 1}, "sink": {"name": "id"}}, {"source": {"ordinal": 2}, "sink": {"name": "name"}}]}))
    finally:
        del os.environ["ADF_CONN_X"]
    with eng.connect() as c:
        assert [tuple(r) for r in c.execute(text("select id, name from t order by id")).fetchall()] == [(1, "Ann"), (2, "Bob")]       # ordinals start at 1; the third column is not mapped, so it is not copied


# ---- mappings --------------------------------------------------------------------------------------------------------------------------------

def test_the_default_mapping_is_by_case_sensitive_name_and_explicit_mapping_copies_only_what_it_lists():
    df = pd.DataFrame({"Id": [1], "Name": ["a"], "extra": [9]})
    assert list(rt._apply_mapping(df, None).columns) == ["Id", "Name", "extra"]
    out = rt._apply_mapping(df, {"Id": "id", "Name": "full_name"})
    assert list(out.columns) == ["id", "full_name"]                                              # only the mapped columns, renamed
    with pytest.raises(rt.AdfExprError):
        rt._apply_mapping(df, {"id": "id"})                                                      # "id" is not "Id": names are case-sensitive


def test_a_translator_with_only_type_conversion_settings_is_the_default_mapping():
    # 52 real Copy activities in the public corpus carry exactly this and no mappings
    t = {"type": "TabularTranslator", "typeConversion": True, "typeConversionSettings": {"allowDataTruncation": True, "treatBooleanAsNumber": False}}
    assert rt.fx.translator_mapping(t) is None
    assert rt.fx.translator_mapping(json.dumps(t)) is None and rt.fx.translator_mapping("") is None and rt.fx.translator_mapping(None) is None


def test_a_metadata_driven_translator_expression_is_resolved_when_the_copy_runs():
    t = json.dumps({"type": "TabularTranslator", "mappings": [{"source": {"name": "a"}, "sink": {"name": "A"}}, {"source": {"ordinal": 2}, "sink": {"name": "B"}}]})
    assert rt.fx.translator_mapping(t) == {"a": "A", "Prop_1": "B"}
    with pytest.raises(rt.AdfExprError):
        rt.fx.translator_mapping({"mappings": [{"source": {"name": "a"}, "sink": {"ordinal": 1}}]})
    with pytest.raises(rt.AdfExprError):
        rt.fx.translator_mapping({"mappings": [{"source": {"path": "$['a']"}, "sink": {"name": "a"}}]})


# ---- hierarchical source to tabular sink: the documentation's own example -------------------------------------------------------------------

DOC_JSON = {"id": {"$oid": "592e07800000000000000000"}, "number": "01", "date": "20170122",
            "orders": [{"prod": "p1", "price": 23}, {"prod": "p2", "price": 13}, {"prod": "p3", "price": 231}], "city": [{"name": "Seattle"}]}
DOC_MAPPINGS = [("$['number']", "orderNumber"), ("$['date']", "orderDate"), ("['prod']", "order_pd"), ("['price']", "order_price"), ("$['city'][0]['name']", "city")]


def test_the_example_document_is_in_the_page():
    page = (DATA / "adf-copy-activity-schema-and-type-mapping.md").read_text()
    assert '"orders": [' in page and "\"collectionReference\": \"$['orders']\"" in page and "$['city'][0]['name']" in page


def test_hierarchical_to_tabular_cross_applies_the_collection_array_with_the_root_values():
    rows = rt.hierarchical_rows(DOC_JSON, DOC_MAPPINGS, "$['orders']")
    # the page's table shows P1, P2, P3; its JSON has lowercase p1, p2, p3 (a documentation typo), so the lowercase values are what come out
    assert rows == [
        {"orderNumber": "01", "orderDate": "20170122", "order_pd": "p1", "order_price": 23, "city": "Seattle"},
        {"orderNumber": "01", "orderDate": "20170122", "order_pd": "p2", "order_price": 13, "city": "Seattle"},
        {"orderNumber": "01", "orderDate": "20170122", "order_pd": "p3", "order_price": 231, "city": "Seattle"},
    ]


def test_an_empty_collection_array_skips_the_record_and_no_collection_means_one_row():
    assert rt.hierarchical_rows({"number": "01", "orders": []}, DOC_MAPPINGS[:1], "$['orders']") == []
    assert rt.hierarchical_rows({"number": "01"}, [("$['number']", "n")], "") == [{"n": "01"}]


def test_a_rest_copy_with_path_mappings_writes_the_documented_table(tmp_path):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(DOC_JSON).encode())
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rt.adf_copy_rest(ctx(), "r", f"http://127.0.0.1:{srv.server_port}/x", "GET", {}, None, str(tmp_path), "", "o.csv", collection="$['orders']",
                         dst_fmt="DelimitedText", dst_opts={"header": True}, path_mappings=DOC_MAPPINGS)
    finally:
        srv.shutdown()
    assert (tmp_path / "o.csv").read_bytes() == (b"orderNumber,orderDate,order_pd,order_price,city\r\n01,20170122,p1,23,Seattle\r\n01,20170122,p2,13,Seattle\r\n01,20170122,p3,231,Seattle\r\n")


def _rest_pipeline(tmp_path, translator):
    fx = tmp_path / "f"
    for d in ("pipeline", "dataset", "linkedService"):
        (fx / d).mkdir(parents=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    (fx / "linkedService" / "Rest.json").write_text(json.dumps({"name": "Rest", "properties": {"type": "RestService", "typeProperties": {"url": "http://x.invalid", "authenticationType": "Anonymous"}}}))
    (fx / "linkedService" / "Files.json").write_text(json.dumps({"name": "Files", "properties": {"type": "FileServer", "typeProperties": {"host": "/x"}}}))
    (fx / "dataset" / "Api.json").write_text(json.dumps({"name": "Api", "properties": {"linkedServiceName": ref("Rest"), "type": "RestResource", "typeProperties": {"relativeUrl": "/o"}}}))
    (fx / "dataset" / "Out.json").write_text(json.dumps({"name": "Out", "properties": {"linkedServiceName": ref("Files"), "type": "DelimitedText",
        "typeProperties": {"location": {"type": "FileServerLocation", "folderPath": "/o", "fileName": "o.csv"}, "firstRowAsHeader": True}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "Get", "type": "Copy",
        "inputs": [{"referenceName": "Api", "type": "DatasetReference"}], "outputs": [{"referenceName": "Out", "type": "DatasetReference"}],
        "typeProperties": {"source": {"type": "RestSource"}, "sink": {"type": "DelimitedTextSink"}, "translator": translator}}]}}))
    return convert(load(fx), pkg="p")


def test_the_converter_emits_the_documented_hierarchical_mapping(tmp_path):
    tr = {"type": "TabularTranslator", "collectionReference": "$['orders']", "mappings": [{"source": {"path": p}, "sink": {"name": n}} for p, n in DOC_MAPPINGS]}
    result = _rest_pipeline(tmp_path, tr)
    assert "path_mappings=" in result.code and "collection=\"$['orders']\"" in result.code or "collection=\"$['orders']\"".replace('"', "'") in result.code
    assert next(i for i in result.items if i.source == "P").status == "review"                 # translated, with the layout note


def test_type_conversion_only_translator_does_not_stub_a_copy(tmp_path):
    result = _rest_pipeline(tmp_path, {"type": "TabularTranslator", "typeConversion": True})
    assert "ADF-CAVEAT" not in result.code


# ---- storage wildcards and copy behavior: the tables in the Blob Storage connector page --------------------------------------------------------

def _tree(root, files):
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f)


FILTER_TREE = ["FolderA/File1.csv", "FolderA/File2.json", "FolderA/Subfolder1/File3.csv", "FolderA/Subfolder1/File4.json", "FolderA/Subfolder1/File5.csv", "AnotherFolderB/File6.csv"]


def _copied(tmp_path, tree, **kw):
    src, dst = tmp_path / "container", tmp_path / "out"
    _tree(src, tree)
    rt.adf_copy_files(ctx(), "c", str(src), "", str(dst), "", **kw)
    return sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())


@pytest.mark.parametrize("wild,recursive,want", [
    ("", False, ["FolderA/File1.csv", "FolderA/File2.json"]),
    ("", True, ["FolderA/File1.csv", "FolderA/File2.json", "FolderA/Subfolder1/File3.csv", "FolderA/Subfolder1/File4.json", "FolderA/Subfolder1/File5.csv"]),
    ("*.csv", False, ["FolderA/File1.csv"]),
    ("*.csv", True, ["FolderA/File1.csv", "FolderA/Subfolder1/File3.csv", "FolderA/Subfolder1/File5.csv"]),
])
def test_documented_folder_and_file_filter_examples(tmp_path, wild, recursive, want):
    """Page table "Folder and file filter examples": folderPath container/Folder*, with and without fileName *.csv, recursive false and true."""
    assert _copied(tmp_path, FILTER_TREE, recursive=recursive, wildcard=wild, wildcard_folder="Folder*") == want        # AnotherFolderB never matches Folder*


def test_wildcard_characters_and_the_escape():
    w = rt._wild
    assert w("*.csv").fullmatch("a.csv") and not w("*.csv").fullmatch("a.json")
    assert w("a?c").fullmatch("abc") and w("a?c").fullmatch("ac") and not w("a?c").fullmatch("abbc")                       # ? is zero or one character
    assert w("???20180427.txt").fullmatch("abc20180427.txt")
    assert w("a^*b").fullmatch("a*b") and not w("a^*b").fullmatch("axb")                                                    # ^ escapes a wildcard
    assert not w("Folder*").fullmatch("AnotherFolderB") and w("Folder*").fullmatch("FolderA")


COPY_TREE = ["Folder1/File1", "Folder1/File2", "Folder1/Subfolder1/File3", "Folder1/Subfolder1/File4", "Folder1/Subfolder1/File5"]


@pytest.mark.parametrize("recursive,flatten,want", [
    (True, False, ["File1", "File2", "Subfolder1/File3", "Subfolder1/File4", "Subfolder1/File5"]),       # preserveHierarchy: same structure
    (True, True, ["File1", "File2", "File3", "File4", "File5"]),                                         # flattenHierarchy: all files in the target folder (ADF autogenerates the names)
    (False, False, ["File1", "File2"]),                                                                  # Subfolder1 with File3, File4 and File5 isn't picked up
    (False, True, ["File1", "File2"]),
])
def test_documented_recursive_and_copy_behavior_examples(tmp_path, recursive, flatten, want):
    src, dst = tmp_path / "s", tmp_path / "out"
    _tree(src, COPY_TREE)
    rt.adf_copy_files(ctx(), "c", str(src / "Folder1"), "", str(dst), "", recursive=recursive, flatten=flatten)
    assert sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()) == want


def test_delete_refuses_wildcard_folder_paths_as_the_documentation_says(tmp_path):
    fx = tmp_path / "f"
    for d in ("pipeline", "dataset"):
        (fx / d).mkdir(parents=True)
    (fx / "dataset" / "D.json").write_text(json.dumps({"name": "D", "properties": {"linkedServiceName": {"referenceName": "L", "type": "LinkedServiceReference"}, "type": "Binary",
        "typeProperties": {"location": {"type": "AzureBlobStorageLocation", "container": "c"}}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "Del", "type": "Delete", "typeProperties": {
        "dataset": {"referenceName": "D", "type": "DatasetReference"}, "storeSettings": {"type": "AzureBlobStorageReadSettings", "recursive": True, "wildcardFolderPath": "a*"}}}]}}))
    result = convert(load(fx), pkg="p")
    assert "does not support wildcard folder paths" in " ".join(next(i for i in result.items if i.source == "P").notes)


# ---- REST pagination: the rules and examples in the REST connector page ----------------------------------------------------------------------

def run_pages(responder, url, rules, headers=None):
    """Drive rest_pages with a fake endpoint: responder(url, headers, call number) -> (status, response headers, body). Returns (bodies, [(url, headers)])."""
    calls = []

    def fetch(u, h, b):
        calls.append((u, dict(h)))
        return responder(u, h, len(calls) - 1)

    return rt.rest_pages(ctx(), fetch, url, headers or {}, None, rules), calls


def qs(u):
    import urllib.parse
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))


def test_page_documents_the_vocabulary_we_implement():
    page = (DATA / "adf-connector-rest.md").read_text()
    for term in ("AbsoluteUrl", "QueryParameters", "EndCondition", "MaxRequestNumber", "SupportRFC5988", "RANGE:0:10000:1000", "RANGE:0::1000", "RANGE:1:100:1", "RANGE:0:100:10", "204"):
        assert term in page, term


def test_example_1_query_parameter_range_sends_one_request_per_value():
    """"QueryParameters.{offset}": "RANGE:0:10000:1000" with sysparm_offset={offset} in the url: offsets 0, 1000, ... 10000 (the end is inclusive)."""
    _, calls = run_pages(lambda u, h, n: (200, {}, {"result": [n]}), "http://b/api/now/table/incident?sysparm_limit=1000&sysparm_offset={offset}", {"QueryParameters.{offset}": "RANGE:0:10000:1000"})
    assert [qs(u)["sysparm_offset"] for u, _ in calls] == [str(i) for i in range(0, 10001, 1000)]


def test_example_2_variables_in_the_absolute_url():
    _, calls = run_pages(lambda u, h, n: (200, {}, {"n": n}), "http://b/api/now/table/t{id}", {"AbsoluteUrl.{id}": "RANGE:1:100:1"})
    assert len(calls) == 100 and calls[0][0].endswith("/t1") and calls[-1][0].endswith("/t100")


def test_example_3_variables_in_headers():
    _, calls = run_pages(lambda u, h, n: (200, {}, {}), "http://b/table", {"Headers.{id}": "RANGE:0:100:10"}, headers={"id": "{id}"})
    assert [h["id"] for _, h in calls] == [str(i) for i in range(0, 101, 10)]


@pytest.mark.parametrize("cond,last", [
    ({"EndCondition:$.data": "Empty"}, {"data": []}),                                        # Example 4.1
    ({"EndCondition:$.data": "NonExist"}, {}),                                               # 4.2
    ({"EndCondition:$.Complete": "Exist"}, {"data": [1], "Complete": "yes"}),                # 4.3
    ({"EndCondition:$.Complete": "Const:true"}, {"data": [1], "Complete": True}),            # 4.5
])
def test_example_4_open_ended_range_stops_on_the_end_condition(cond, last):
    """"AbsoluteUrl.{offset}": "RANGE:0::1000" has no end, so the response decides: here the fourth response is the last."""
    def resp(u, h, n):
        return 200, {}, (last if n == 3 else {"data": [n]})
    bodies, calls = run_pages(resp, "http://b/t?offset={offset}", {"AbsoluteUrl.{offset}": "RANGE:0::1000", **cond})
    assert [qs(u)["offset"] for u, _ in calls] == ["0", "1000", "2000", "3000"] and len(bodies) == 4


def test_example_4_end_condition_on_a_response_header():
    def resp(u, h, n):
        return 200, ({"Complete": "1"} if n == 2 else {}), {"data": [n]}
    _, calls = run_pages(resp, "http://b/t?o={o}", {"QueryParameters.{o}": "RANGE:0::10", "EndCondition:headers.Complete": "Const:1"})
    assert len(calls) == 3
    _, calls = run_pages(lambda u, h, n: (200, ({"CompleteTime": "x"} if n == 1 else {}), {}), "http://b/t?o={o}", {"QueryParameters.{o}": "RANGE:0::10", "EndCondition:headers.CompleteTime": "Exist"})
    assert len(calls) == 2


def test_the_next_url_from_the_body_may_be_relative_and_a_null_value_ends_the_loop():
    pages = {0: {"next": "/api/p2", "d": 1}, 1: {"next": "/api/p3", "d": 2}, 2: {"next": None, "d": 3}}
    bodies, calls = run_pages(lambda u, h, n: (200, {}, pages[n]), "http://b/api/p1", {"AbsoluteUrl": "$.next"})
    assert [u for u, _ in calls] == ["http://b/api/p1", "http://b/api/p2", "http://b/api/p3"] and [b["d"] for b in bodies] == [1, 2, 3]
    _, calls = run_pages(lambda u, h, n: (200, {}, {"d": 1}), "http://b/x", {"AbsoluteUrl": "$.missing"})                # no next value: one request
    assert len(calls) == 1


def test_a_query_parameter_and_a_header_can_be_taken_from_the_previous_response():
    def resp(u, h, n):
        return 200, {"X-Next-Token": f"tok{n}"} if n < 2 else {}, {"cursor": f"c{n}" if n < 2 else None}
    _, calls = run_pages(resp, "http://b/items?limit=5", {"QueryParameters.cursor": "$.cursor", "Headers.token": "Headers.X-Next-Token"})
    assert qs(calls[1][0])["cursor"] == "c0" and calls[1][1]["token"] == "tok0" and qs(calls[2][0])["cursor"] == "c1" and qs(calls[1][0])["limit"] == "5"
    assert len(calls) == 3                                                                                       # the third response has no cursor: stop


def test_http_204_ends_the_loop_and_max_request_number_caps_it():
    bodies, calls = run_pages(lambda u, h, n: (204, {}, None) if n == 2 else (200, {}, {"n": n}), "http://b/t?o={o}", {"QueryParameters.{o}": "RANGE:0::1"})
    assert len(calls) == 3 and len(bodies) == 2                                                                  # the 204 page is not data
    _, calls = run_pages(lambda u, h, n: (200, {}, {"n": n}), "http://b/t?o={o}", {"QueryParameters.{o}": "RANGE:0::1", "MaxRequestNumber": "4"})
    assert len(calls) == 4


def test_rfc_5988_link_header_is_followed_when_no_rule_is_given():
    def resp(u, h, n):
        return 200, ({"Link": '<http://b/p2>; rel="next"'} if n == 0 else {}), {"n": n}
    _, calls = run_pages(resp, "http://b/p1", {})
    assert [u for u, _ in calls] == ["http://b/p1", "http://b/p2"]
    _, calls = run_pages(resp, "http://b/p1", {"SupportRFC5988": "false", "MaxRequestNumber": "1"})
    assert len(calls) == 1


def test_the_converter_rejects_pagination_keys_outside_the_documented_vocabulary(tmp_path):
    result = _rest_pipeline_with(tmp_path, {"paginationRules": {"Nonsense": "$.x"}})
    assert "outside the documented vocabulary" in " ".join(next(i for i in result.items if i.source == "P").notes)
    ok = _rest_pipeline_with(tmp_path, {"paginationRules": {"QueryParameters.{offset}": "RANGE:0:100:10", "EndCondition:$.d": "Empty"}, "requestInterval": "00:00:01"})
    assert "rest_pages" not in ok.code and "ADF-CAVEAT" not in ok.code


def _rest_pipeline_with(tmp_path, source_extra):
    import shutil
    fx = tmp_path / "g"
    shutil.rmtree(fx, ignore_errors=True)
    for d in ("pipeline", "dataset", "linkedService"):
        (fx / d).mkdir(parents=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    (fx / "linkedService" / "Rest.json").write_text(json.dumps({"name": "Rest", "properties": {"type": "RestService", "typeProperties": {"url": "http://x.invalid", "authenticationType": "Anonymous"}}}))
    (fx / "linkedService" / "Files.json").write_text(json.dumps({"name": "Files", "properties": {"type": "FileServer", "typeProperties": {"host": "/x"}}}))
    (fx / "dataset" / "Api.json").write_text(json.dumps({"name": "Api", "properties": {"linkedServiceName": ref("Rest"), "type": "RestResource", "typeProperties": {"relativeUrl": "/o?offset={offset}"}}}))
    (fx / "dataset" / "Out.json").write_text(json.dumps({"name": "Out", "properties": {"linkedServiceName": ref("Files"), "type": "Json",
        "typeProperties": {"location": {"type": "FileServerLocation", "folderPath": "/o", "fileName": "o.json"}}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "Get", "type": "Copy",
        "inputs": [{"referenceName": "Api", "type": "DatasetReference"}], "outputs": [{"referenceName": "Out", "type": "DatasetReference"}],
        "typeProperties": {"source": {"type": "RestSource", **source_extra}, "sink": {"type": "JsonSink"}}}]}}))
    return convert(load(fx), pkg="p")


# ---- connector-page filters: modified time, file list, prefix, delete after completion, merge, partition discovery ----------------------------------

def _touch(path, text, iso):
    import os
    from datetime import datetime
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (ts, ts))


def test_the_connector_pages_define_the_filters_we_implement():
    blob = (DATA / "adf-connector-azure-blob-storage.md").read_text()
    assert "greater than or equal to `modifiedDatetimeStart` and less than `modifiedDatetimeEnd`" in blob
    assert "one file per line, which is the relative path to the path configured in the dataset" in blob
    assert "only valid in binary files copy scenario" in blob and "The file deletion is per file" in blob
    assert "Objects whose keys start with this prefix are selected" in (DATA / "adf-connector-amazon-simple-storage-service.md").read_text()


def test_modified_time_start_is_inclusive_end_is_exclusive_and_either_may_be_open(tmp_path):
    for n, ts in (("old.txt", "2025-01-01T00:00:00Z"), ("edge.txt", "2025-02-01T00:00:00Z"), ("mid.txt", "2025-02-15T00:00:00Z"), ("late.txt", "2025-03-01T00:00:00Z")):
        _touch(tmp_path / "s" / n, n, ts)
    def got(**f):
        out = tmp_path / f"o{len(list(tmp_path.glob('o*')))}"
        rt.adf_copy_files(ctx(), "c", str(tmp_path / "s"), "", str(out), "", filters=f)
        return sorted(p.name for p in out.glob("*")) if out.exists() else []
    assert got(modified_start="2025-02-01T00:00:00Z", modified_end="2025-03-01T00:00:00Z") == ["edge.txt", "mid.txt"]        # >= start and < end
    assert got(modified_start="2025-02-01T00:00:00Z") == ["edge.txt", "late.txt", "mid.txt"]                                # no end: everything from the start on
    assert got(modified_end="2025-02-01T00:00:00Z") == ["old.txt"]                                                          # no start: everything before the end


def test_a_file_list_names_the_files_relative_to_the_dataset_path(tmp_path):
    _tree(tmp_path / "container", ["FolderA/File1.csv", "FolderA/File2.json", "FolderA/Subfolder1/File3.csv", "FolderA/Subfolder1/File4.json", "FolderA/Subfolder1/File5.csv"])
    (tmp_path / "container" / "Metadata").mkdir()
    (tmp_path / "container" / "Metadata" / "FileListToCopy.txt").write_text("File1.csv\nSubfolder1/File3.csv\nSubfolder1/File5.csv\n")
    rt.adf_copy_files(ctx(), "c", str(tmp_path / "container" / "FolderA"), "", str(tmp_path / "o"), "", filters={"file_list": str(tmp_path / "container" / "Metadata" / "FileListToCopy.txt")})
    assert sorted(str(p.relative_to(tmp_path / "o")) for p in (tmp_path / "o").rglob("*") if p.is_file()) == ["File1.csv", "Subfolder1/File3.csv", "Subfolder1/File5.csv"]
    (tmp_path / "container" / "Metadata" / "bad.txt").write_text("nope.csv\n")
    with pytest.raises(rt.AdfExprError, match="do not exist"):
        rt.adf_copy_files(ctx(), "c", str(tmp_path / "container" / "FolderA"), "", str(tmp_path / "o2"), "", filters={"file_list": str(tmp_path / "container" / "Metadata" / "bad.txt")})


def test_a_prefix_selects_keys_under_the_store_root(tmp_path):
    _tree(tmp_path / "bucket", ["logs/a.csv", "logs/sub/b.csv", "data/c.csv", "logs2/d.csv"])
    rt.adf_copy_files(ctx(), "c", str(tmp_path / "bucket"), "", str(tmp_path / "o"), "", filters={"prefix": "logs/"})
    assert sorted(str(p.relative_to(tmp_path / "o")) for p in (tmp_path / "o").rglob("*") if p.is_file()) == ["logs/a.csv", "logs/sub/b.csv"]


def test_delete_after_completion_removes_each_file_once_it_was_copied_and_a_failure_leaves_the_rest(tmp_path, monkeypatch):
    _tree(tmp_path / "s", ["a.bin", "b.bin", "c.bin"])
    rt.adf_copy_files(ctx(), "c", str(tmp_path / "s"), "", str(tmp_path / "o"), "", delete_source=True)
    assert not list((tmp_path / "s").glob("*")) and sorted(p.name for p in (tmp_path / "o").glob("*")) == ["a.bin", "b.bin", "c.bin"]
    _tree(tmp_path / "t", ["a.bin", "b.bin", "c.bin"])
    import shutil
    real, calls = shutil.copyfileobj, []
    def flaky(src, dst, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(shutil, "copyfileobj", flaky)
    with pytest.raises(OSError):
        rt.adf_copy_files(ctx(), "c", str(tmp_path / "t"), "", str(tmp_path / "o2"), "", delete_source=True)
    assert sorted(p.name for p in (tmp_path / "t").glob("*")) == ["b.bin", "c.bin"]          # a.bin was copied and deleted; the rest are still on the source


def test_merge_files_joins_the_contents_in_name_order(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "b.txt").write_text("B\n")
    (tmp_path / "s" / "a.txt").write_text("A\n")
    rt.adf_copy_files(ctx(), "c", str(tmp_path / "s"), "", str(tmp_path / "o"), "", dst_file="merged.txt", merge=True)
    assert (tmp_path / "o" / "merged.txt").read_text() == "A\nB\n"
    rt.adf_copy_files(ctx(), "c", str(tmp_path / "s"), "", str(tmp_path / "o2"), "", merge=True)
    (only,) = list((tmp_path / "o2").glob("data_*.txt"))                                       # no sink file name: autogenerated, one file
    assert only.read_text() == "A\nB\n"


def test_partition_discovery_turns_key_value_folders_into_columns(tmp_path):
    _tree(tmp_path / "s", [])
    for ym, body in (("year=2025/month=01", "id\n1\n"), ("year=2025/month=02", "id\n2\n")):
        (tmp_path / "s" / ym).mkdir(parents=True)
        (tmp_path / "s" / ym / "f.csv").write_text(body)
    rt.adf_copy_convert(ctx(), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True}, str(tmp_path / "o"), "", "Parquet", {}, partition_root="")
    df = pd.concat([pd.read_parquet(p) for p in sorted((tmp_path / "o").rglob("*.parquet"))], ignore_index=True)
    assert sorted(zip(df["id"], df["year"], df["month"])) == [("1", "2025", "01"), ("2", "2025", "02")]
    assert rt._partition_columns("/root/a=1/b=x/f.csv", "/root") == {"a": "1", "b": "x"} and rt._partition_columns("/root/f.csv", "/root") == {}


def _store_pipeline(tmp_path, ds_type, loc, source_settings, sink_settings=None, sink_type="BinarySink", src_type="BinarySource", sink_ds_type=None):
    import shutil
    fx = tmp_path / "sp"
    shutil.rmtree(fx, ignore_errors=True)
    for d in ("pipeline", "dataset", "linkedService"):
        (fx / d).mkdir(parents=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    (fx / "linkedService" / "Lake.json").write_text(json.dumps({"name": "Lake", "properties": {"type": "AzureBlobStorage", "typeProperties": {}}}))
    for n, t in (("In", ds_type), ("Out", sink_ds_type or ds_type)):
        (fx / "dataset" / f"{n}.json").write_text(json.dumps({"name": n, "properties": {"linkedServiceName": ref("Lake"), "type": t,
            "typeProperties": {"location": {"type": "AzureBlobStorageLocation", "container": "c" if n == "In" else "o", **loc}, **({"firstRowAsHeader": True} if t == "DelimitedText" else {})}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "C", "type": "Copy",
        "inputs": [{"referenceName": "In", "type": "DatasetReference"}], "outputs": [{"referenceName": "Out", "type": "DatasetReference"}],
        "typeProperties": {"source": {"type": src_type, "storeSettings": {"type": "AzureBlobStorageReadSettings", **source_settings}},
                           "sink": {"type": sink_type, "storeSettings": {"type": "AzureBlobStorageWriteSettings", **(sink_settings or {})}}}}]}}))
    return convert(load(fx), pkg="p")


def test_the_converter_passes_the_filters_merge_and_delete_through(tmp_path):
    r = _store_pipeline(tmp_path, "Binary", {"folderPath": "in"}, {"recursive": True, "modifiedDatetimeStart": "2025-01-01T00:00:00Z", "modifiedDatetimeEnd": {"value": "@utcnow()", "type": "Expression"},
                        "prefix": "logs/", "deleteFilesAfterCompletion": True}, {"copyBehavior": "MergeFiles"})
    code = next(l for l in r.code.splitlines() if "adf_copy_files(ctx" in l)
    assert "'modified_start': '2025-01-01T00:00:00Z'" in code and "'modified_end': fx.utcnow()" in code and "'prefix': 'logs/'" in code
    assert "delete_source=True" in code and "merge=True" in code and "ADF-CAVEAT" not in r.code
    assert "abfs://', 'c', '/', ''" in code.replace("(", "").replace('"', "'") or "fx.concat('abfs://', 'c', '/', '')" in code           # a prefix reads from the container root, not the dataset folder


def test_delete_after_completion_is_for_binary_copies_and_merge_for_byte_copies(tmp_path):
    r = _store_pipeline(tmp_path, "DelimitedText", {"folderPath": "in"}, {"deleteFilesAfterCompletion": True}, None, "DelimitedTextSink", "DelimitedTextSource")
    # same format, same settings: a byte copy of delimited text, where deleteFilesAfterCompletion is not valid
    assert "only valid for a binary file copy" in " ".join(next(i for i in r.items if i.source == "P").notes)
    r2 = _store_pipeline(tmp_path, "DelimitedText", {"folderPath": "in"}, {}, {"copyBehavior": "MergeFiles"}, "ParquetSink", "DelimitedTextSource", "Parquet")
    assert "MergeFiles is translated for same-format byte copies only" in " ".join(next(i for i in r2.items if i.source == "P").notes)
