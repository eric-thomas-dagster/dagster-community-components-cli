"""Mapping Data Flow -> SQL: the same parsed script `dflow.py` runs in Python, compiled to one SELECT per transformation.

The registry's `warehouse_pipeline` component takes `steps` (each a named CTE, with `source` a table, query or `ref` to an earlier step, and operations) and `sinks`
(one CTAS per sink, one Dagster asset per sink). Its `sql` operation accepts a single SELECT written over `<<self>>` (the step's previous CTE) and `<<step_id>>`
(another step), so every ADF transformation can be a step, including the ones it has no named operation for (window, pivot, lookup, exists, split ...).

This module is converter-side only (it is not copied into generated projects). It follows the same rules as the executor and is checked against it: the tests run
each flow through both and compare the rows. A flow, expression or option that has no faithful SQL here raises `Unsupported`, and the flow stays on the inline runner.

Columns must be known: every stream carries its column list and types, taken from the sources' declared `output(...)` projections and propagated through each
transformation. A flow that relies on schema drift (columns it never declares) is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .dflow import AGG, Node, Unsupported, check_call_args, parse_expr, plan_node

DIALECTS = {"mssql", "duckdb", "postgres", "postgresql", "snowflake", "databricks", "bigquery", "mysql", "redshift", "sqlite"}
SAFE_CAST = {"mssql": "TRY_CAST", "duckdb": "TRY_CAST", "snowflake": "TRY_CAST", "databricks": "TRY_CAST", "bigquery": "SAFE_CAST"}   # a failed conversion is null, as in ADF

_NUM = {"short", "integer", "long", "float", "double"}
_TYPE_SQL = {
    "string": {"mssql": "VARCHAR(MAX)", "bigquery": "STRING", "mysql": "CHAR", "databricks": "STRING"},
    "integer": {"bigquery": "INT64", "mysql": "SIGNED"},
    "long": {"bigquery": "INT64", "mysql": "SIGNED", "mssql": "BIGINT"},
    "short": {"bigquery": "INT64", "mysql": "SIGNED", "mssql": "SMALLINT"},
    "double": {"mssql": "FLOAT", "bigquery": "FLOAT64", "mysql": "DOUBLE", "snowflake": "FLOAT", "databricks": "DOUBLE", "duckdb": "DOUBLE"},
    "float": {"mssql": "REAL", "bigquery": "FLOAT64", "mysql": "FLOAT"},
    "date": {}, "timestamp": {"mssql": "DATETIME2", "bigquery": "TIMESTAMP"},
}
_TYPE_DEFAULT = {"string": "VARCHAR", "integer": "INTEGER", "long": "BIGINT", "short": "SMALLINT", "double": "DOUBLE PRECISION", "float": "REAL", "date": "DATE", "timestamp": "TIMESTAMP"}
_CONV = {"tostring": "string", "tointeger": "integer", "tolong": "long", "toshort": "short", "todouble": "double", "tofloat": "float", "todate": "date", "totimestamp": "timestamp"}


def norm_type(t: str | None) -> str | None:
    """An ADF column type reduced to the kinds this module reasons about."""
    if not t:
        return None
    t = t.strip().lower()
    if t.startswith("decimal"):
        return "decimal"
    return t if t in _NUM | {"string", "boolean", "date", "timestamp", "decimal"} else None


def _numeric(t) -> bool:
    return t in _NUM or t == "decimal"


@dataclass
class Rel:
    """A stream: the step id that produces it and its columns, in order, as (name, normalized type)."""
    step: str
    cols: list[tuple[str, str | None]]

    def has(self, name: str) -> bool:
        return any(c.lower() == name.lower() for c, _ in self.cols)

    def type_of(self, name: str):
        return next((t for c, t in self.cols if c.lower() == name.lower()), None)


@dataclass
class Scope:
    """How a column name is written in SQL: for one input, `"col"`; in a join condition, through the two aliases."""
    dialect: str
    params: dict
    rels: dict[str, tuple[str, Rel]] = field(default_factory=dict)       # stream name -> (alias, rel); one entry (alias "") for a single input
    ambiguous: set = field(default_factory=set)

    def q(self, ident: str) -> str:
        if self.dialect == "mssql":
            return "[" + ident.replace("]", "]]") + "]"
        if self.dialect in ("mysql", "bigquery", "databricks"):
            return "`" + ident.replace("`", "``") + "`"
        return '"' + ident.replace('"', '""') + '"'

    def col(self, name: str, stream: str | None):
        if stream is not None:
            hit = self.rels.get(stream)
            if hit is None:
                # a qualified name inside a single-input scope: the column as the join step exposed it
                if len(self.rels) == 1:
                    alias, rel = next(iter(self.rels.values()))
                    for cand in (f"{stream}@{name}", name):
                        if rel.has(cand):
                            return (alias + "." if alias else "") + self.q(next(c for c, _ in rel.cols if c.lower() == cand.lower())), rel.type_of(cand)
                raise Unsupported(f"the stream {stream!r} is not an input here")
            alias, rel = hit
            if not rel.has(name):
                raise Unsupported(f"column {name!r} is not in stream {stream!r}")
            return (alias + "." if alias else "") + self.q(next(c for c, _ in rel.cols if c.lower() == name.lower())), rel.type_of(name)
        found = [(alias, rel) for alias, rel in self.rels.values() if rel.has(name)]
        if not found:
            raise Unsupported(f"column {name!r} is not known (a flow that relies on schema drift stays on the inline runner)")
        if len(found) > 1:
            raise Unsupported(f"column {name!r} is in both join inputs: name its stream (Stream@{name})")
        alias, rel = found[0]
        return (alias + "." if alias else "") + self.q(next(c for c, _ in rel.cols if c.lower() == name.lower())), rel.type_of(name)


def _lit_str(s: str, dialect: str) -> str:
    if dialect in ("mysql", "bigquery", "databricks") and "\\" in s:
        raise Unsupported("a string literal with a backslash is not translated for this dialect")
    return "'" + s.replace("'", "''") + "'"


class Sql:
    """Emit the expression tree as SQL. `emit(tree)` returns (sql, type); a boolean in a value position is wrapped for dialects with no boolean type."""

    def __init__(self, scope: Scope, agg_ok: bool = False, win: tuple | None = None, agg_filter: str | None = None):
        self.s, self.agg_ok = scope, agg_ok or win is not None
        self.d = scope.dialect
        self.win = win                    # (PARTITION BY list, ORDER BY text) inside a window transformation
        self.agg_filter = agg_filter      # a pivot's "this pivot value" predicate, applied inside every aggregate

    # a predicate (filter, CASE WHEN, join ON)
    def pred(self, tree) -> str:
        return self.emit(tree)[0]

    # a boolean used as a value (a selected column)
    def value(self, tree) -> tuple[str, str | None]:
        sql, t = self.emit(tree)
        if t == "boolean" and self.d == "mssql" and not (tree[0] == "col"):
            return f"(CASE WHEN {sql} THEN 1 ELSE 0 END)", t
        return sql, t

    def emit(self, n):
        k = n[0]
        if k == "num":
            return n[1], "double" if "." in n[1] else "integer"
        if k == "str":
            return _lit_str(n[1], self.d), "string"
        if k == "lit":
            if n[1] is None:
                return "NULL", None
            return ("(1 = 1)" if n[1] else "(1 = 0)"), "boolean"
        if k == "col":
            return self.s.col(n[1], n[2])
        if k == "param":
            if n[1] not in self.s.params:
                raise Unsupported(f"the data flow parameter ${n[1]} has no literal value to put in SQL")
            v = self.s.params[n[1]]
            if isinstance(v, bool):
                return ("(1 = 1)" if v else "(1 = 0)"), "boolean"
            if isinstance(v, (int, float)):
                return repr(v), "double" if isinstance(v, float) else "integer"
            if isinstance(v, str):
                return _lit_str(v, self.d), "string"
            raise Unsupported(f"the parameter ${n[1]} is not a plain number, string or boolean")
        if k == "not":
            return f"(NOT {self.pred(n[1])})", "boolean"
        if k == "neg":
            sql, t = self.emit(n[1])
            return f"(-{sql})", t
        if k == "bin":
            return self.binary(n)
        return self.call(n)

    def binary(self, n):
        op = n[1]
        if op in ("&&", "||"):
            a, b = self.emit(n[2])[0], self.emit(n[3])[0]
            return f"({a} {'AND' if op == '&&' else 'OR'} {b})", "boolean"
        if op == "<=>":
            raise Unsupported("<=> is not translated to SQL")
        (a, ta), (b, tb) = self.value(n[2]), self.value(n[3])
        if op in ("==", "!=", "<>", "<", ">", "<=", ">="):
            if ta is not None and tb is not None and (ta == "string") != (tb == "string"):
                raise Unsupported("comparing a string with a non-string is not translated (SQL would convert implicitly)")
            return f"({a} {'=' if op == '==' else '<>' if op == '!=' else op} {b})", "boolean"
        if op == "+":
            if ta == "string" or tb == "string":
                if ta != tb and (_numeric(ta) or _numeric(tb) or "boolean" in (ta, tb)):
                    raise Unsupported("+ between text and a number is not translated (SQL would convert implicitly or fail)")
                return (f"({a} + {b})" if self.d == "mssql" else f"({a} || {b})"), "string"
            if _numeric(ta) and _numeric(tb):
                return f"({a} + {b})", self.arith(ta, tb)
            raise Unsupported("+ between columns of unknown types is not translated (it is a sum for numbers and a join for text)")
        if op in ("-", "*", "%"):
            if not (_numeric(ta) and _numeric(tb)):
                raise Unsupported(f"{op} between columns of unknown types is not translated")
            return f"({a} {op} {b})", self.arith(ta, tb)
        if op == "/":
            if not (_numeric(ta) and _numeric(tb)):
                raise Unsupported("/ between columns of unknown types is not translated")
            return f"(1.0 * {a} / {b})", "double"                     # data flows divide to a fractional result
        raise Unsupported(f"operator {op} is not translated")

    @staticmethod
    def arith(a, b):
        if "double" in (a, b) or "float" in (a, b):
            return "double"
        if "decimal" in (a, b):
            return "decimal"
        return "long" if "long" in (a, b) else "integer"

    def call(self, n):
        _, low, args, name = n
        d = self.d
        if low in AGG:
            return self.aggregate(low, args, name)
        if low in ("rank", "denserank", "rownumber", "cumedist", "ntile", "lag", "lead"):
            return self.analytic(low, args, name)
        if low == "ismatch":
            return f"({self.s.col('__isMatch__', None)[0]} = 1)", "boolean"
        check_call_args(name, low, args)
        if low == "iif":
            cond = self.pred(args[0])
            (t, tt), (f, tf) = self.value(args[1]), (self.value(args[2]) if len(args) > 2 else ("NULL", None))
            return f"(CASE WHEN {cond} THEN {t} ELSE {f} END)", tt or tf
        if low == "case":
            parts, typ = [], None
            rest = args
            while len(rest) >= 2:
                v, tv = self.value(rest[1])
                parts.append(f"WHEN {self.pred(rest[0])} THEN {v}")
                typ = typ or tv
                rest = rest[2:]
            default = ""
            if rest:
                dv, td = self.value(rest[0])
                default, typ = f" ELSE {dv}", typ or td
            return f"(CASE {' '.join(parts)}{default} END)", typ
        if low in ("iifnull", "coalesce"):
            vals = [self.value(a) for a in args]
            return f"COALESCE({', '.join(v for v, _ in vals)})", next((t for _, t in vals if t), None)
        vals = [self.value(a) for a in args]
        sql = [v for v, _ in vals]
        types = [t for _, t in vals]
        if low == "isnull":
            return f"({sql[0]} IS NULL)", "boolean"
        if low == "true":
            return "(1 = 1)", "boolean"
        if low == "false":
            return "(1 = 0)", "boolean"
        if low == "not":
            return f"(NOT {sql[0]})", "boolean"
        if low in ("upper", "lower"):
            return f"{low.upper()}({sql[0]})", "string"
        if low in ("trim", "ltrim", "rtrim"):
            if len(args) != 1:
                raise Unsupported(f"{name}() with a character set is not translated to SQL")
            if low == "trim":
                return f"TRIM({sql[0]})", "string"
            return f"{low.upper()}({sql[0]})", "string"
        if low == "length":
            return f"{'LEN' if d == 'mssql' else 'LENGTH'}({sql[0]})", "integer"
        if low == "concat":
            if any(t not in ("string", None) for t in types) or None in types:
                raise Unsupported("concat() over values that are not known to be text is not translated")
            return "(" + (" + " if d == "mssql" else " || ").join(sql) + ")", "string"
        if low == "replace":
            return f"REPLACE({', '.join(sql)})", "string"
        if low == "substring":
            if len(sql) == 2:
                ln = f"LEN({sql[0]})" if d == "mssql" else f"LENGTH({sql[0]})"
                return f"SUBSTRING({sql[0]}, {sql[1]}, {ln})", "string"
            return f"SUBSTRING({', '.join(sql)})", "string"
        if low in ("left", "right"):
            return f"{low.upper()}({', '.join(sql)})", "string"
        if low == "abs":
            return f"ABS({sql[0]})", types[0]
        if low == "ceil":
            return f"{'CEILING' if d == 'mssql' else 'CEIL'}({sql[0]})", types[0]
        if low == "floor":
            return f"FLOOR({sql[0]})", types[0]
        if low in ("year", "month", "dayofmonth"):
            part = {"year": "YEAR", "month": "MONTH", "dayofmonth": "DAY"}[low]
            return (f"{part}({sql[0]})" if d in ("mssql", "mysql", "databricks", "snowflake", "duckdb") else f"EXTRACT({part} FROM {sql[0]})"), "integer"
        if low in _CONV:
            return self.convert(low, args, sql[0], types[0])
        if low == "todecimal":
            p, s = (sql[1], sql[2]) if len(sql) >= 3 else ("38", "18")
            return self.cast(sql[0], f"DECIMAL({p}, {s})", types[0], "decimal"), "decimal"
        raise Unsupported(f"the function {name}() is not translated to SQL")

    def cast(self, x: str, typ: str, src_type, kind: str) -> str:
        """A conversion that gives null when the value does not convert (ADF), with the dialect's try-cast; between types that always convert a plain CAST."""
        safe = SAFE_CAST.get(self.d)
        if safe:
            return f"{safe}({x} AS {typ})"
        always = (_numeric(src_type) and kind in ("string", "double", "float", "decimal", "long", "integer", "short")) or (kind == "string" and src_type is not None)
        if not always:
            raise Unsupported(f"this dialect has no try-cast, so a conversion of {src_type or 'an unknown type'} that can fail is not translated")
        return f"CAST({x} AS {typ})"

    def convert(self, low: str, args, x: str, src_type):
        kind = _CONV[low]
        typ = _TYPE_SQL.get(kind, {}).get(self.d) or _TYPE_DEFAULT[kind]
        if kind in ("date", "timestamp") and len(args) > 1:
            raise Unsupported(f"{low}() with a pattern is not translated to SQL")
        return self.cast(x, typ, src_type, kind), kind

    def aggregate(self, low: str, args, name: str):
        if not self.agg_ok:
            raise Unsupported(f"{name}() outside an aggregate")
        sql, t = self._aggregate(low, args, name)
        if self.win is not None:                                    # over the whole partition, whatever the sort conditions say
            part = f"PARTITION BY {', '.join(self.win[0])}" if self.win[0] else ""
            return f"{sql} OVER ({part})", t
        return sql, t

    def _filtered(self, x: str) -> str:
        return f"CASE WHEN {self.agg_filter} THEN {x} END" if self.agg_filter else x

    def _aggregate(self, low: str, args, name: str):
        d = self.d
        if low == "count":
            if not args:
                return (f"COUNT({self._filtered('1')})" if self.agg_filter else "COUNT(*)"), "long"
            return f"COUNT({self._filtered(self.value(args[0])[0])})", "long"
        x, t = self.value(args[0])
        x = self._filtered(x)
        if low == "countdistinct":
            return f"COUNT(DISTINCT {x})", "long"
        if low in ("sum", "min", "max"):
            return f"{low.upper()}({x})", t
        if low == "avg":
            if not _numeric(t):
                raise Unsupported("avg() of a column that is not known to be numeric is not translated")
            return (f"AVG(1.0 * {x})" if t not in ("double", "float", "decimal") else f"AVG({x})"), "double"
        if low in ("stddev", "variance"):
            fn = {"stddev": ("STDEV" if d == "mssql" else "STDDEV_SAMP"), "variance": ("VAR" if d == "mssql" else "VAR_SAMP")}[low]
            return f"{fn}({x})", "double"
        raise Unsupported(f"{name}() is not translated to SQL (it depends on row order)")

    def analytic(self, low: str, args, name: str):
        if self.win is None:
            raise Unsupported(f"{name}() outside a window transformation")
        part = f"PARTITION BY {', '.join(self.win[0])} " if self.win[0] else ""
        over = f"OVER ({part}ORDER BY {self.win[1]})" if self.win[1] else None
        if over is None:
            raise Unsupported(f"{name}() needs sort conditions in its window")
        if low in ("rank", "denserank", "rownumber", "cumedist"):
            if args:
                raise Unsupported(f"{name}() takes no arguments")
            fn = {"rank": "RANK", "denserank": "DENSE_RANK", "rownumber": "ROW_NUMBER", "cumedist": "CUME_DIST"}[low]
            return f"{fn}() {over}", "double" if low == "cumedist" else "long"
        if low == "ntile":
            n = self.emit(args[0])[0] if args else "1"
            return f"NTILE({n}) {over}", "integer"
        if not args:
            raise Unsupported(f"{name}() needs a value")
        x, t = self.value(args[0])
        off = self.emit(args[1])[0] if len(args) > 1 else "1"
        default = self.value(args[2])[0] if len(args) > 2 else "NULL"
        return f"{low.upper()}({x}, {off}, {default}) {over}", t


def emit_sql(src: str, scope: Scope, agg: bool = False) -> tuple[str, str | None]:
    return Sql(scope, agg).value(parse_expr(src))


def emit_pred(src: str, scope: Scope) -> str:
    return Sql(scope).pred(parse_expr(src))


# ---------------------------------------------------------------------------------------------------------------------------------------------
# transformations: a step is {id, source, operations}; its operation is one `sql` SELECT over <<self>> (the input step) and <<other_step>>
# ---------------------------------------------------------------------------------------------------------------------------------------------

def _single(dialect: str, params: dict, rel: Rel) -> Scope:
    return Scope(dialect, params, {rel.step: ("", rel)})


def _names(cols) -> set[str]:
    return {c.lower() for c, _ in cols}


def step_sql(n: Node, ins: list[Rel], dialect: str, params: dict) -> tuple[str, list[tuple[str, str | None]]]:
    """(SELECT text, output columns) for one transformation over its input streams. `<<self>>` is the first input; further inputs are `<<step id>>`."""
    plan = plan_node(n)
    k = n.kind
    q = Scope(dialect, params).q
    left = ins[0]
    sc = _single(dialect, params, left)
    if k == "filter":
        return f"SELECT * FROM <<self>> WHERE {emit_pred(n.pos[0], sc)}", list(left.cols)
    if k == "derive":
        derived = {}
        for name, expr in _derive_pairs(n):
            sql, t = emit_sql(expr, sc)
            derived[name] = (sql, t)
        cols, parts = [], []
        for c, t in left.cols:
            if c in derived:                                        # an exact name replaces the column
                sql, dt = derived.pop(c)
                parts.append(f"{sql} AS {q(c)}")
                cols.append((c, dt))
            else:
                parts.append(q(c))
                cols.append((c, t))
        for name, (sql, dt) in derived.items():
            if name.lower() in _names(cols):
                raise Unsupported(f"derive {n.name}: {name!r} differs from an existing column only in case, which SQL cannot hold")
            parts.append(f"{sql} AS {q(name)}")
            cols.append((name, dt))
        return f"SELECT {', '.join(parts)} FROM <<self>>", cols
    if k == "select":
        parts, cols = [], []
        for new, old in plan["maps"]:
            stream, col = old.split("@", 1) if "@" in old else (None, old)
            sql, t = sc.col(col, stream)
            parts.append(f"{sql} AS {q(new)}")
            cols.append((new, t))
        if len({c.lower() for c, _ in cols}) != len(cols):
            raise Unsupported(f"select {n.name}: duplicate output names")
        return f"SELECT {', '.join(parts)} FROM <<self>>", cols
    if k == "aggregate":
        parts, cols, group_sql = [], [], []
        for name, f in plan["groups"]:
            expr = _group_expr(n, name)
            sql, t = emit_sql(expr, sc)
            parts.append(f"{sql} AS {q(name)}")
            group_sql.append(sql)
            cols.append((name, t))
        for name, expr in _agg_pairs(n):
            sql, t = emit_sql(expr, sc, agg=True)
            parts.append(f"{sql} AS {q(name)}")
            cols.append((name, t))
        tail = f" GROUP BY {', '.join(group_sql)}" if group_sql else ""
        return f"SELECT {', '.join(parts)} FROM <<self>>{tail}", cols
    if k == "sort":
        return "SELECT * FROM <<self>>", list(left.cols)                    # row order inside a table is not defined; a flow that depends on it stays inline
    if k == "union":
        right = ins[1]
        if plan["by"] == "name":
            names = list(dict.fromkeys([c for c, _ in left.cols] + [c for c, _ in right.cols]))
            lt, rt = {c.lower(): t for c, t in left.cols}, {c.lower(): t for c, t in right.cols}
            sel = lambda rel: ", ".join((q(next(c for c, _ in rel.cols if c.lower() == nm.lower())) if rel.has(nm) else "NULL") + f" AS {q(nm)}" for nm in names)
            cols = [(nm, lt.get(nm.lower()) or rt.get(nm.lower())) for nm in names]
            return f"SELECT {sel(left)} FROM <<self>> UNION ALL SELECT {sel(right)} FROM <<{right.step}>>", cols
        if len(left.cols) != len(right.cols):
            raise Unsupported(f"union {n.name}: by position needs the same number of columns")
        ls = ", ".join(q(c) for c, _ in left.cols)
        rs = ", ".join(f"{q(rc)} AS {q(lc)}" for (lc, _), (rc, _) in zip(left.cols, right.cols))
        return f"SELECT {ls} FROM <<self>> UNION ALL SELECT {rs} FROM <<{right.step}>>", list(left.cols)
    if k == "join":
        return _join_sql(n, plan, ins, dialect, params)
    if k == "exists":
        right = ins[1]
        ln, rn = (i.split("@")[0] for i in n.inputs)
        two = Scope(dialect, params, {ln: ("_l", left), rn: ("_r", right)})
        cond = emit_pred(n.pos[0], two)
        neg = "NOT " if plan["negate"] else ""
        cols = ", ".join(f"_l.{q(c)} AS {q(c)}" for c, _ in left.cols)
        return f"SELECT {cols} FROM <<self>> AS _l WHERE {neg}EXISTS (SELECT 1 FROM <<{right.step}>> AS _r WHERE {cond})", list(left.cols)
    if k == "keygenerate":
        col = plan["col"]
        if left.has(col):
            raise Unsupported(f"keyGenerate {n.name}: the column {col!r} exists already")
        key = f"(ROW_NUMBER() OVER (ORDER BY {_arbitrary(dialect)}) + {plan['start'] - 1})"
        return f"SELECT *, {key} AS {q(col)} FROM <<self>>", [*left.cols, (col, "long")]
    if k == "rank":
        order = _order_by(n, plan, sc, ci=plan["ci"])
        fn = "DENSE_RANK" if plan["dense"] else "RANK"
        if left.has(plan["col"]):
            raise Unsupported(f"rank {n.name}: the column {plan['col']!r} exists already")
        return f"SELECT *, {fn}() OVER (ORDER BY {order}) AS {q(plan['col'])} FROM <<self>>", [*left.cols, (plan["col"], "long")]
    if k == "window":
        from .dflow import _split_top
        over = [emit_sql(x, sc)[0] for p in n.pos if p.startswith("over(") for x in _split_top(p[5:-1])]
        order = _order_by(n, plan, sc)
        em = Sql(sc, win=(over, order))
        cols, parts = list(left.cols), ["*"]
        for name, expr in _window_pairs(n):
            sql, t = em.value(parse_expr(expr))
            if left.has(name):
                raise Unsupported(f"window {n.name}: the column {name!r} exists already")
            parts.append(f"{sql} AS {q(name)}")
            cols.append((name, t))
        return f"SELECT {', '.join(parts)} FROM <<self>>", cols
    if k == "lookup":
        return _lookup_sql(n, plan, ins, dialect, params)
    if k == "pivot":
        return _pivot_sql(n, plan, left, dialect, params)
    if k == "unpivot":
        return _unpivot_sql(n, plan, left, dialect, params)
    raise Unsupported(f"transformation {k!r} has no SQL translation yet")


def _derive_pairs(n: Node):
    from .dflow import _assignments
    return _assignments(n)


def _agg_pairs(n: Node):
    from .dflow import _assignments
    return _assignments(Node(n.name, "aggregate", [], [p for p in n.pos if not p.startswith("groupBy(")]))


def _group_expr(n: Node, name: str) -> str:
    from .dflow import _split_top
    for p in n.pos:
        if p.startswith("groupBy("):
            for g in _split_top(p[len("groupBy("):-1]):
                m = re.match(r"^(\{[^}]*\}|[A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", g, re.S)
                if m and m.group(1).strip("{}") == name:
                    return m.group(2)
                if not m and g.strip("{}") == name:
                    return g
    raise Unsupported(f"aggregate {n.name}: cannot find the group column {name!r}")


def _join_sql(n: Node, plan: dict, ins: list[Rel], dialect: str, params: dict):
    left, right = ins
    ln, rn = (i.split("@")[0] for i in n.inputs)
    q = Scope(dialect, params).q
    both = _names(left.cols) & _names(right.cols)
    sc = Scope(dialect, params, {ln: ("_l", left), rn: ("_r", right)})
    # a bare name that both streams have is ambiguous in the condition, as in the executor: hide it so it raises
    cond = emit_pred(n.pos[0], sc)
    how = {"inner": "INNER", "left_outer": "LEFT", "right_outer": "RIGHT", "outer": "FULL OUTER", "cross": "INNER"}[plan["type"]]
    parts, cols = [], []
    for c, t in left.cols:
        parts.append(f"_l.{q(c)} AS {q(c)}")
        cols.append((c, t))
    for c, t in right.cols:
        if c.lower() not in both:
            parts.append(f"_r.{q(c)} AS {q(c)}")
            cols.append((c, t))
    for c, t in left.cols:
        if c.lower() in both:
            parts.append(f"_l.{q(c)} AS {q(ln + '@' + c)}")
            cols.append((f"{ln}@{c}", t))
    for c, t in right.cols:
        if c.lower() in both:
            parts.append(f"_r.{q(c)} AS {q(rn + '@' + c)}")
            cols.append((f"{rn}@{c}", t))
    return f"SELECT {', '.join(parts)} FROM <<self>> AS _l {how} JOIN <<{right.step}>> AS _r ON {cond}", cols


# ---------------------------------------------------------------------------------------------------------------------------------------------
# a whole flow -> warehouse_pipeline steps and sinks
# ---------------------------------------------------------------------------------------------------------------------------------------------

def sid(name: str) -> str:
    """A step id for a stream name (stream names may hold spaces; split outputs are `Name@output`)."""
    return re.sub(r"\W", "_", name)


def compile_flow(nodes: list[Node], sources: dict[str, dict], sinks: dict[str, str], dialect: str, params: dict | None = None) -> dict:
    """Steps and sinks for the registry's warehouse_pipeline from a parsed flow.

    `sources` maps a source name to {"table": "schema.t"} or {"sql": "SELECT ..."}; `sinks` maps a sink name to its table. Column lists come from each source's declared
    `output(...)`. Raises Unsupported for anything that has no faithful SQL."""
    from .dflow import source_schema
    if dialect not in DIALECTS:
        raise Unsupported(f"dialect {dialect!r} is not one warehouse_pipeline supports")
    params = params or {}
    rels: dict[str, Rel] = {}
    steps: list[dict] = []
    out_sinks: list[dict] = []
    pending = list(nodes)
    while pending:
        progressed = False
        for n in list(pending):
            keys = [i if i in rels else i.split("@")[0] for i in n.inputs]
            if any(k not in rels for k in keys):
                continue
            pending.remove(n)
            progressed = True
            ins = [rels[k] for k in keys]
            if n.kind == "source":
                schema = source_schema(n)
                if not schema:
                    raise Unsupported(f"source {n.name}: no declared columns (output(...)), so the SQL cannot be checked against them")
                spec = sources[n.name]
                src = {"kind": "table", "table": spec["table"]} if "table" in spec else {"kind": "sql", "sql": spec["sql"]}
                steps.append({"id": sid(n.name), "source": src, "operations": []})
                rels[n.name] = Rel(sid(n.name), [(c, norm_type(t)) for c, t in schema])
            elif n.kind == "sink":
                opts = n.opts
                on = lambda o: str(opts.get(o, "false")).lower() == "true"       # noqa: E731
                # Without an alterRow every row is an insert, so updateable/deletable/upsertable change nothing (alterRow has no SQL translation yet).
                # truncate: delete the rows and insert (the table, its grants and indexes stay); recreate: drop and create the table.
                if on("recreate"):
                    mode = "replace"
                elif on("truncate"):
                    mode = "truncate_insert"
                else:
                    raise Unsupported(f"sink {n.name}: inserting into rows already there (no truncate or recreate) has no warehouse_pipeline mode: merge needs keys and alterRow, incremental needs a watermark column")
                out_sinks.append({"from": ins[0].step, "table": sinks[n.name], "mode": mode})
            elif n.kind == "split":
                plan = plan_node(n)
                sc = _single(dialect, params, ins[0])
                q = Scope(dialect, params).q
                preds = [emit_pred(p, sc) for p in n.pos]
                flag = lambda pr: f"(CASE WHEN {pr} THEN 1 ELSE 0 END)"
                for i, out in enumerate(n.outputs):
                    if i < len(preds):
                        mine = f"{flag(preds[i])} = 1"
                        if not plan["disjoint"]:
                            mine = " AND ".join([f"{flag(p)} = 0" for p in preds[:i]] + [mine])
                    else:
                        mine = " AND ".join(f"{flag(p)} = 0" for p in preds)
                    step = sid(f"{n.name}@{out}")
                    steps.append({"id": step, "source": {"kind": "ref", "ref": ins[0].step}, "operations": [{"op": "sql", "sql": f"SELECT * FROM <<self>> WHERE {mine}"}]})
                    rels[f"{n.name}@{out}"] = Rel(step, list(ins[0].cols))
            else:
                text, cols = step_sql(n, ins, dialect, params)
                step = sid(n.name)
                steps.append({"id": step, "source": {"kind": "ref", "ref": ins[0].step}, "operations": [{"op": "sql", "sql": text}]})
                rels[n.name] = Rel(step, cols)
        if not progressed:
            raise Unsupported("the flow has a cycle or an unknown input stream")
    return {"dialect": dialect, "steps": steps, "sinks": out_sinks}


def _arbitrary(dialect: str) -> str:
    """An ORDER BY that asks for no particular order (row numbering over rows that have none)."""
    return "(SELECT NULL)" if dialect == "mssql" else "1" if dialect in ("postgres", "postgresql", "redshift", "snowflake", "duckdb", "sqlite", "databricks") else "(SELECT NULL)"


def _order_terms(keys: list[tuple[str, bool, bool, str | None]], ci: bool = False) -> str:
    """ORDER BY text for sort conditions (sql, descending, nulls first, type). Nulls are placed by a leading CASE key, since not every dialect has NULLS FIRST."""
    out = []
    for sql, desc, nulls_first, typ in keys:
        out.append(f"CASE WHEN {sql} IS NULL THEN {0 if nulls_first else 1} ELSE {1 if nulls_first else 0} END")
        out.append(f"{'LOWER(' + sql + ')' if ci and typ == 'string' else sql}{' DESC' if desc else ''}")
    return ", ".join(out)


def _order_by(n: Node, plan: dict, sc: Scope, ci: bool = False) -> str:
    """The sort conditions of a rank or window node as ORDER BY text."""
    import re as _re
    from .dflow import _split_top
    keys = []
    for p in n.pos:
        m = _re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, _re.S | _re.I)
        if m:
            sql, t = emit_sql(m.group(2), sc)
            keys.append((sql, m.group(1).lower() == "desc", m.group(3).lower() == "true", t))
    return _order_terms(keys, ci)


def _window_pairs(n: Node):
    from .dflow import _assignments
    return _assignments(Node(n.name, "window", [], [p for p in n.pos if not p.startswith("over(") and not _re_order(p)]))


def _re_order(p: str) -> bool:
    return bool(re.match(r"^(asc|desc)\(", p, re.I))


def _lookup_sql(n: Node, plan: dict, ins: list[Rel], dialect: str, params: dict):
    left, right = ins
    ln, rn = (i.split("@")[0] for i in n.inputs)
    q = Scope(dialect, params).q
    both = _names(left.cols) & _names(right.cols)
    sc = Scope(dialect, params, {ln: ("_l", left), rn: ("_r", right)})
    cond = emit_pred(n.pos[0], sc)
    sel, cols = [], []
    for c, t in left.cols:
        sel.append(f"_l.{q(c)} AS {q(c)}")
        cols.append((c, t))
    for c, t in right.cols:
        if c.lower() not in both:
            sel.append(f"_r.{q(c)} AS {q(c)}")
            cols.append((c, t))
    for c, t in left.cols:
        if c.lower() in both:
            sel.append(f"_l.{q(c)} AS {q(ln + '@' + c)}")
            cols.append((f"{ln}@{c}", t))
    for c, t in right.cols:
        if c.lower() in both:
            sel.append(f"_r.{q(c)} AS {q(rn + '@' + c)}")
            cols.append((f"{rn}@{c}", t))
    sel.append(f"(CASE WHEN _r.{q('__m')} IS NULL THEN 0 ELSE 1 END) AS {q('__isMatch__')}")
    cols.append(("__isMatch__", "integer"))
    rhs = f"(SELECT x.*, 1 AS {q('__m')} FROM <<{right.step}>> AS x) AS _r"
    if plan["multiple"]:
        return f"SELECT {', '.join(sel)} FROM <<self>> AS _l LEFT JOIN {rhs} ON {cond}", cols
    # one match per primary row: number the matches within each primary row and keep the first (or last), in the sort conditions' order
    order_keys = []
    rscope = Scope(dialect, params, {rn: ("_r", right)})
    from .dflow import _split_top
    for p in n.pos[1:]:
        m = re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, re.S | re.I)
        sql, t = emit_sql(m.group(2), rscope)
        desc = m.group(1).lower() == "desc"
        nulls_first = m.group(3).lower() == "true"
        order_keys.append((sql, (not desc, not nulls_first) if plan["pickup"] == "last" else (desc, nulls_first), t))
    order_keys = [(sql, d, nf, t) for sql, (d, nf), t in order_keys]            # 'last' is the first row in the reversed order
    order = _order_terms(order_keys) if order_keys else _arbitrary(dialect)
    inner = ", ".join(sel)
    lid = f"ROW_NUMBER() OVER (ORDER BY {_arbitrary(dialect)})"
    return (f"SELECT {', '.join(q(c) for c, _ in cols)} FROM (SELECT {inner}, ROW_NUMBER() OVER (PARTITION BY _l.{q('__rid')} ORDER BY {order}) AS {q('__rn')} "
            f"FROM (SELECT x.*, {lid} AS {q('__rid')} FROM <<self>> AS x) AS _l LEFT JOIN {rhs} ON {cond}) AS t WHERE {q('__rn')} = 1"), cols


def _pivot_sql(n: Node, plan: dict, left: Rel, dialect: str, params: dict):
    if plan["values"] is None:
        raise Unsupported(f"pivot {n.name}: the pivot values are found from the data when the flow runs; SQL needs them listed")
    q = Scope(dialect, params).q
    sc = _single(dialect, params, left)
    by_sql, by_t = sc.col(plan["by"], None)
    parts, cols = [], []
    for g in plan["groups"]:
        sql, t = sc.col(g, None)
        parts.append(f"{sql} AS {q(g)}")
        cols.append((g, t))
    exprs = []
    for p in n.pos:
        if p.startswith("groupBy(") or p.startswith("pivotBy("):
            continue
        m = re.match(r"^(\{[^}]*\}|\w*)\s*=(?!=)\s*(.*)$", p, re.S)
        exprs.append((m.group(1).strip("{}"), m.group(2)))
    for v in plan["values"]:
        flt = f"{by_sql} = {_lit_str(v, dialect)}" if by_t in ("string", None) else None
        if flt is None:
            raise Unsupported(f"pivot {n.name}: pivoting on a column that is not text is not translated")
        for name, expr in exprs:
            em = Sql(sc, agg_ok=True, agg_filter=flt)
            sql, t = em.value(parse_expr(expr))
            guard = f"CASE WHEN COUNT(CASE WHEN {flt} THEN 1 END) = 0 THEN NULL ELSE {sql} END"        # no rows for this value: null, as a pivot gives
            out = plan["naming"].replace("$N", name).replace("$V", v)
            parts.append(f"{guard} AS {q(out)}")
            cols.append((out, t))
    gb = ", ".join(sc.col(g, None)[0] for g in plan["groups"])
    return f"SELECT {', '.join(parts)} FROM <<self>>" + (f" GROUP BY {gb}" if gb else ""), cols


def _unpivot_sql(n: Node, plan: dict, left: Rel, dialect: str, params: dict):
    q = Scope(dialect, params).q
    (kname, _), (vname, vtype) = plan["key"], plan["value"]
    groups = [next((c for c, _ in left.cols if c.lower() == g.lower()), None) for g in plan["groups"]]
    if None in groups:
        raise Unsupported(f"unpivot {n.name}: an ungroupBy column is not in the input")
    values = plan["values"] if plan["values"] is not None else [c for c, _ in left.cols if c.lower() not in {g.lower() for g in groups}]
    target = norm_type(vtype)
    sx = Sql(_single(dialect, params, left))
    branches = []
    for c in values:
        real = next((x for x, _ in left.cols if x.lower() == c.lower()), None)
        if real is None:
            raise Unsupported(f"unpivot {n.name}: {c!r} is not a column of the input")
        src_t = left.type_of(real)
        val = q(real)
        if target and src_t != target and not (_numeric(src_t) and _numeric(target)):
            val = sx.cast(val, _TYPE_SQL.get(target, {}).get(dialect) or _TYPE_DEFAULT.get(target, "VARCHAR"), src_t, target)
        sel = ", ".join(q(g) for g in groups) + (", " if groups else "") + f"{_lit_str(c, dialect)} AS {q(kname)}, {val} AS {q(vname)}"
        where = f" WHERE {q(real)} IS NOT NULL" if plan["drop_null"] else ""
        branches.append(f"SELECT {sel} FROM <<self>>{where}")
    cols = [(g, left.type_of(g)) for g in groups] + [(kname, "string"), (vname, target)]
    return " UNION ALL ".join(branches), cols
