"""Trace collection (test-module import) once per test file: python trace_collect.py <tests.json> <out_dir>"""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
ROOT = "/opt/sqlglot"
tests = json.load(open(sys.argv[1]))["tests"]
out_dir = sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
files = sorted({t["file"] for t in tests})
def run(f):
    out = os.path.join(out_dir, f.replace("/", "__") + ".json")
    env = dict(os.environ, BR_OUT=out, PYTHONPATH=f"{ROOT}:/app/tools:/app", SKIP_INTEGRATION="0",
               PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "br_trace", "-p", "no:cacheprovider", "-q",
                        "--collect-only", f], cwd=ROOT, env=env, capture_output=True, text=True)
    return f, p.returncode
with ThreadPoolExecutor(6) as pool:
    for f, rc in pool.map(run, files):
        if rc:
            print(f, rc)
print("done", len(files))
