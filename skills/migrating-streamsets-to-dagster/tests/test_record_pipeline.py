"""Tests for the generic record-graph engine on its own -- every import here comes from
`record_pipeline`, never `runtime`, proving the engine has no StreamSets-specific coupling."""
import pytest

from streamsets_to_dagster.record_pipeline import (
    ErrorPolicy,
    Record,
    Run,
    Stage,
    UnimplementedStageError,
    del_path,
    each,
    get_path,
    set_path,
    truthy,
)


def collect(run, lane="out"):
    got = []
    run.add(Stage("sink", lambda recs: got.extend(recs) or {}, inputs=[lane]))
    return got


def test_a_single_origin_and_destination_moves_every_record():
    run = Run(None, "t", {})

    def gen():
        yield {"out": [Record({"n": 1}), Record({"n": 2})]}
    run.add(Stage("src", gen=gen, outputs=["out"]))
    got = collect(run)
    stats = run.execute()
    assert [r.value for r in got] == [{"n": 1}, {"n": 2}]
    assert stats["records_in"] == {"sink": 2}
    assert stats["records_out"] == {"src": 2}
    assert stats["error_records"] == {}


def test_a_processor_transforms_records_between_origin_and_destination():
    run = Run(None, "t", {})

    def gen():
        yield {"a": [Record({"n": 1}), Record({"n": 2})]}
    run.add(Stage("src", gen=gen, outputs=["a"]))

    def double(recs):
        for r in recs:
            r.value["n"] *= 2
        return {"b": recs}
    run.add(Stage("doubler", double, inputs=["a"], outputs=["b"]))
    got = collect(run, lane="b")
    run.execute()
    assert [r.value["n"] for r in got] == [2, 4]


def test_stream_selector_style_fan_out_by_lane():
    """A stage can route different records to different lanes -- the Stage/lane contract the
    engine relies on for fan-out (e.g. a Stream Selector), exercised directly here."""
    run = Run(None, "t", {})

    def gen():
        yield {"a": [Record({"n": 1}), Record({"n": 2}), Record({"n": 3})]}
    run.add(Stage("src", gen=gen, outputs=["a"]))

    def split(recs):
        return {
            "evens": [r for r in recs if r.value["n"] % 2 == 0],
            "odds": [r for r in recs if r.value["n"] % 2 == 1],
        }
    run.add(Stage("splitter", split, inputs=["a"], outputs=["evens", "odds"]))
    evens = collect(run, lane="evens")
    run.add(Stage("odd_sink", lambda recs: None, inputs=["odds"]))
    run.execute()
    assert [r.value["n"] for r in evens] == [2]


def test_topological_order_runs_a_stage_only_after_its_input_lane_is_produced():
    """Stages are added out of dependency order; the engine must still deliver b's output to c."""
    run = Run(None, "t", {})
    run.add(Stage("c", lambda recs: {"final": recs}, inputs=["b_out"], outputs=["final"]))
    run.add(Stage("b", lambda recs: {"b_out": recs}, inputs=["a"], outputs=["b_out"]))

    def gen():
        yield {"a": [Record({"x": 1})]}
    run.add(Stage("a_src", gen=gen, outputs=["a"]))
    got = collect(run, lane="final")
    run.execute()
    assert len(got) == 1


def _divide_stage(run, inst, *, on_error):
    """A stage built the way every real stage factory in runtime.py is: `each()` wraps a
    per-record function so a record-level exception routes through `Stage.on_error` instead of
    crashing the batch outright."""
    holder: dict = {}
    one = lambda r: Record({"r": 10 / r.value["n"]})  # noqa: E731 - ZeroDivisionError on n == 0
    st = Stage(inst, lambda recs: {"b": each(holder, run, one)(recs)}, inputs=["a"], outputs=["b"], on_error=on_error)
    holder["st"] = st
    return st


def test_on_error_continue_counts_the_error_and_keeps_going():
    run = Run(None, "t", {})

    def gen():
        yield {"a": [Record({"n": 1}), Record({"n": 0}), Record({"n": 2})]}
    run.add(Stage("src", gen=gen, outputs=["a"]))
    run.add(_divide_stage(run, "div", on_error=ErrorPolicy.CONTINUE))
    got = collect(run, lane="b")
    stats = run.execute()
    assert [r.value["r"] for r in got] == [10.0, 5.0]
    assert stats["error_records"]["div"] == 1


def test_on_error_stop_pipeline_raises_immediately_on_the_first_bad_record():
    run = Run(None, "t", {})

    def gen():
        yield {"a": [Record({"n": 0}), Record({"n": 2})]}
    run.add(Stage("src", gen=gen, outputs=["a"]))
    run.add(_divide_stage(run, "div", on_error=ErrorPolicy.STOP))
    collect(run, lane="b")
    with pytest.raises(RuntimeError, match="div"):
        run.execute()


def test_no_more_data_event_is_delivered_once_the_origin_is_exhausted():
    run = Run(None, "t", {})

    def gen():
        yield {"a": [Record({"n": 1})]}
    run.add(Stage("src", gen=gen, outputs=["a"], events=["done"]))
    events = collect(run, lane="done")
    run.add(Stage("data_sink", lambda recs: None, inputs=["a"]))
    run.execute()
    assert len(events) == 1
    assert events[0].event == "no-more-data"


def test_stub_fails_loudly_before_any_stage_reads_or_writes():
    run = Run(None, "t", {})
    reads = []

    def gen():
        reads.append(True)
        yield {"a": [Record({"n": 1})]}
    run.add(Stage("src", gen=gen, outputs=["a"]))
    run.stub("unsupported_stage", "GroovyEvaluator is not translated", inputs=["a"])
    with pytest.raises(UnimplementedStageError, match="GroovyEvaluator"):
        run.execute()
    assert reads == []  # the origin must never run once any stub is registered


def test_unimplemented_error_and_prefix_are_overridable_by_a_subclass():
    """This is exactly the mechanism streamsets_to_dagster.runtime.Run relies on to raise
    SdcCaveat with an "SDC-CAVEAT:" prefix instead of the generic error/message."""
    class DomainError(UnimplementedStageError):
        pass

    class DomainRun(Run):
        def __init__(self, *a, **kw):
            super().__init__(*a, unimplemented_error=DomainError, unimplemented_prefix="DOMAIN-GAP: ", **kw)

    run = DomainRun(None, "t", {})
    run.stub("x", "not supported")
    with pytest.raises(DomainError, match=r"^DOMAIN-GAP: x: not supported$"):
        run.execute()


def test_offsets_in_and_out_start_as_independent_copies():
    run = Run(None, "t", {}, offsets={"src": "42"})
    assert run.offsets_in == {"src": "42"}
    assert run.offsets_out == {"src": "42"}
    run.offsets_out["src"] = "43"
    assert run.offsets_in == {"src": "42"}  # mutating offsets_out must not affect offsets_in


def test_dry_flag_is_a_plain_passthrough_for_stage_factories_to_check():
    assert Run(None, "t", {}).dry is False
    assert Run(None, "t", {}, dry=True).dry is True


def test_record_copy_deep_copies_value_independently_of_the_original():
    r = Record({"a": {"b": 1}}, {"attr": "x"}, "an-event")
    c = r.copy()
    c.value["a"]["b"] = 2
    assert r.value["a"]["b"] == 1
    assert c.attrs == {"attr": "x"} and c.attrs is not r.attrs
    assert c.event == "an-event"
    assert r.attributes is r.attrs  # the `attributes` alias StreamSets scripts use


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/a/b", ["a", "b"]),
        ("/a[0]/b", ["a", 0, "b"]),
        ("/", []),
        ("/a[0][1]", ["a", 0, 1]),
    ],
)
def test_field_paths_parse_dict_and_list_indexing(path, expected):
    from streamsets_to_dagster.record_pipeline import _parts
    assert _parts(path) == expected


def test_get_set_del_path_round_trip_through_nested_structures():
    value = {}
    set_path(value, "/a/b", 1)
    assert value == {"a": {"b": 1}}
    assert get_path(value, "/a/b") == 1
    assert get_path(value, "/missing", "default") == "default"
    assert del_path(value, "/a/b") is True
    assert value == {"a": {}}
    assert del_path(value, "/a/b") is False  # already gone


def test_set_path_at_root_replaces_the_whole_value_when_given_a_dict():
    value = {"old": 1}
    set_path(value, "/", {"new": 2})
    assert value == {"new": 2}


def test_truthy_matches_python_bool_and_the_case_insensitive_string_convention():
    assert truthy(1) is True
    assert truthy(0) is False
    assert truthy("true") is True
    assert truthy("True") is True
    assert truthy("false") is False
    assert truthy("anything else") is False


def test_error_policy_constants_are_the_strings_stage_factories_compare_against():
    assert ErrorPolicy.CONTINUE == "TO_ERROR"
    assert ErrorPolicy.STOP == "STOP_PIPELINE"
