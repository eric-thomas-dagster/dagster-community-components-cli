"""Pull (expression, documented result) pairs out of Microsoft's ADF expression-function reference (tests/data/adf-expression-functions.md).

A pair is a fenced code block holding one expression, followed by "And returns this result: `x`" (or "returns true/false" in the paragraph above it).
Prints them as JSON; tests/test_doc_conformance.py runs them through the translator.
"""
import json
import re
import sys
from pathlib import Path

text = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/data/adf-expression-functions.md").read_text()
pairs = []
for m in re.finditer(r"```\n(?P<expr>[^`\n]+)\n```\s*\n\s*(?:And returns this result:|Returns this result:|And returns:)\s*`(?P<res>[^`]+)`", text):
    pairs.append({"expr": m.group("expr").strip(), "result": m.group("res").strip()})
for m in re.finditer(r"(?P<para>[^\n]*(?:returns|return) (?P<tf>true|false)[^\n]*:\s*)\n\n```\n(?P<expr>[^`\n]+)\n```", text):
    pairs.append({"expr": m.group("expr").strip(), "result": m.group("tf")})
for m in re.finditer(r"returns (?:true|false)[^\n]*\n\n```\n(?P<expr>[^`\n]+)\n```\n", text):
    pass
seen, out = set(), []
for p in pairs:
    if p["expr"] not in seen:
        seen.add(p["expr"]); out.append(p)
print(json.dumps(out, indent=1))
