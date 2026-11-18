"""Scenarios shared by both sides. `hook` is Airflow's S3Hook or the shim; `calls` records every boto call."""
import io, types, botocore.exceptions as be
CALLS = []


def canon(x):
    """Drop explicit defaults (None, empty string, empty dict) so a call that sets them compares equal to one that leaves them out:
    S3 treats both the same."""
    if isinstance(x, dict):
        out = {k: canon(v) for k, v in x.items()}
        return {k: v for k, v in out.items() if v not in (None, "", {}) or v == 0}
    return x
class FakeClient:
    exceptions = types.SimpleNamespace(ClientError=be.ClientError)
    existing = {"b/exists.txt"}
    def head_object(self, **kw):
        CALLS.append(("head_object", kw))
        if f"{kw['Bucket']}/{kw['Key']}" not in self.existing:
            raise be.ClientError({"Error": {"Code": "404", "Message": "nf"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {}
    def upload_file(self, *a, **kw): CALLS.append(("upload_file", list(a), canon({k: v for k, v in kw.items() if k != "Config"})))
    def upload_fileobj(self, fileobj, *a, **kw): CALLS.append(("upload_fileobj", fileobj.read(), list(a), {k: v for k, v in kw.items() if k != "Config"}, "Config" in kw))
    def get_object(self, **kw): CALLS.append(("get_object", kw)); return {"Body": io.BytesIO(b"hello")}
    def delete_objects(self, **kw):
        CALLS.append(("delete_objects", len(kw["Delete"]["Objects"]) if len(kw["Delete"]["Objects"]) > 3 else canon(kw)))
        bad = [o["Key"] for o in kw["Delete"]["Objects"] if o["Key"] == "bad"]
        if bad:
            return {"Errors": [{"Key": k, "Code": "AccessDenied", "Message": "no"} for k in bad]}
        return {"Deleted": [{"Key": o["Key"]} for o in kw["Delete"]["Objects"]]}
    def copy_object(self, **kw): CALLS.append(("copy_object", canon(kw))); return {}
    def get_paginator(self, name):
        outer = self
        class P:
            def paginate(self, **kw): CALLS.append(("paginate", name, canon(kw))); return [{"Contents": [{"Key": "p/a", "LastModified": __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)}, {"Key": "p/b", "LastModified": __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)}]}]
        return P()

def run(hook):
    out = {}
    def step(name, fn):
        CALLS.clear()
        try: r = fn(); out[name] = {"result": r if not isinstance(r, bytes) else r.decode(), "calls": [list(map(str, c)) for c in CALLS]}
        except Exception as e: out[name] = {"error": f"{type(e).__name__}: {e}", "calls": [list(map(str, c)) for c in CALLS]}
    step("load_file replace=True", lambda: hook.load_file("f.txt", "k/f.txt", bucket_name="b", replace=True))
    step("load_file replace=False key absent", lambda: hook.load_file("f.txt", "k/f.txt", bucket_name="b", replace=False))
    step("load_file replace=False key exists", lambda: hook.load_file("f.txt", "exists.txt", bucket_name="b", replace=False))
    step("load_string", lambda: hook.load_string("hi there", "k/s.txt", bucket_name="b", replace=True))
    step("load_bytes", lambda: hook.load_bytes(b"\x00\x01", "k/b.bin", bucket_name="b", replace=True))
    step("check_for_key present", lambda: hook.check_for_key("exists.txt", bucket_name="b"))
    step("check_for_key absent", lambda: hook.check_for_key("nope.txt", bucket_name="b"))
    step("list_keys", lambda: hook.list_keys("b", prefix="p/"))
    step("delete_objects", lambda: hook.delete_objects("b", ["a", "b"]))
    step("copy_object", lambda: hook.copy_object("src.txt", "dst.txt", "b", "b"))
    step("url bucket parse (load_file)", lambda: hook.load_file("f.txt", "s3://b/u/f.txt", replace=True))
    return out
