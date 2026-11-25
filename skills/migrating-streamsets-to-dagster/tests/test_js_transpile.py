"""JavaScript to Python: each snippet runs in QuickJS and as transpiled Python, and the results must match."""
import os
from pathlib import Path

import pytest

from js_diff import run_both
from streamsets_to_dagster.jstopy import Unsupported, transpile

REC = [({"a": "x", "n": 3, "f": 2.5, "z": 0, "e": "", "nul": None, "l": [1, 2, 3], "o": {"k": "v", "d": {"deep": 1}}, "s": " Hello World "}, {"h": "1"})]

SNIPPETS = {
    "copy": "for (var i=0;i<records.length;i++){ output.write(records[i]); }",
    "arith_and_concat": "var r=records[0]; r.value.t = r.value.n + 1; r.value.u = r.value.a + r.value.n; r.value.v = r.value.n + '1'; r.value.w = '3' * '4'; r.value.m = 7 % 3; r.value.d = 7 / 2; output.write(r);",
    "truthiness": "var r=records[0]; var v=r.value; r.value.t = [!!v.a, !!v.z, !!v.e, !!v.nul, !!v.l, !!v.missing, !!v.o, !!v.f]; output.write(r);",
    "loose_vs_strict": "var v=records[0].value; records[0].value.t = [v.z == '', v.z === '', v.nul == undefined, v.nul === undefined, v.missing == null, v.missing === null, v.n == '3', v.n === '3', null == 0, '1' == 1]; output.write(records[0]);",
    "typeof": "var v=records[0].value; records[0].value.t=[typeof v.a, typeof v.n, typeof v.l, typeof v.nul, typeof v.missing, typeof v.o, typeof output]; output.write(records[0]);",
    "string_methods": "var s=records[0].value.s; var r=records[0]; r.value.t=[s.trim(), s.toLowerCase(), s.toUpperCase(), s.indexOf('o'), s.substring(2,5), s.slice(-5), s.split(' '), s.replace('l','L'), s.replace(/l/g,'L'), s.charAt(1), s.length, s.trim().split(/\\s+/).join('_')]; output.write(r);",
    "array_methods": "var a=[3,1,2]; var r=records[0]; a.push(9); r.value.t=[a.length, a.join('-'), a.indexOf(2), a.slice(1,3), a.concat([7]), a.map(function(x){return x*2}), a.filter(function(x){return x>1}), a.sort(), a.reverse()]; output.write(r);",
    "object_iteration": "var o=records[0].value.o; var keys=[]; for (var k in o) { keys.push(k); } records[0].value.t = keys; output.write(records[0]);",
    "ternary_logic": "var v=records[0].value; records[0].value.t=[v.a ? 'y':'n', v.z ? 'y':'n', v.a && v.n, v.z && v.n, v.z || 'dflt', v.a || 'dflt', v.missing || 0, (v.nul || v.e) || 'last']; output.write(records[0]);",
    "undefined_property_read_is_undefined": "var v=records[0].value; if (v.missing === undefined) { v.flag = 'undef'; } if (v.o.nope === undefined) { v.flag2 = 1; } output.write(records[0]);",
    "typeerror_counts_as_error": "for (var i=0;i<records.length;i++){ try { var x = records[i].value.missing.deep; output.write(records[i]); } catch (e) { error.write(records[i], e); } }",
    "throw_and_catch": "try { throw new Error('boom'); } catch (e) { records[0].value.msg = e.message; records[0].value.str = String(e); } output.write(records[0]);",
    "finally_runs": "var log2=[]; try { log2.push('a'); } finally { log2.push('b'); } records[0].value.t=log2; output.write(records[0]);",
    "while_break_continue": "var i=0, hits=[]; while (true) { i++; if (i > 6) break; if (i % 2 == 0) continue; hits.push(i); } records[0].value.t=hits; output.write(records[0]);",
    "for_continue_runs_update": "var out=[]; for (var i=0;i<6;i++){ if (i==2||i==4) { continue; } out.push(i); } records[0].value.t=out; output.write(records[0]);",
    "do_while": "var i=0, c=0; do { c += i; i++; } while (i < 4); records[0].value.t=c; output.write(records[0]);",
    "switch": "var k=records[0].value.a; var r; switch (k) { case 'x': r='is x'; break; case 'y': case 'z': r='y or z'; break; default: r='other'; } records[0].value.t=r; output.write(records[0]);",
    "closures_and_nested_functions": "function make(n){ var c=0; function inc(){ c = c + n; return c; } return inc; } var f=make(2); f(); records[0].value.t=f(); output.write(records[0]);",
    "recursion": "function fact(n){ return n <= 1 ? 1 : n * fact(n-1); } records[0].value.t=fact(10); output.write(records[0]);",
    "math_and_parse": "var r=records[0]; r.value.t=[Math.floor(2.7), Math.ceil(2.1), Math.round(2.5), Math.max(1,5,3), Math.min(4,2), Math.abs(-3), Math.pow(2,10), parseInt('42px'), parseFloat('3.5e2x'), parseInt('0x1f'), isNaN('abc'), Number('12'), String(12.0)]; output.write(r);",
    "json_roundtrip": "var r=records[0]; r.value.t=JSON.parse(JSON.stringify({a:[1,2,{b:null}], c:'x'})); r.value.u = JSON.stringify(r.value.o); output.write(r);",
    "new_record_and_attributes": "var n = sdcFunctions.createRecord('x:' + 1); n.value = {a: records[0].value.a, h: records[0].attributes.h}; n.attributes['k'] = 'v'; output.write(n);",
    "state_persists_across_batches": "state.count = (state.count || 0) + records.length; records[0].value.seen = state.count; output.write(records[0]);",
    "events": "var e = sdc.createEvent('done', 1); e.value = {n: records.length}; sdc.toEvent(e); output.write(records[0]);",
    "delete_and_in": "var v=records[0].value; delete v.a; v.has_n = ('n' in v); v.has_a = ('a' in v); output.write(records[0]);",
    "instanceof_and_isarray": "var v=records[0].value; v.t=[v.a instanceof String, Array.isArray(v.l), Array.isArray(v.o), Object.keys(v.o)]; output.write(records[0]);",
    "regex_test_and_match": "var s=records[0].value.s; records[0].value.t=[/world/i.test(s), s.match(/o/g), s.match(/(W)(o)/)]; output.write(records[0]);",
    "template_and_compound_assign": "var v=records[0].value; v.n += 4; v.a += 'y'; v.l.push(v.n); v.t = `n=${v.n}, a=${v.a}`; output.write(records[0]);",
    "member_compound_assign": "var o=records[0].value.o; o.count = 1; o.count += 2; o['d'].deep *= 5; output.write(records[0]);",
    "error_write_with_message": "error.write(records[0], 'bad'); ",
    "null_property_read_throws": "var v=records[0].value; try { v.nul.x; v.t='no'; } catch (e) { v.t='caught'; } output.write(records[0]);",
    "undeclared_assignment_inside_function": "function f(x){ tmp = x * 2; return tmp + 1; } records[0].value.t = f(5); output.write(records[0]);",
}


@pytest.mark.parametrize("name", sorted(SNIPPETS))
def test_snippet_matches_quickjs(name):
    batches = [REC, [({"a": "y", "n": 1}, {})]]
    js, py = run_both(SNIPPETS[name], batches, init="")
    assert py == js, f"{name}\nQuickJS : {js}\nPython  : {py}"


def test_init_script_state_matches():
    js, py = run_both("records[0].value.k = state.k; output.write(records[0]);", [REC], init="state.k = ['a','b'].join('+');")
    assert py == js and py[0][0][0]["k"] == "a+b"


@pytest.mark.parametrize("bad", ["this.x = 1;", "var o = {get a(){return 1}};", "label: for(;;){break label;}", "var [a,b] = [1,2];", "(function(){})();", "var x = new Date();", "var y = 1; var z = y++ + 1;"])
def test_unsupported_constructs_are_refused_not_guessed(bad):
    with pytest.raises(Unsupported):
        transpile(bad)


CORPUS = os.environ.get("SDC_JS_CORPUS")


@pytest.mark.skipif(not CORPUS, reason="set SDC_JS_CORPUS to a folder of pipeline JSON exports")
def test_every_corpus_script_that_transpiles_matches_quickjs():
    import random
    import re
    from streamsets_to_dagster.loader import load
    rnd = random.Random(7)
    done = diff = 0
    for title, p in load(Path(CORPUS)).pipelines.items():
        for s in p.stages:
            if s.cls != "JavaScriptDProcessor":
                continue
            script, init = str(s.conf.get("script") or ""), str(s.conf.get("initScript") or "")
            try:
                transpile(script, init)
            except Unsupported:
                continue
            keys = sorted(set(re.findall(r"['\"]([A-Za-z_][\w./-]*)['\"]", script + init)))
            vals = ["", " x ", "abc", "12.5", 7, 0, None, True, [1, 2], {"a": 1}, "2020-01-01"]
            batches = [[({k: rnd.choice(vals) for k in keys[:12]} | {"timestamp": 5}, {}) for _ in range(3)] for _ in range(6)]
            try:
                js, py = run_both(script, batches, init)
            except Exception as e:                  # noqa: BLE001 - a script that throws on random input in only one engine is itself a difference
                diff += 1
                print("DIFF (raised)", title, s.instance, type(e).__name__, str(e)[:200])
                continue
            done += 1
            if js != py:
                diff += 1
                print("DIFF", title, s.instance)
    print(f"{done} scripts compared, {diff} differences")
    assert diff == 0
