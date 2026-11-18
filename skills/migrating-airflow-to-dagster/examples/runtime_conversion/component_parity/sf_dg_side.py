"""The COPY INTO statement the community component builds for the equivalent configuration."""
import importlib.util, json, pathlib, re, warnings
warnings.filterwarnings("ignore")
import dagster_community_components as dcc
path = next((pathlib.Path(dcc.__file__).parent / "assets").glob("*/snowflake_copy_into_asset/component.py"))
spec = importlib.util.spec_from_file_location("sf_copy", path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
af = json.load(open("sf_af_out.json"))


def to_component(a):
    """How a converter would map the operator's arguments: a raw file_format string becomes a format name or an options dict."""
    kw = dict(asset_key="t", resource_key="snowflake_default", database="DB", schema=a["schema"], table=a["table"], stage=a["stage"], stage_path=a.get("prefix"),
              files=a.get("files"), pattern=a.get("pattern"), validation_mode=(a.get("validation_mode") or "").split("=")[-1].strip() or None)
    fmt = a["file_format"]
    named = re.fullmatch(r"\(\s*format_name\s*=\s*'([^']+)'\s*\)", fmt, re.I)
    if named:
        kw["file_format_name"] = named.group(1)
    else:
        kw["file_format_options"] = {k.upper(): v for k, v in re.findall(r"(\w+)\s*=\s*'([^']*)'", fmt)}
    co = a.get("copy_options")
    if co:
        mm = re.fullmatch(r"ON_ERROR\s*=\s*(\w+)", co)
        kw["on_error"] = mm.group(1)
    return {k: v for k, v in kw.items() if v is not None}


out = {}
for name, rec in af.items():
    c = m.SnowflakeCopyIntoAssetComponent(**to_component(rec["args"]))
    out[name] = {"sql": c._build_copy_sql()}
json.dump(out, open("sf_dg_out.json", "w"), indent=1); print("component copy statements:", len(out))
