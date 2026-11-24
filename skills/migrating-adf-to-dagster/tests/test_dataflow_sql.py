"""Data flow -> SQL: each flow runs through the inline executor and through the SQL that goes to the registry's warehouse_pipeline, and the rows must agree.

The SQL is rendered by the component's own step compiler (`_compile_pipeline`, dialect duckdb for its quoting) and run on sqlite, so the check covers the component's
CTE layout as well as this module's SELECTs.
"""
import sys

import pytest
from sqlalchemy import create_engine, text

from adf_to_dagster import dflow as df
from adf_to_dagster import dflow_sql as ds

dcc = pytest.importorskip("dagster_community_components")
WP = sys.modules[dcc._load_real_class("WarehousePipelineComponent").__module__]


class Mem:
    def __init__(self, **t):
        self.t, self.out = t, {}

    def read(self, name, schema):
        return self.t[name]

    def write(self, name, rows, opts):
        self.out[name] = rows
        return len(rows)


def both(script, tables, params=None, dialect="duckdb"):
    """(inline rows per sink, SQL rows per sink) for the same data. `tables`: source name -> (columns [(name, sqltype)], rows)."""
    mem = Mem(**{k: v[1] for k, v in tables.items()})
    df.run_flow(script, mem, params)
    _, nodes = df.parse_script(script)
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        for name, (cols, rows) in tables.items():
            c.execute(text(f'create table "src_{name}" (' + ", ".join(f'"{n}" {t}' for n, t in cols) + ")"))
            for r in rows:
                c.execute(text(f'insert into "src_{name}" values (' + ", ".join(f":{i}" for i in range(len(cols))) + ")"), {str(i): r.get(n) for i, (n, _) in enumerate(cols)})
    sinks = {n.name: f"out_{n.name}" for n in nodes if n.kind == "sink"}
    plan = ds.compile_flow(nodes, {n: {"table": f"src_{n}"} for n in tables}, {k: v for k, v in sinks.items()}, dialect="duckdb", params=params)
    # the component renders the CTE chain; each sink selects from its step
    ctes, refs = WP._compile_pipeline(plan["steps"], "duckdb")
    got = {}
    for sink in plan["sinks"]:
        body = "WITH " + ", ".join(f'"{n}" AS ({b})' for n, b in ctes) + f' SELECT * FROM "{refs[sink["from"]]}"'
        with eng.connect() as c:
            res = c.execute(text(body))
            got[next(k for k, v in sinks.items() if v == sink["table"])] = [dict(zip(res.keys(), r)) for r in res.fetchall()]
    return mem.out, got, plan


def same(a, b, key=None):
    norm = lambda rows: sorted((tuple(sorted((k, (round(v, 6) if isinstance(v, float) else v)) for k, v in r.items())) for r in rows), key=repr)
    assert norm(a) == norm(b)


SALES = ([("id", "integer"), ("region", "text"), ("amount", "real")],
         [{"id": 1, "region": "east", "amount": 10.5}, {"id": 2, "region": "west", "amount": 3.0}, {"id": 3, "region": "east", "amount": 20.0},
          {"id": 4, "region": "north", "amount": None}, {"id": 5, "region": "west", "amount": 7.25}])
CUST = ([("cid", "integer"), ("name", "text")], [{"cid": 1, "name": "Ann"}, {"cid": 3, "name": "Cy"}, {"cid": 9, "name": "Zed"}])
HEAD = "source(output(id as integer, region as string, amount as double)) ~> sales\n"


def test_filter_derive_select_aggregate_agree_with_the_executor():
    script = HEAD + """sales filter(amount > 4 && region != 'north') ~> f
f derive(double = amount * 2, label = upper(region) + '!', big = iif(amount > 10, 'yes', 'no')) ~> d
d aggregate(groupBy(region),
    total = sum(double), n = count(id), top = max(amount), mean = avg(amount)) ~> agg
agg select(mapColumn(grupo = region, total, n, mean)) ~> sel
sel sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES})
    same(inline["out"], sql["out"])
    assert len(sql["out"]) == 2


def test_derive_keeps_untouched_columns_and_replaces_by_exact_name():
    script = HEAD + """sales derive(amount = amount + 1, extra = id * 10) ~> d
d sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES})
    same(inline["out"], sql["out"])


@pytest.mark.parametrize("jt", ["inner", "left", "right", "outer"])
def test_joins_agree_including_unmatched_rows(jt):
    script = HEAD + f"""source(output(cid as integer, name as string)) ~> cust
sales, cust join(id == cid,
    joinType:'{jt}',
    broadcast: 'auto')~> J
J select(mapColumn(id, cid, name, amount)) ~> S
S sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES, "cust": CUST})
    same(inline["out"], sql["out"])


def test_join_with_clashing_names_uses_stream_qualified_columns():
    script = """source(output(id as integer, v as string)) ~> A
source(output(id as integer, v as string)) ~> B
A, B join(A@id == B@id, joinType:'inner') ~> J
J select(mapColumn(id = A@id, left_v = A@v, right_v = B@v)) ~> S
S sink(truncate: true) ~> out"""
    t = {"A": ([("id", "integer"), ("v", "text")], [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]), "B": ([("id", "integer"), ("v", "text")], [{"id": 2, "v": "x"}])}
    inline, sql, _ = both(script, t)
    same(inline["out"], sql["out"])
    assert sql["out"] == [{"id": 2, "left_v": "b", "right_v": "x"}]


def test_union_by_name_and_conditional_split_agree():
    script = HEAD + """source(output(id as integer, region as string, amount as double)) ~> more
sales, more union(unionBy: 'name') ~> U
U split(amount > 10,
    region == 'west',
    disjoint: false) ~> Sp@(big, west, rest)
Sp@big sink(truncate: true) ~> o1
Sp@west sink(truncate: true) ~> o2
Sp@rest sink(truncate: true) ~> o3"""
    more = ([("id", "integer"), ("region", "text"), ("amount", "real")], [{"id": 6, "region": "west", "amount": 50.0}, {"id": 7, "region": "south", "amount": None}])
    inline, sql, _ = both(script, {"sales": SALES, "more": more})
    for k in ("o1", "o2", "o3"):
        same(inline[k], sql[k])
    assert len(sql["o1"]) == 3 and len(sql["o2"]) == 2 and len(sql["o3"]) == 2


def test_disjoint_split_sends_a_row_to_every_matching_stream():
    script = HEAD + """sales split(amount > 5,
    region == 'west',
    disjoint: true) ~> Sp@(a, b, rest)
Sp@a sink(truncate: true) ~> o1
Sp@b sink(truncate: true) ~> o2
Sp@rest sink(truncate: true) ~> o3"""
    inline, sql, _ = both(script, {"sales": SALES})
    for k in ("o1", "o2", "o3"):
        same(inline[k], sql[k])


def test_null_comparison_and_three_valued_logic_agree():
    script = HEAD + """sales filter(!(amount > 5) || region == 'north') ~> f
f derive(flag = isNull(amount)) ~> d
d sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES})
    same([{k: (bool(v) if k == "flag" else v) for k, v in r.items()} for r in inline["out"]], [{k: (bool(v) if k == "flag" else v) for k, v in r.items()} for r in sql["out"]])


def test_parameters_become_literals_and_missing_ones_refuse():
    script = HEAD + """sales filter(region == $wanted) ~> f
f sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES}, params={"wanted": "west"})
    same(inline["out"], sql["out"])
    with pytest.raises(df.Unsupported):
        ds.compile_flow(df.parse_script(script)[1], {"sales": {"table": "s"}}, {"out": "o"}, "mssql", {})


@pytest.mark.parametrize("script,why", [
    (HEAD + "sales derive(x = amount + region) ~> d\nd sink(truncate: true) ~> o", "text and a number"),
    (HEAD + "sales derive(x = toInteger(region)) ~> d\nd sink(truncate: true) ~> o", "try-cast"),
    (HEAD + "sales derive(x = trim(region, '-')) ~> d\nd sink(truncate: true) ~> o", "character set"),
    (HEAD + "sales derive(x = mystery) ~> d\nd sink(truncate: true) ~> o", "not known"),
    (HEAD + "sales sink() ~> o", "no truncate or recreate"),
    ("source(allowSchemaDrift: true) ~> s\ns sink(truncate: true) ~> o", "declared columns"),
])
def test_what_has_no_faithful_sql_is_refused_with_a_reason(script, why):
    with pytest.raises(df.Unsupported) as e:
        ds.compile_flow(df.parse_script(script)[1], {"sales": {"table": "s"}, "s": {"table": "s"}}, {"o": "o"}, "sqlite")
    assert why in str(e.value)


@pytest.mark.parametrize("opts,mode", [("truncate: true", "truncate_insert"), ("recreate: true", "replace"), ("truncate: true, upsertable: true, keys:['id']", "truncate_insert")])
def test_the_sink_options_choose_the_component_mode(opts, mode):
    plan = ds.compile_flow(df.parse_script(HEAD + f"sales sink({opts}) ~> o")[1], {"sales": {"table": "s"}}, {"o": "o"}, "sqlite")
    assert [s["mode"] for s in plan["sinks"]] == [mode]


def test_mssql_flow_uses_brackets_bit_cases_and_try_cast():
    script = HEAD + """sales derive(n = toInteger(amount), t = amount > 5, s = region + 'x') ~> d
d sink(truncate: true) ~> o"""
    plan = ds.compile_flow(df.parse_script(script)[1], {"sales": {"table": "dbo.s"}}, {"o": "dbo.o"}, "mssql")
    sql = plan["steps"][1]["operations"][0]["sql"]
    assert "TRY_CAST([amount] AS INTEGER)" in sql and "CASE WHEN ([amount] > 5) THEN 1 ELSE 0 END" in sql and "([region] + 'x')" in sql


# ---- exists, lookup, rank, window, surrogate key, pivot, unpivot -----------------------------------------------------------------------------

def test_exists_and_negated_exists_agree():
    for neg in ("false", "true"):
        script = HEAD + f"""source(output(cid as integer, name as string)) ~> cust
sales, cust exists(sales@id == cust@cid,
    negate: {neg},
    broadcast: 'auto') ~> E
E sink(truncate: true) ~> out"""
        inline, sql, _ = both(script, {"sales": SALES, "cust": CUST})
        same(inline["out"], sql["out"])


def test_lookup_multiple_and_picked_rows_agree():
    script = HEAD + """source(output(cid as integer, name as string)) ~> cust
sales, cust lookup(id == cid,
    multiple: {m},
    pickup: 'first',
    asc(name, true),
    broadcast: 'auto')~> L
L derive(hit = isMatch()) ~> D
D select(mapColumn(id, region, name, hit)) ~> S
S sink(truncate: true) ~> out"""
    cust2 = ([("cid", "integer"), ("name", "text")], [{"cid": 1, "name": "Zed"}, {"cid": 1, "name": "Ann"}, {"cid": 3, "name": "Cy"}])
    for m in ("true", "false"):
        inline, sql, _ = both(script.replace("{m}", m), {"sales": SALES, "cust": cust2})
        norm = lambda rows: [{k: (bool(v) if k == "hit" else v) for k, v in r.items()} for r in rows]
        same(norm(inline["out"]), norm(sql["out"]))
    assert sorted(r["name"] for r in sql["out"] if r["id"] == 1) == ["Ann"]


def test_lookup_last_picks_the_other_end():
    script = HEAD + """source(output(cid as integer, name as string)) ~> cust
sales, cust lookup(id == cid, multiple: false, pickup: 'last', asc(name, true)) ~> L
L select(mapColumn(id, name)) ~> S
S sink(truncate: true) ~> out"""
    cust2 = ([("cid", "integer"), ("name", "text")], [{"cid": 1, "name": "Zed"}, {"cid": 1, "name": "Ann"}])
    inline, sql, _ = both(script, {"sales": SALES, "cust": cust2})
    same(inline["out"], sql["out"])
    assert [r["name"] for r in sql["out"] if r["id"] == 1] == ["Zed"]


def test_rank_and_window_functions_agree():
    script = HEAD + """sales rank(desc(amount, true),
    caseInsensitive: false,
    output(pts as long),
    dense: false) ~> R
R window(over(region),
    desc(amount, false),
    rn = rowNumber(),
    rk = rank(),
    dr = denseRank(),
    prev = lag(amount),
    nxt = lead(amount, 1, -1),
    best = max(amount),
    total = sum(amount),
    cd = cumeDist(),
    bucket = nTile(2)) ~> W
W sink(truncate: true) ~> out"""
    sales = ([("id", "integer"), ("region", "text"), ("amount", "real")], [{"id": i, "region": r, "amount": a} for i, r, a in
             [(1, "e", 10.0), (2, "e", 30.0), (3, "e", 30.0), (4, "e", 20.0), (5, "w", 5.0), (6, "w", None), (7, "w", 5.0)]])
    inline, sql, _ = both(script, {"sales": sales})
    # row_number and lag/lead depend on how ties are ordered, which neither side defines; compare the ones that do not
    keep = lambda rows: [{k: v for k, v in r.items() if k not in ("rn", "prev", "nxt", "bucket")} for r in rows]
    same(keep(inline["out"]), keep(sql["out"]))


def test_surrogate_key_numbers_every_row_from_the_start_value():
    script = HEAD + """sales keyGenerate(output(key as long),
    startAt: 100L) ~> K
K sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"sales": SALES})
    assert sorted(r["key"] for r in sql["out"]) == [100, 101, 102, 103, 104] == sorted(r["key"] for r in inline["out"])


def test_pivot_with_listed_values_and_unpivot_agree():
    rows = ([("tm", "text"), ("pos", "text"), ("x", "integer")], [{"tm": "A", "pos": "G", "x": 1}, {"tm": "A", "pos": "G", "x": 2}, {"tm": "A", "pos": "F", "x": 5}, {"tm": "B", "pos": "G", "x": 7}])
    script = """source(output(tm as string, pos as string, x as integer)) ~> s
s pivot(groupBy(tm),
    pivotBy(pos, ['G', 'F']),
    total = sum(x),
    n = count(),
    columnNaming: '$V$N',
    lateral: true) ~> p
p sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"s": rows})
    same(inline["out"], sql["out"])
    assert {"tm": "B", "Gtotal": 7, "Gn": 1, "Ftotal": None, "Fn": None} in sql["out"]                  # no rows for a value: null, even for count
    wide = ([("id", "integer"), ("us", "integer"), ("cn", "integer")], [{"id": 1, "us": 10, "cn": None}, {"id": 2, "us": 4, "cn": 8}])
    script = """source(output(id as integer, us as integer, cn as integer)) ~> s
s unpivot(output(Pais as string, Precio as integer),
    ungroupBy(id),
    pivotValues: ['us', 'cn'],
    lateral: true,
    ignoreNullPivots: true) ~> u
u sink(truncate: true) ~> out"""
    inline, sql, _ = both(script, {"s": wide})
    same(inline["out"], sql["out"])
    assert len(sql["out"]) == 3


def test_a_pivot_whose_values_come_from_the_data_stays_inline():
    script = """source(output(tm as string, pos as string, x as integer)) ~> s
s pivot(groupBy(tm), pivotBy(pos), total = sum(x), columnNaming: '$V$N', lateral: true) ~> p
p sink(truncate: true) ~> out"""
    with pytest.raises(df.Unsupported) as e:
        ds.compile_flow(df.parse_script(script)[1], {"s": {"table": "s"}}, {"out": "o"}, "mssql")
    assert "listed" in str(e.value)


# ---- the registry component itself, on a real engine (duckdb) ---------------------------------------------------------------------------------

def test_the_warehouse_pipeline_component_runs_the_compiled_flow_and_matches_the_executor(tmp_path, monkeypatch):
    pytest.importorskip("duckdb_engine")
    import dagster as dg
    url = f"duckdb:///{tmp_path / 'wh.duckdb'}"
    monkeypatch.setenv("WH_URL", url)
    eng = create_engine(url)
    orders = [{"id": i, "cid": c, "amount": a} for i, c, a in [(1, 1, 10.0), (2, 1, 30.0), (3, 2, 7.5), (4, 3, 99.0), (5, 9, 50.0)]]
    custs = [{"cid": 1, "name": "Ann", "region": "east"}, {"cid": 2, "name": "Bo", "region": "west"}, {"cid": 3, "name": "Cy", "region": "east"}]
    with eng.begin() as c:
        c.execute(text("create table orders (id integer, cid integer, amount double)"))
        c.execute(text("create table customers (cid integer, name varchar, region varchar)"))
        for r in orders:
            c.execute(text("insert into orders values (:id, :cid, :amount)"), r)
        for r in custs:
            c.execute(text("insert into customers values (:cid, :name, :region)"), r)
    script = """parameters{ minimum as integer (5) }
source(output(id as integer, cid as integer, amount as double), format: 'table') ~> orders
source(output(cid as integer, name as string, region as string), format: 'table') ~> customers
orders, customers join(orders@cid == customers@cid, joinType:'inner', broadcast: 'auto') ~> j
j filter(amount > $minimum) ~> big
big select(mapColumn(id, name, region, amount)) ~> bigout
big aggregate(groupBy(region), total = sum(amount), n = count()) ~> summ
bigout sink(truncate: true) ~> sinkBig
summ sink(truncate: true) ~> sinkSummary"""
    params = {"minimum": 8}
    mem = Mem(orders=orders, customers=custs)
    df.run_flow(script, mem, params)
    _, nodes = df.parse_script(script)
    plan = ds.compile_flow(nodes, {"orders": {"table": "orders"}, "customers": {"table": "customers"}}, {"sinkBig": "big_orders", "sinkSummary": "region_summary"}, "duckdb", params)
    for sk in plan["sinks"]:
        sk["asset_name"] = sk["table"]
    from dagster.components.testing import create_defs_folder_sandbox
    cls = dcc._load_real_class("WarehousePipelineComponent")
    with create_defs_folder_sandbox() as sandbox:
        path = sandbox.scaffold_component(component_cls=cls, defs_yaml_contents={
            "type": f"{cls.__module__}.{cls.__name__}",
            "attributes": {"asset_name": "flow", "dialect": "duckdb", "database_url_env_var": "WH_URL", "steps": plan["steps"], "sinks": plan["sinks"]}})
        with sandbox.load_component_and_build_defs(defs_path=path) as (_, defs):
            result = dg.materialize(list(defs.assets), raise_on_error=True)
            assert dg.materialize(list(defs.assets), raise_on_error=True).success          # truncate_insert: a second run replaces the rows, it does not add to them
    assert result.success
    with eng.connect() as c:
        got = {t: [dict(r._mapping) for r in c.execute(text(f"select * from {t}"))] for t in ("big_orders", "region_summary")}
    same(mem.out["sinkBig"], got["big_orders"])
    same(mem.out["sinkSummary"], got["region_summary"])
    assert len(got["big_orders"]) == 3 and {r["region"] for r in got["region_summary"]} == {"east"}
