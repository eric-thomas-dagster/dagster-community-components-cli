"""The data flow script parser and the row executor, on scripts in the shapes Microsoft's pages and the public corpus use."""
import pytest

from adf_to_dagster import dflow as df


class Mem:
    def __init__(self, **tables):
        self.tables, self.out = tables, {}

    def read(self, name, schema):
        return self.tables[name]

    def write(self, name, rows, opts):
        self.out[name] = rows
        return len(rows)


def run(script, params=None, **tables):
    io = Mem(**tables)
    df.run_flow(script, io, params)
    return io.out


def test_parameters_and_statements_parse():
    params, nodes = df.parse_script("""parameters{
    since as string ('2020-01-01'),
    n as integer (toInteger(3))
}
source(output(a as integer, {b c} as string), allowSchemaDrift: true) ~> src
src filter(a > 1 && {b c} != 'x') ~> f
f sink(allowSchemaDrift: true) ~> out""")
    assert params == {"since": ("string", "'2020-01-01'"), "n": ("integer", "toInteger(3)")}
    assert [(n.kind, n.name, n.inputs) for n in nodes] == [("source", "src", []), ("filter", "f", ["src"]), ("sink", "out", ["f"])]
    assert df.source_schema(nodes[0]) == [("a", "integer"), ("b c", "string")]
    assert nodes[0].opts["allowSchemaDrift"] == "true"


def test_documented_sort_example_nulls_and_direction():
    rows = [{"PTS": 3, "Age": 30}, {"PTS": 9, "Age": 25}, {"PTS": 9, "Age": None}, {"PTS": None, "Age": 20}, {"PTS": 9, "Age": 22}]
    out = run("""src sort(desc(PTS, true),
    asc(Age, true)) ~> Sort1
Sort1 sink() ~> o
source(output(PTS as integer, Age as integer)) ~> src""", src=rows)["o"]
    assert [(r["PTS"], r["Age"]) for r in out] == [(None, 20), (9, None), (9, 22), (9, 25), (3, 30)]


def test_filter_derive_select_aggregate_chain():
    rows = [{"g": "a", "v": 1}, {"g": "a", "v": 5}, {"g": "b", "v": 2}, {"g": "b", "v": None}, {"g": "c", "v": 100}]
    out = run("""source(output(g as string, v as integer)) ~> src
src filter(g != 'c') ~> f
f derive(v2 = v * 2, label = upper(g) + '!') ~> d
d aggregate(groupBy(g),
    total = sum(v2), n = count(v), top = max(v)) ~> agg
agg select(mapColumn(grupo = g, total, n)) ~> sel
sel sink() ~> out""", src=rows)["out"]
    assert out == [{"grupo": "a", "total": 12, "n": 2}, {"grupo": "b", "total": 4, "n": 1}]


def test_derive_sees_incoming_row_and_overwrites_columns():
    out = run("""source(output(a as integer)) ~> src
src derive(a = a + 1, b = a) ~> d
d sink() ~> out""", src=[{"a": 1}])["out"]
    assert out == [{"a": 2, "b": 1}]


@pytest.mark.parametrize("jt,expect", [("inner", [(1, "x")]), ("left", [(1, "x"), (2, None)]), ("right", [(1, "x"), (None, "z")]), ("outer", [(1, "x"), (2, None), (None, "z")])])
def test_join_types(jt, expect):
    out = run(f"""source(output(id as integer)) ~> L
source(output(id as integer, tag as string)) ~> R
L, R join(L@id == R@id,
    joinType:'{jt}',
    broadcast: 'auto')~> J
J select(mapColumn(lid = L@id, tag)) ~> S
S sink() ~> out""", L=[{"id": 1}, {"id": 2}], R=[{"id": 1, "tag": "x"}, {"id": 3, "tag": "z"}])["out"]
    got = [(r["lid"], r["tag"]) for r in out]
    if jt in ("right", "outer"):
        got = [(None if i is None else i, t) for i, t in got]
    assert got == expect


def test_ambiguous_unqualified_column_in_a_join_is_an_error():
    with pytest.raises(KeyError):
        run("""source(output(id as integer)) ~> L
source(output(id as integer)) ~> R
L, R join(id == 1, joinType:'inner') ~> J
J sink() ~> out""", L=[{"id": 1}], R=[{"id": 1}])


def test_union_by_name_and_position():
    a, b = [{"x": 1, "y": 2}], [{"y": 20, "x": 10}]
    by_name = run("""source(output(x as integer, y as integer)) ~> A
source(output(x as integer, y as integer)) ~> B
A, B union(byName: true) ~> U
U sink() ~> o""".replace("byName: true", "unionBy: 'name'"), A=a, B=b)["o"]
    assert by_name == [{"x": 1, "y": 2}, {"x": 10, "y": 20}]
    by_pos = run("""source(output(x as integer, y as integer)) ~> A
source(output(x as integer, y as integer)) ~> B
A, B union(unionBy: 'position') ~> U
U sink() ~> o""", A=a, B=b)["o"]
    assert by_pos == [{"x": 1, "y": 2}, {"x": 20, "y": 10}]


def test_parameters_default_and_override():
    script = """parameters{ who as string ('guest') }
source(output(a as integer)) ~> s
s derive(who = $who) ~> d
d sink() ~> o"""
    assert run(script, s=[{"a": 1}])["o"][0]["who"] == "guest"
    assert run(script, {"who": "ann"}, s=[{"a": 1}])["o"][0]["who"] == "ann"


def test_unsupported_transformations_are_named():
    _, nodes = df.parse_script("""source(output(a as integer)) ~> s
s pivot(groupBy(a), pivotBy(b), n = count()) ~> m
m sink() ~> o""")
    why = df.check_flow(nodes)
    assert any("pivot" in w for w in why)
    _, nodes = df.parse_script("""source(output(a as integer)) ~> s
s derive(each(match(true()), $$ = 1)) ~> d
d sink() ~> o""")
    assert any("column pattern" in w for w in df.check_flow(nodes))
    _, nodes = df.parse_script("""source(output(a as integer)) ~> s
s derive(a = byName('x')) ~> d
d sink() ~> o""")
    assert any("byName" in w for w in df.check_flow(nodes))


def test_casts_from_text():
    assert df.cast_value("12", "integer") == 12 and df.cast_value("x", "integer") is None and df.cast_value("", "string") is None
    assert df.cast_value("true", "boolean") is True and str(df.cast_value("1.50", "decimal(10,2)")) == "1.50"
    assert df.cast_value("2020-02-03", "date").isoformat() == "2020-02-03"


# ---- through the runtime: files and SQL ---------------------------------------------------------------------------------------------------

import json
from pathlib import Path

from sqlalchemy import create_engine, text

from adf_to_dagster import runtime as rt
from adf_to_dagster.convert import convert
from adf_to_dagster.loader import load

SCRIPT = """parameters{ minimum as integer (0) }
source(output(id as integer, region as string, amount as decimal(10,2)), allowSchemaDrift: true, validateSchema: false) ~> sales
sales filter(amount > $minimum) ~> big
big aggregate(groupBy(region), total = sum(amount), orders = count()) ~> byRegion
byRegion sort(desc(total, true)) ~> sorted
sorted sink(allowSchemaDrift: true, validateSchema: false, insertable: true, truncate: true) ~> toTable
sorted sink(allowSchemaDrift: true, partitionFileNames:['totals.csv'], truncate: true, partitionBy('hash', 1)) ~> toFile"""


def _ctx():
    return rt.Ctx("P", {}, {}, {}, log=lambda m: None)


def test_runtime_runs_a_flow_from_files_to_sql_and_files(tmp_path, monkeypatch):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "sales.csv").write_text("id,region,amount\n1,east,10.50\n2,west,3\n3,east,20\n4,north,\n5,west,7.25\n")
    db = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table totals (region text, total real, orders integer)"))
        c.execute(text("insert into totals values ('stale', 0, 0)"))
    monkeypatch.setenv("ADF_CONN_DB", f"sqlite:///{db}")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old.csv").write_text("x")
    spec_in = {"kind": "file", "url": str(tmp_path / "in"), "env": "", "fmt": "DelimitedText", "opts": {"header": True, "delimiter": ",", "quote": '"', "null_value": ""}, "file": "sales.csv"}
    sinks = {"toTable": {"kind": "sql", "env": "ADF_CONN_DB", "table": "totals", "truncate": True},
             "toFile": {"kind": "file", "url": str(tmp_path / "out"), "env": "", "fmt": "DelimitedText", "opts": {"header": True, "delimiter": ",", "quote": '"', "null_value": ""},
                        "file": "totals.csv", "truncate": True}}
    out = rt.adf_dataflow(_ctx(), "run", "F", SCRIPT, {"sales": spec_in}, sinks, {"minimum": 4})
    assert out["runStatus"]["metrics"] == {"toTable": {"rowsWritten": 2}, "toFile": {"rowsWritten": 2}}
    with eng.begin() as c:
        assert [tuple(r) for r in c.execute(text("select region, total, orders from totals order by total desc"))] == [("east", 30.5, 2), ("west", 7.25, 1)]
    assert not (tmp_path / "out" / "old.csv").exists()                                                # truncate cleared the folder
    assert (tmp_path / "out" / "totals.csv").read_text().splitlines()[0] == "region,total,orders"


def test_runtime_dry_run_reads_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    out = rt.adf_dataflow(_ctx(), "run", "F", SCRIPT, {}, {"toTable": {}}, {})
    assert out["runStatus"]["metrics"] == {"toTable": {"rowsWritten": 0}}


def _factory(tmp_path, script, source_ds="Sales", sink_ds="Totals", extra_sources=None):
    fx = tmp_path / "f"
    for d in ("pipeline", "dataset", "linkedService", "dataflow"):
        (fx / d).mkdir(parents=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    (fx / "linkedService" / "Files.json").write_text(json.dumps({"name": "Files", "properties": {"type": "FileServer", "typeProperties": {"host": "/x"}}}))
    (fx / "linkedService" / "Db.json").write_text(json.dumps({"name": "Db", "properties": {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "x"}}}))
    (fx / "dataset" / "Sales.json").write_text(json.dumps({"name": "Sales", "properties": {"linkedServiceName": ref("Files"), "type": "DelimitedText",
        "typeProperties": {"location": {"type": "FileServerLocation", "folderPath": "/in", "fileName": "sales.csv"}, "firstRowAsHeader": True}}}))
    (fx / "dataset" / "Totals.json").write_text(json.dumps({"name": "Totals", "properties": {"linkedServiceName": ref("Db"), "type": "AzureSqlTable",
        "typeProperties": {"schema": "dbo", "table": "totals"}}}))
    (fx / "dataflow" / "Flow.json").write_text(json.dumps({"name": "Flow", "properties": {"type": "MappingDataFlow", "typeProperties": {
        "sources": [{"name": "sales", "dataset": {"referenceName": source_ds, "type": "DatasetReference"}}],
        "sinks": [{"name": "toTable", "dataset": {"referenceName": sink_ds, "type": "DatasetReference"}}],
        "scriptLines": script.split("\n")}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "Run", "type": "ExecuteDataFlow", "typeProperties": {
        "dataflow": {"referenceName": "Flow", "type": "DataFlowReference", "parameters": {"minimum": {"value": "@pipeline().parameters.min", "type": "Expression"}, "label": {"value": "'x'", "type": "Expression"}}},
        "compute": {"coreCount": 8, "computeType": "General"}}}], "parameters": {"min": {"type": "int", "defaultValue": 1}}}}))
    return convert(load(fx), pkg="p")


SIMPLE = """parameters{ minimum as integer (0), label as string ('') }
source(output(id as integer, region as string), allowSchemaDrift: true) ~> sales
sales filter(id > $minimum) ~> f
f sink(allowSchemaDrift: true, insertable: true, truncate: true) ~> toTable"""


def test_converter_translates_an_execute_data_flow(tmp_path):
    result = _factory(tmp_path, SIMPLE)
    assert "ADF-CAVEAT" not in result.code and "adf_dataflow(ctx, 'Run', 'Flow'" in result.code
    assert "'minimum': ctx.param('min')" in result.code and "'label': df_param(\"'x'\")" in result.code
    assert "'env': 'ADF_CONN_DB'" in result.code and "'kind': 'sql'" in result.code
    assert next(i for i in result.items if i.kind == "data_flow").status == "review"


def test_converter_names_why_a_data_flow_is_not_translated(tmp_path):
    result = _factory(tmp_path, SIMPLE.replace("sales filter(id > $minimum) ~> f", "sales pivot(groupBy(id), pivotBy(region), n = count()) ~> f"))
    assert "adf_caveat(" in result.code
    flow = next(i for i in result.items if i.kind == "data_flow")
    assert flow.status == "unmapped" and "pivot" in " ".join(flow.notes)
    pipe = next(i for i in result.items if i.source == "P")
    assert "pivot" in " ".join(pipe.notes)


# ---- documented transformations: exists, lookup, conditional split, surrogate key, alter row --------------------------------------------------

def test_exists_keeps_left_rows_and_negate_inverts_it():
    left, right = [{"EmpID": 1, "Region": "n"}, {"EmpID": 2, "Region": "s"}, {"EmpID": 3, "Region": "n"}], [{"EmpID": 1, "Region": "n"}, {"EmpID": 3, "Region": "x"}]
    script = """source(output(EmpID as integer, Region as string)) ~> L
source(output(EmpID as integer, Region as string)) ~> R
L, R
    exists(
        L@EmpID == R@EmpID && L@Region == R@Region,
	    negate:{neg},
	    broadcast: 'auto'
    ) ~> checkForChanges
checkForChanges sink() ~> o"""
    assert [r["EmpID"] for r in run(script.replace("{neg}", "false"), L=left, R=right)["o"]] == [1]
    assert [r["EmpID"] for r in run(script.replace("{neg}", "true"), L=left, R=right)["o"]] == [2, 3]


def test_lookup_picks_first_by_sort_and_isMatch_splits_matches_from_misses():
    prod = [{"ProductID": 1}, {"ProductID": 2}, {"ProductID": 9}]
    dim = [{"ProductKey": 1, "v": "b"}, {"ProductKey": 1, "v": "a"}, {"ProductKey": 2, "v": "c"}]
    script = """source(output(ProductID as integer)) ~> SQLProducts
source(output(ProductKey as integer, v as string)) ~> DimProd
SQLProducts, DimProd lookup(ProductID == ProductKey,
    multiple: false,
    pickup: 'first',
    asc(v, true),
    broadcast: 'auto')~> LookupKeys
LookupKeys split(isMatch(),
    disjoint: false) ~> Matched@(Found, NoMatch)
Matched@Found sink() ~> good
Matched@NoMatch sink() ~> missing"""
    out = run(script, SQLProducts=prod, DimProd=dim)
    assert [(r["ProductID"], r["v"]) for r in out["good"]] == [(1, "a"), (2, "c")]
    assert [r["ProductID"] for r in out["missing"]] == [9]


def test_lookup_multiple_returns_every_match():
    out = run("""source(output(k as integer)) ~> A
source(output(k2 as integer, v as string)) ~> B
A, B lookup(k == k2, multiple: true) ~> L
L sink() ~> o""", A=[{"k": 1}], B=[{"k2": 1, "v": "x"}, {"k2": 1, "v": "y"}])["o"]
    assert [r["v"] for r in out] == ["x", "y"]


def test_conditional_split_documented_example_first_match_wins_and_default():
    rows = [{"year": y} for y in (1950, 1970, 1990, 1955, 2000)]
    script = """source(output(year as integer)) ~> CleanData
CleanData
    split(
        year < 1960,
	    year > 1980,
	    disjoint: {d}
    ) ~> SplitByYear@(moviesBefore1960, moviesAfter1980, AllOtherMovies)
SplitByYear@moviesBefore1960 sink() ~> before
SplitByYear@moviesAfter1980 sink() ~> after
SplitByYear@AllOtherMovies sink() ~> other"""
    out = run(script.replace("{d}", "false"), CleanData=rows)
    assert [r["year"] for r in out["before"]] == [1950, 1955] and [r["year"] for r in out["after"]] == [1990, 2000] and [r["year"] for r in out["other"]] == [1970]
    both = run(script.replace("{d}", "true").replace("year < 1960", "year < 1960 || year == 1990"), CleanData=rows)
    assert [r["year"] for r in both["before"]] == [1950, 1990, 1955] and [r["year"] for r in both["after"]] == [1990, 2000]      # disjoint: every matching stream


def test_surrogate_key_documented_example():
    out = run("""source(output(a as integer)) ~> AggregateDayStats
AggregateDayStats
    keyGenerate(
        output(key as long),
        startAt: 5L
    ) ~> SurrogateKey1
SurrogateKey1 sink() ~> o""", AggregateDayStats=[{"a": 7}, {"a": 8}, {"a": 9}])["o"]
    assert [r["key"] for r in out] == [5, 6, 7]


def test_alter_row_marks_rows_in_order_and_the_default_is_insert():
    rows = [{"id": 1, "alterRowCondition": "insert"}, {"id": 2, "alterRowCondition": "update"}, {"id": 3, "alterRowCondition": "delete"}, {"id": 4, "alterRowCondition": "?"}]
    out = run("""source(output(id as integer, alterRowCondition as string)) ~> SpecifyUpsertConditions
SpecifyUpsertConditions alterRow(insertIf(alterRowCondition == 'insert'),
	updateIf(alterRowCondition == 'update'),
	deleteIf(alterRowCondition == 'delete')) ~> AlterRow
AlterRow sink() ~> o""", SpecifyUpsertConditions=rows)["o"]
    assert [r["__alterRow__"] for r in out] == ["insert", "update", "delete", "insert"]


def test_sql_sink_applies_only_the_policies_it_allows(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table t (id integer primary key, v text)"))
        c.execute(text("insert into t values (2, 'old'), (3, 'gone'), (5, 'kept')"))
    monkeypatch.setenv("ADF_CONN_DB", f"sqlite:///{db}")
    script = """source(output(id as integer, v as string, act as string)) ~> s
s alterRow(insertIf(act == 'i'), updateIf(act == 'u'), deleteIf(act == 'd'), upsertIf(act == 'x')) ~> a
a sink(allowSchemaDrift: true) ~> out"""
    rows = [{"id": 1, "v": "new", "act": "i"}, {"id": 2, "v": "upd", "act": "u"}, {"id": 3, "v": "-", "act": "d"}, {"id": 4, "v": "ups", "act": "x"}, {"id": 5, "v": "zzz", "act": "x"}]
    spec = {"kind": "sql", "env": "ADF_CONN_DB", "table": "t", "truncate": False, "pre_sql": [], "keys": ["id"], "allow": {"insert": True, "update": True, "delete": True, "upsert": False}}
    with eng.begin() as con:
        n = rt._write_policies(con, spec, [{**r, "__alterRow__": {"i": "insert", "u": "update", "d": "delete", "x": "upsert"}[r["act"]]} for r in rows])
    with eng.begin() as c:
        got = {r[0]: r[1] for r in c.execute(text("select id, v from t"))}
    assert got == {1: "new", 2: "upd", 5: "kept"} and n == 3                      # upserts are not allowed here, so ids 4 and 5 were not written


def test_rank_documented_example_standard_and_dense():
    rows = [{"PTS": 20}, {"PTS": 30}, {"PTS": 30}, {"PTS": 10}]
    script = """source(output(PTS as integer)) ~> PruneColumns
PruneColumns
    rank(
        desc(PTS, true),
        caseInsensitive: false,
        output(pointsRanking as long),
        dense: {d}
    ) ~> RankByPoints
RankByPoints sink() ~> o"""
    assert [r["pointsRanking"] for r in run(script.replace("{d}", "false"), PruneColumns=rows)["o"]] == [3, 1, 1, 4]       # ties share a rank and the next rank is skipped
    assert [r["pointsRanking"] for r in run(script.replace("{d}", "true"), PruneColumns=rows)["o"]] == [2, 1, 1, 3]        # dense: no gap after a tie


# ---- window, pivot, unpivot, flatten -------------------------------------------------------------------------------------------------------

def test_window_analytic_functions_and_partition_aggregates():
    rows = [{"g": "a", "v": 10}, {"g": "a", "v": 30}, {"g": "a", "v": 30}, {"g": "a", "v": 20}, {"g": "b", "v": 5}]
    out = run("""source(output(g as string, v as integer)) ~> s
s window(over(g),
    desc(v, true),
    rn = rowNumber(),
    rk = rank(),
    dr = denseRank(),
    prev = lag(v),
    nxt = lead(v, 1, -1),
    best = max(v),
    cd = cumeDist(),
    bucket = nTile(2)) ~> w
w sink() ~> o""", s=rows)["o"]
    assert [r["rk"] for r in out] == [4, 1, 1, 3, 1] and [r["dr"] for r in out] == [3, 1, 1, 2, 1]
    assert [r["best"] for r in out] == [30, 30, 30, 30, 5] and [r["rn"] for r in out] == [4, 1, 2, 3, 1]          # rows come back in their incoming order
    assert [r["prev"] for r in out] == [20, None, 30, 30, None] and [r["nxt"] for r in out] == [-1, 30, 20, 10, -1]
    assert [r["cd"] for r in out] == [1.0, 0.5, 0.5, 0.75, 1.0] and [r["bucket"] for r in out] == [2, 1, 1, 2, 1]


def test_window_latest_per_key_like_the_corpus_pattern():
    rows = [{"k": 1, "t": "2020-01-01"}, {"k": 1, "t": "2021-01-01"}, {"k": 2, "t": "2019-05-05"}]
    out = run("""source(output(k as integer, t as string)) ~> s
s window(over(k),
    desc(t, true),
    latest = max(t)) ~> w
w filter(t == latest) ~> f
f sink() ~> o""", s=rows)["o"]
    assert [(r["k"], r["t"]) for r in out] == [(1, "2021-01-01"), (2, "2019-05-05")]


def test_pivot_documented_shape_with_column_naming():
    rows = [{"Tm": "A", "Pos": "G", "x": 1}, {"Tm": "A", "Pos": "G", "x": 2}, {"Tm": "A", "Pos": "F", "x": 5}, {"Tm": "B", "Pos": "G", "x": 7}]
    out = run("""source(output(Tm as string, Pos as string, x as integer)) ~> BasketballPlayerStats
BasketballPlayerStats pivot(groupBy(Tm),
    pivotBy(Pos),
    {} = count(),
    columnNaming: '$V$N count',
    lateral: true) ~> PivotExample
PivotExample sink() ~> o""", BasketballPlayerStats=rows)["o"]
    assert out == [{"Tm": "A", "F count": 1, "G count": 2}, {"Tm": "B", "F count": None, "G count": 1}]        # no rows for a value: null (Spark's pivot; unverified for ADF)
    named = run("""source(output(Tm as string, Pos as string, x as integer)) ~> s
s pivot(groupBy(Tm), pivotBy(Pos, ['G']), total = sum(x), columnNaming: '$V$N', lateral: true) ~> p
p sink() ~> o""", s=rows)["o"]
    assert named == [{"Tm": "A", "Gtotal": 3}, {"Tm": "B", "Gtotal": 7}]


def test_unpivot_turns_columns_into_rows():
    rows = [{"id": 1, "us": 10, "cn": None, "fr": 3}, {"id": 2, "us": 4, "cn": 8, "fr": None}]
    out = run("""source(output(id as integer, us as integer, cn as integer, fr as integer)) ~> s
s unpivot(output(Pais as string, Precio as short),
    ungroupBy(id),
    pivotValues: ['us', 'cn'],
    lateral: true,
    ignoreNullPivots: true) ~> u
u sink() ~> o""", s=rows)["o"]
    assert out == [{"id": 1, "Pais": "us", "Precio": 10}, {"id": 2, "Pais": "us", "Precio": 4}, {"id": 2, "Pais": "cn", "Precio": 8}]


FLATTEN_DOC = [
    {"name": "MSFT", "location": "Redmond", "goods": {"orders": [
        {"orderId": 1, "shipped": {"orderItems": [{"itemName": "Laptop", "itemQty": 20}, {"itemName": "Charger", "itemQty": 2}]}},
        {"orderId": 2, "shipped": {"orderItems": [{"itemName": "Mice", "itemQty": 2}]}}]}},
    {"name": "Company2", "location": "Bellevue", "goods": {"orders": [{"orderId": 4}]}},
    {"name": "Company3", "location": "Kirkland"},
]


def test_flatten_documented_example_with_an_unroll_root():
    out = run("""source(output(name as string)) ~> source
source foldDown(unroll(goods.orders.shipped.orderItems, goods.orders),
    mapColumn(
        name,
        orderId = goods.orders.orderId,
        itemName = goods.orders.shipped.orderItems.itemName,
        itemQty = goods.orders.shipped.orderItems.itemQty,
        location = location
    ),
    skipDuplicateMapInputs: false,
    skipDuplicateMapOutputs: false) ~> flat
flat sink() ~> o""", source=FLATTEN_DOC)["o"]
    assert [(r["name"], r["orderId"], r["itemName"]) for r in out] == [("MSFT", 1, "Laptop"), ("MSFT", 1, "Charger"), ("MSFT", 2, "Mice"), ("Company2", 4, None)]
    # a row with no items in the unroll root (Company3) is dropped, as the page says


def test_flatten_without_a_root_keeps_rows_whose_array_is_missing():
    out = run("""source(output(name as string)) ~> source
source foldDown(unroll(goods.orders),
    mapColumn(name, orderId = goods.orders.orderId)) ~> flat
flat sink() ~> o""", source=FLATTEN_DOC)["o"]
    assert [(r["name"], r["orderId"]) for r in out] == [("MSFT", 1), ("MSFT", 2), ("Company2", 4), ("Company3", None)]       # a null or empty array gives one row with nulls


def test_the_remaining_unsupported_forms_are_named():
    for body, word in [("s window(over(a), asc(a, true), startRowOffset: -1L, endRowOffset: 0L, x = sum(a)) ~> w", "bounded frame"),
                       ("s pivot(groupBy(a), pivotBy(b), n = count()) ~> w", "columnNaming"),
                       ("s foldDown(unroll(a), mapColumn(each(match(true())))) ~> w", "rule-based"),
                       ("s derive(x = rank()) ~> w", "outside a window")]:
        _, nodes = df.parse_script("source(output(a as integer, b as string)) ~> s\n" + body + "\nw sink() ~> o")
        assert any(word in why for why in df.check_flow(nodes)), body


def test_pivot_column_names_match_what_a_real_flow_references_downstream():
    """From a public flow (df_cases_and_deaths): pivotBy(indicator, ['confirmed_cases', 'deaths']), count = sum(daily_count), columnNaming '$V$N', and a later select that
    reads `confirmed_casescount` and `deathscount`. The real flow's own references confirm that $V is the value and $N the pivoted column's name."""
    rows = [{"country": "ES", "reported_date": "d1", "indicator": "confirmed_cases", "daily_count": 5},
            {"country": "ES", "reported_date": "d1", "indicator": "confirmed_cases", "daily_count": 7},
            {"country": "ES", "reported_date": "d1", "indicator": "deaths", "daily_count": 1}]
    out = run("""source(output(country as string, reported_date as string, indicator as string, daily_count as integer)) ~> s
s pivot(groupBy(country,
          reported_date),
     pivotBy(indicator, ['confirmed_cases', 'deaths']),
     count = sum(daily_count),
     columnNaming: '$V$N',
     lateral: true) ~> PivotCounts
PivotCounts select(mapColumn(
          country,
          cases_count = confirmed_casescount,
          deaths_count = deathscount,
          reported_date)) ~> SelectForSink
SelectForSink sink() ~> o""", s=rows)["o"]
    assert out == [{"country": "ES", "cases_count": 12, "deaths_count": 1, "reported_date": "d1"}]


def test_unpivot_key_column_holds_the_column_name_as_a_real_flow_assumes():
    """From a public flow (13-UnPivot): unpivot(output(Pais as string, ...), ungroupBy(...), pivotValues: ['Ventas EE.UU. KG', ...]) is followed by a derive that cuts the
    country out of `Pais` with substring between ' ' and ' KG', so the key column holds the unpivoted column's name."""
    out = run("""source(output(Empresas as string)) ~> s
s unpivot(output(Pais as string, {Precio(Kg)} as short),
     ungroupBy(Empresas),
     pivotValues: ['Ventas EE.UU. KG', 'Ventas China KG'],
     lateral: true,
     ignoreNullPivots: true) ~> u
u derive(PAIS = substring(Pais, locate(' ', Pais) + 1, locate(' KG', Pais) - locate(' ', Pais) - 1)) ~> d
d select(mapColumn(Empresas, Pais = d@PAIS, {Precio(Kg)})) ~> sel
sel sink() ~> o""", s=[{"Empresas": "Acme", "Ventas EE.UU. KG": "5", "Ventas China KG": None}])["o"]
    assert out == [{"Empresas": "Acme", "Pais": "EE.UU.", "Precio(Kg)": 5}]            # derive made PAIS beside Pais, as the real flow's qualified select implies
