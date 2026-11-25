"""Transpile a StreamSets JavaScript Evaluator script (ES5-style) to Python source.

The output is an ordinary Python module with `init(state, log, sdc, sdcFunctions)` and `run(records, output, error, state, log, sdc, sdcFunctions)`; JavaScript
semantics that Python lacks (truthiness, `+`, `==` and `===`, `undefined`, string and array methods) are expressed as calls to `jsrt.J`. A construct the transpiler does
not handle raises Unsupported, and the caller falls back to running the script in QuickJS: nothing is ever translated by guessing.
"""
from __future__ import annotations

import keyword
import re

import esprima


class Unsupported(Exception):
    pass


_GLOBAL_OBJECTS = {"Math": "J.Math", "JSON": "J.JSON"}
_GLOBAL_FUNCS = {"parseFloat", "parseInt", "String", "Number", "Boolean", "isNaN", "Error", "TypeError", "RegExp", "Array", "Object"}
_SCRIPT_GLOBALS = {"records", "output", "error", "state", "log", "sdc", "sdcFunctions"}
_RESERVED = set(keyword.kwlist) | {"J", "UNDEF", "JSError", "isinstance", "list", "dict", "len", "str", "int", "float", "print", "id", "type", "object"}
_BINOPS = {"+": "J.add", "-": "J.sub", "*": "J.mul", "/": "J.div", "%": "J.mod", "==": "J.eq", "===": "J.seq", "<": "J.lt", ">": "J.gt", "<=": "J.le", ">=": "J.ge"}


def _py(name: str) -> str:
    if name in _RESERVED or re.fullmatch(r"_[tfeok]\d+", name):
        return name + "_"
    return name


def _walk_vars(node, out: set):
    """var/let/const names and function declarations in this function's own scope (not nested functions)."""
    if isinstance(node, list):
        for x in node:
            _walk_vars(x, out)
        return
    if node is None or not hasattr(node, "type"):
        return
    t = node.type
    if t == "VariableDeclaration":
        for d in node.declarations:
            if d.id.type != "Identifier":
                raise Unsupported("destructuring")
            out.add(d.id.name)
            _walk_vars(d.init, out) if d.init is not None else None
        return
    if t == "FunctionDeclaration":
        out.add(node.id.name)
        return
    if t in ("FunctionExpression", "ArrowFunctionExpression"):
        return
    for k, v in node.__dict__.items():
        if k == "type":
            continue
        if isinstance(v, list) or hasattr(v, "type"):
            _walk_vars(v, out)


class _Fn:
    def __init__(self, params, declared, parent):
        self.params, self.declared, self.parent, self.nonlocals = params, declared, parent, set()

    def owner(self, name):
        f = self
        while f is not None:
            if name in f.declared or name in f.params:
                return f
            f = f.parent
        return None


class Transpiler:
    def __init__(self, implicit: set | None = None, lenient: bool = False):
        self.implicit = set(implicit or ())       # sloppy-mode globals (assigned without var): live in the outermost function
        self.lenient = lenient                    # first pass: collect implicit globals, do not refuse unknown names
        self.root: _Fn | None = None
        self.t = 0
        self.fn: _Fn | None = None
        self.loops: list = []
        self.pre: list = []           # def statements hoisted out of expressions, flushed before the statement being emitted
        self.depth = 0

    def tmp(self, p="_t"):
        self.t += 1
        return f"{p}{self.t}"

    # ---- expressions --------------------------------------------------------------------------------------------------------------------
    def ident(self, name: str) -> str:
        if name == "undefined":
            return "UNDEF"
        if name in ("NaN", "Infinity"):
            return f"J.{name}"
        if self.fn is not None and self.fn.owner(name) is not None:
            return _py(name)
        if name in _SCRIPT_GLOBALS:
            return name
        if name in _GLOBAL_OBJECTS:
            return _GLOBAL_OBJECTS[name]
        if name in _GLOBAL_FUNCS:
            return f"J.{name}"
        if self.lenient:
            return _py(name)
        raise Unsupported(f"undeclared identifier {name!r}")

    def key(self, m) -> str:
        return self.ex(m.property) if m.computed else repr(m.property.name)

    def args(self, nodes) -> str:
        for a in nodes:
            if a.type == "SpreadElement":
                raise Unsupported("spread")
        return "[" + ", ".join(self.ex(a) for a in nodes) + "]"

    def func(self, node, name=None) -> str:
        """Emit a nested def into self.pre and return its name."""
        nm = name or self.tmp("_f")
        lines = self.function_lines(node, _py(nm) if name else nm)
        self.pre.extend(lines)
        return _py(nm) if name else nm

    def ex(self, n) -> str:
        t = n.type
        if t == "Literal":
            if getattr(n, "regex", None):
                return f"J.regex({n.regex.pattern!r}, {n.regex.flags!r})"
            v = n.value
            if v is None:
                return "None"
            return repr(v) if not isinstance(v, float) or v != int(v) or abs(v) >= 1e15 else repr(int(v))
        if t == "Identifier":
            return self.ident(n.name)
        if t == "MemberExpression":
            return f"J.get({self.ex(n.object)}, {self.key(n)})"
        if t == "CallExpression":
            c = n.callee
            if c.type == "MemberExpression":
                if c.object.type == "Identifier" and c.object.name in _GLOBAL_OBJECTS and not c.computed and not (self.fn and self.fn.owner(c.object.name)):
                    return f"{_GLOBAL_OBJECTS[c.object.name]}.{c.property.name}({', '.join(self.ex(a) for a in n.arguments)})"
                if c.object.type == "Identifier" and c.object.name == "Object" and not c.computed and c.property.name in ("keys", "values"):
                    return f"J.object_{c.property.name}({self.ex(n.arguments[0])})"
                if c.object.type == "Identifier" and c.object.name == "Array" and not c.computed and c.property.name == "isArray":
                    return f"J.instanceof({self.ex(n.arguments[0])}, 'Array')"
                return f"J.call({self.ex(c.object)}, {self.key(c)}, {self.args(n.arguments)})"
            if c.type == "Identifier":
                for a in n.arguments:
                    if a.type == "SpreadElement":
                        raise Unsupported("spread")
                return f"{self.ident(c.name)}({', '.join(self.ex(a) for a in n.arguments)})"
            raise Unsupported("call of a computed callee")
        if t == "BinaryExpression":
            op = n.operator
            if op in _BINOPS:
                return f"{_BINOPS[op]}({self.ex(n.left)}, {self.ex(n.right)})"
            if op == "!=":
                return f"(not J.eq({self.ex(n.left)}, {self.ex(n.right)}))"
            if op == "!==":
                return f"(not J.seq({self.ex(n.left)}, {self.ex(n.right)}))"
            if op == "in":
                return f"J.has({self.ex(n.right)}, {self.ex(n.left)})"
            if op == "instanceof" and n.right.type == "Identifier":
                return f"J.instanceof({self.ex(n.left)}, {n.right.name!r})"
            raise Unsupported(f"operator {op}")
        if t == "LogicalExpression":
            x = self.tmp()
            a, b = self.ex(n.left), self.ex(n.right)
            return f"({b} if J.truthy({x} := {a}) else {x})" if n.operator == "&&" else f"({x} if J.truthy({x} := {a}) else {b})"
        if t == "UnaryExpression":
            op = n.operator
            if op == "!":
                return f"(not J.truthy({self.ex(n.argument)}))"
            if op == "-":
                return f"J.neg({self.ex(n.argument)})"
            if op == "+":
                return f"J.num({self.ex(n.argument)})"
            if op == "typeof":
                return f"J.typeof({self.ex(n.argument)})"
            if op == "delete" and n.argument.type == "MemberExpression":
                return f"J.delete({self.ex(n.argument.object)}, {self.key(n.argument)})"
            raise Unsupported(f"unary {op}")
        if t == "ConditionalExpression":
            return f"({self.ex(n.consequent)} if J.truthy({self.ex(n.test)}) else {self.ex(n.alternate)})"
        if t == "ArrayExpression":
            if any(e is None or e.type == "SpreadElement" for e in n.elements):
                raise Unsupported("sparse or spread array")
            return "[" + ", ".join(self.ex(e) for e in n.elements) + "]"
        if t == "ObjectExpression":
            items = []
            for p in n.properties:
                if p.type != "Property" or p.computed or p.kind != "init":
                    raise Unsupported("object property kind")
                k = p.key.name if p.key.type == "Identifier" else str(p.key.value)
                items.append(f"{k!r}: {self.ex(p.value)}")
            return "{" + ", ".join(items) + "}"
        if t == "TemplateLiteral":
            parts = []
            for i, q in enumerate(n.quasis):
                parts.append(repr(q.value.cooked))
                if i < len(n.expressions):
                    parts.append(f"J.tostr({self.ex(n.expressions[i])})")
            return "(" + " + ".join(parts) + ")"
        if t in ("FunctionExpression",):
            return self.func(n)
        if t == "ArrowFunctionExpression":
            if n.expression:
                ps = ", ".join(f"{_py(p.name)}=UNDEF" for p in n.params) + (", " if n.params else "") + "*_x"
                saved = self.fn
                self.fn = _Fn({p.name for p in n.params}, set(), saved)
                try:
                    body = self.ex(n.body)
                finally:
                    self.fn = saved
                return f"(lambda {ps}: {body})"
            return self.func(n)
        if t == "NewExpression" and n.callee.type == "Identifier" and n.callee.name in ("Error", "TypeError", "RegExp", "Array", "Object"):
            return f"J.{n.callee.name}({', '.join(self.ex(a) for a in n.arguments)})"
        raise Unsupported(t)

    # ---- statements ---------------------------------------------------------------------------------------------------------------------
    def block(self, node) -> list[str]:
        body = node.body if node.type == "BlockStatement" else [node]
        out: list[str] = []
        for s in body:
            out += self.stmt(s)
        return out or ["pass"]

    def indent(self, lines):
        return ["    " + ln for ln in lines]

    def assign_target(self, tgt, value: str) -> list[str]:
        if tgt.type == "Identifier":
            name = tgt.name
            owner = self.resolve_assign(name)
            if owner is not self.fn:
                self.fn.nonlocals.add(_py(name))
            return [f"{_py(name)} = {value}"]
        if tgt.type == "MemberExpression":
            return [f"J.put({self.ex(tgt.object)}, {self.key(tgt)}, {value})"]
        raise Unsupported("assignment target")

    def resolve_assign(self, name: str):
        """The function that owns `name`; an undeclared name is a sloppy-mode global, owned by the outermost function."""
        owner = self.fn.owner(name)
        if owner is None:
            self.implicit.add(name)
            self.root.declared.add(name)
            owner = self.root
        return owner

    def simple_expr_stmt(self, e) -> list[str]:
        t = e.type
        if t == "AssignmentExpression":
            op = e.operator
            if op == "=":
                return self.assign_target(e.left, self.ex(e.right))
            if op in ("+=", "-=", "*=", "/=", "%="):
                bop = {"+=": "J.add", "-=": "J.sub", "*=": "J.mul", "/=": "J.div", "%=": "J.mod"}[op]
                if e.left.type == "Identifier":
                    return self.assign_target(e.left, f"{bop}({self.ex(e.left)}, {self.ex(e.right)})")
                o, k = self.tmp("_o"), self.tmp("_k")
                return [f"{o} = {self.ex(e.left.object)}", f"{k} = {self.key(e.left)}", f"J.put({o}, {k}, {bop}(J.get({o}, {k}), {self.ex(e.right)}))"]
            raise Unsupported(f"assignment {op}")
        if t == "UpdateExpression":
            bop = "J.add" if e.operator == "++" else "J.sub"
            if e.argument.type == "Identifier":
                return self.assign_target(e.argument, f"{bop}({self.ex(e.argument)}, 1)")
            o, k = self.tmp("_o"), self.tmp("_k")
            return [f"{o} = {self.ex(e.argument.object)}", f"{k} = {self.key(e.argument)}", f"J.put({o}, {k}, {bop}(J.get({o}, {k}), 1))"]
        if t == "SequenceExpression":
            out = []
            for x in e.expressions:
                out += self.simple_expr_stmt(x)
            return out
        return [self.ex(e)]

    def stmt(self, s) -> list[str]:
        saved_pre, self.pre = self.pre, []
        try:
            lines = self._stmt(s)
            return self.pre + lines
        finally:
            self.pre = saved_pre

    def _stmt(self, s) -> list[str]:
        t = s.type
        if t == "VariableDeclaration":
            out = []
            for d in s.declarations:
                if d.init is None:
                    continue          # hoisted: already UNDEF
                out += self.assign_target(d.id, self.ex(d.init))
            return out
        if t == "ExpressionStatement":
            if s.expression.type == "Literal" and isinstance(s.expression.value, str):
                return []
            return self.simple_expr_stmt(s.expression)
        if t == "BlockStatement":
            out = []
            for x in s.body:
                out += self.stmt(x)
            return out
        if t == "EmptyStatement":
            return []
        if t == "IfStatement":
            cond = self.ex(s.test)
            out = [f"if J.truthy({cond}):"] + self.indent(self.block(s.consequent))
            alt = s.alternate
            if alt is not None:
                out += ["else:"] + self.indent(self.block(alt))
            return out
        if t == "ForStatement":
            init = []
            if s.init is not None:
                init = self._stmt(s.init) if s.init.type == "VariableDeclaration" else self.simple_expr_stmt(s.init)
            update = self.simple_expr_stmt(s.update) if s.update is not None else []
            test = f"J.truthy({self.ex(s.test)})" if s.test is not None else "True"
            self.loops.append(update)
            try:
                body = self.block(s.body)
            finally:
                self.loops.pop()
            return init + [f"while {test}:"] + self.indent(body + update)
        if t == "WhileStatement":
            self.loops.append([])
            try:
                body = self.block(s.body)
            finally:
                self.loops.pop()
            return [f"while J.truthy({self.ex(s.test)}):"] + self.indent(body)
        if t == "DoWhileStatement":
            self.loops.append(None)
            try:
                body = self.block(s.body)
            finally:
                self.loops.pop()
            return ["while True:"] + self.indent(body + [f"if not J.truthy({self.ex(s.test)}):", "    break"])
        if t in ("ForInStatement", "ForOfStatement"):
            if s.left.type == "VariableDeclaration":
                var = s.left.declarations[0].id
            else:
                var = s.left
            if var.type != "Identifier":
                raise Unsupported("for-in target")
            src = self.ex(s.right)
            it = f"J.keys_of({src})" if t == "ForInStatement" else f"list({src})"
            self.loops.append([])
            try:
                body = self.block(s.body)
            finally:
                self.loops.pop()
            tgt = _py(var.name)
            if self.resolve_assign(var.name) is not self.fn:
                self.fn.nonlocals.add(tgt)
            return [f"for {tgt} in {it}:"] + self.indent(body)
        if t == "BreakStatement":
            if s.label is not None:
                raise Unsupported("labelled break")
            return ["break"]
        if t == "ContinueStatement":
            if s.label is not None or not self.loops:
                raise Unsupported("continue")
            upd = self.loops[-1]
            if upd is None:
                raise Unsupported("continue in do-while")
            return upd + ["continue"]
        if t == "ReturnStatement":
            return [f"return {self.ex(s.argument)}" if s.argument is not None else "return UNDEF"]
        if t == "ThrowStatement":
            return [f"raise JSError({self.ex(s.argument)})"]
        if t == "TryStatement":
            out = ["try:"] + self.indent(self.block(s.block))
            if s.handler is not None:
                e = self.tmp("_e")
                prm = s.handler.param
                if prm is None or prm.type != "Identifier":
                    raise Unsupported("catch parameter")
                name = _py(prm.name)
                owner = self.resolve_assign(prm.name)
                if owner is not self.fn:
                    self.fn.nonlocals.add(name)
                out += [f"except JSError as {e}:", f"    {name} = J.errval({e})"] + self.indent(self.block(s.handler.body))
            if s.finalizer is not None:
                out += ["finally:"] + self.indent(self.block(s.finalizer))
            return out
        if t == "FunctionDeclaration":
            return self.function_lines(s, _py(s.id.name))
        if t == "SwitchStatement":
            return self.switch(s)
        raise Unsupported(t)

    def switch(self, s) -> list[str]:
        d = self.tmp()
        out = [f"{d} = {self.ex(s.discriminant)}"]
        groups, pending = [], []
        for c in s.cases:
            pending.append(c.test)
            if c.consequent:
                body = list(c.consequent)
                if body and body[-1].type == "BreakStatement":
                    body = body[:-1]
                elif c is not s.cases[-1]:
                    raise Unsupported("switch fallthrough")
                if any(x.type == "BreakStatement" for x in body):
                    raise Unsupported("break inside switch body")
                groups.append((pending, body))
                pending = []
        if pending:
            groups.append((pending, []))
        first = True
        default = None
        for tests, body in groups:
            if None in tests:
                default = body
                if len([x for x in tests if x is not None]):
                    raise Unsupported("default grouped with cases")
                continue
            cond = " or ".join(f"J.seq({d}, {self.ex(x)})" for x in tests)
            out += [("if " if first else "elif ") + cond + ":"] + self.indent(self.block_of(body))
            first = False
        if default is not None:
            out += (["else:"] + self.indent(self.block_of(default))) if not first else self.block_of(default)
        return out

    def block_of(self, stmts) -> list[str]:
        out: list[str] = []
        for x in stmts:
            out += self.stmt(x)
        return out or ["pass"]

    # ---- functions ----------------------------------------------------------------------------------------------------------------------
    def function_lines(self, node, py_name: str, top_params: list[str] | None = None) -> list[str]:
        params = [p.name for p in node.params] if top_params is None else top_params
        if any(p.type != "Identifier" for p in node.params) if top_params is None else False:
            raise Unsupported("destructured parameter")
        body = node.body.body if node.body.type == "BlockStatement" else [node.body]
        return self.body_lines(py_name, params, body, top_params is not None, node)

    def body_lines(self, py_name, params, body, top, node=None) -> list[str]:
        declared: set = set()
        _walk_vars(body, declared)
        saved_fn, saved_loops = self.fn, self.loops
        self.fn = _Fn(set(params), (declared | (self.implicit if saved_fn is None else set())) - set(params), saved_fn)
        if saved_fn is None:
            self.root = self.fn
        self.loops = []
        try:
            fdecls = [x for x in body if x.type == "FunctionDeclaration"]
            rest = [x for x in body if x.type != "FunctionDeclaration"]
            inner: list[str] = []
            for x in fdecls:
                inner += self.stmt(x)
            for x in rest:
                inner += self.stmt(x)
            init = [f"{_py(v)} = UNDEF" for v in sorted(self.fn.declared) if v not in {x.id.name for x in fdecls}]
            nl = [f"nonlocal {', '.join(sorted(self.fn.nonlocals))}"] if self.fn.nonlocals else []
            inner = nl + init + inner
            if node is not None and node.type == "ArrowFunctionExpression" and node.expression:
                inner = [f"return {self.ex(node.body)}"]
        finally:
            self.fn, self.loops = saved_fn, saved_loops
        sig = ", ".join(f"{_py(p)}" if top else f"{_py(p)}=UNDEF" for p in params) + ("" if top else (", " if params else "") + "*_x")
        return [f"def {py_name}({sig}):"] + self.indent(inner or ["pass"])


def transpile(script: str, init: str = "") -> str:
    """Python source for the script (and its init script). Raises Unsupported."""
    out = ["from ..jsrt import J, UNDEF, JSError", ""]
    for fname, src, params in (("init", init or "", ["state", "log", "sdc", "sdcFunctions"]),
                               ("run", script, ["records", "output", "error", "state", "log", "sdc", "sdcFunctions"])):
        try:
            tree = esprima.parseScript(src)
        except Exception as e:                    # noqa: BLE001 - esprima raises its own error types
            raise Unsupported(f"cannot parse the script: {e}")
        first = Transpiler(lenient=True)
        try:
            first.body_lines(fname, params, tree.body, True)
        except Unsupported:
            pass                                  # the real pass below reports it
        tr = Transpiler(implicit=first.implicit)
        out += [""] + tr.body_lines(fname, params, tree.body, True)
    code = "\n".join(out) + "\n"
    compile(code, "<script>", "exec")
    return code
