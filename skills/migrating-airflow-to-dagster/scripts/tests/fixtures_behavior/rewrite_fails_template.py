"""Rewrite of `fixture_fails` (a fixture for the harness's own tests).

ok: replaced by asset `fixture_fails/ok`.
boom: replaced by asset `fixture_fails/boom`.
after: replaced by asset `fixture_fails/after`.
side: replaced by asset `fixture_fails/side`.
"""
import dagster as dg


@dg.asset(key=["fixture_fails", "ok"])
def ok():
    return "fine"


@dg.asset(key=["fixture_fails", "boom"], deps=[ok])
def boom():
    if @FAIL@:
        raise ValueError("by design")


@dg.asset(key=["fixture_fails", "after"], deps=[boom])
def after():
    with open("after.txt", "w") as f:
        f.write("should not exist")


@dg.asset(key=["fixture_fails", "side"])
def side():
    with open("side.txt", "w") as f:
        f.write("independent")


ASSETS = [ok, boom, after, side]
fixture_fails = dg.define_asset_job("fixture_fails", selection=ASSETS)
