"""Run the community components against the same recording fake client; write what they called and returned."""
import importlib.util, json, pathlib, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import dagster as dg
import fake_s3 as S

af = json.load(open("af_out.json"))
local = af["_local_file"]
import dagster_community_components as dcc
root = pathlib.Path(dcc.__file__).parent / "assets"


def load(name):
    path = next(root.glob(f"*/{name}/component.py"))
    spec = importlib.util.spec_from_file_location(name + "_component", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class FakeResource(dg.ConfigurableResource):
    def get_client(self):
        return S.FakeClient()


out = {}


def step(name, component):
    S.CALLS.clear()
    d = component.build_defs(context=None).assets[0]
    try:
        r = dg.materialize([d], resources={"io_manager": dg.InMemoryIOManager(), "aws": FakeResource()}, raise_on_error=False)
        value = None
        if r.success:
            try: value = r.asset_value(d.key)
            except Exception: value = None
            if value is None or "NoValueSentinel" in str(value):          # these components report through metadata, not a value
                md = r.get_asset_materialization_events()[0].materialization.metadata
                value = md["keys"].value if "keys" in md else None
        out[name] = {"result": value, "success": r.success, "calls": [list(map(str, c)) for c in S.CALLS]}
    except Exception as e:
        out[name] = {"error": type(e).__name__, "calls": [list(map(str, c)) for c in S.CALLS]}


copy = load("s3_copy_object_asset"); dele = load("s3_delete_objects_asset"); lst = load("s3_list_keys_asset"); up = load("s3_upload_file_asset")
step("copy by url", copy.S3CopyObjectAssetComponent(asset_name="x", source_s3_url="s3://b/src.txt", dest_s3_url="s3://b/dst.txt", resource_key="aws"))
step("copy by bucket+key", copy.S3CopyObjectAssetComponent(asset_name="x", source_bucket="b", source_key="src.txt", dest_bucket="b2", dest_key="dst.txt", resource_key="aws"))
step("delete keys", dele.S3DeleteObjectsAssetComponent(asset_name="x", bucket="b", keys=["a", "b"], resource_key="aws"))
step("delete reports an error", dele.S3DeleteObjectsAssetComponent(asset_name="x", bucket="b", keys=["a", "bad"], resource_key="aws"))
step("delete 1500 keys", dele.S3DeleteObjectsAssetComponent(asset_name="x", bucket="b", keys=[f"k{i}" for i in range(1500)], resource_key="aws"))
step("list prefix", lst.S3ListKeysAssetComponent(asset_name="x", bucket="b", prefix="p/", resource_key="aws"))
step("upload replace", up.S3UploadFileAssetComponent(asset_name="x", local_path=local, bucket="b", key="k/f.txt", replace=True, resource_key="aws"))
step("upload no replace, key exists", up.S3UploadFileAssetComponent(asset_name="x", local_path=local, bucket="b", key="exists.txt", replace=False, resource_key="aws"))
step("upload no replace, key absent", up.S3UploadFileAssetComponent(asset_name="x", local_path=local, bucket="b", key="k/new.txt", replace=False, resource_key="aws"))
json.dump(out, open("dg_out.json", "w"), indent=1, default=str); print("component scenarios:", len(out))
