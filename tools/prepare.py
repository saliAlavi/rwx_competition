"""Build-time step: turn the committed maps into a fast-loading pickle."""
import sys
sys.path.insert(0, "/app")
try:
    from br import index
except ImportError:
    index = None
if index is not None:
    index.prepare("/app/maps", "/app/maps/index.pkl")

# pristine import-time state of sqlglot, for diffing patched imports at run time
import os
import subprocess

env = dict(os.environ, PYTHONPATH="/opt/sqlglot:/app")
subprocess.run([sys.executable, "-m", "br.state", "/app/maps/state.pkl"], env=env, check=True, cwd="/app")

# pristine symbolic fingerprints of the TPC-DS executor test's queries
subprocess.run([sys.executable, "-m", "br.tpcds", "/app/maps/tpcds_fp.pkl"], env=env, check=True, cwd="/app")

# identifiers read by the statements test_execute_tpcds runs outside its symbolic part
import ast
import gzip
import json
import pickle

ns = json.load(gzip.open("/app/maps/tpcds_ns.json.gz", "rt"))["ns"]
idents = set()
for rel, lines in ns.items():
    lines = set(lines)
    try:
        tree = ast.parse(open(os.path.join("/opt/sqlglot", rel), encoding="utf-8").read())
    except (OSError, SyntaxError):
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if not any(ln in lines for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                idents.add(sub.attr)
            elif isinstance(sub, ast.Name):
                idents.add(sub.id)
fp = pickle.load(open("/app/maps/tpcds_fp.pkl", "rb"))
fp["ns_idents"] = sorted(idents)
pickle.dump(fp, open("/app/maps/tpcds_fp.pkl", "wb"))
print(f"tpcds: {len(idents)} identifiers read outside the symbolic part", file=sys.stderr)
