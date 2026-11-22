"""Trace every test in its own fresh process (inside the env image).

    python /app/tools/trace_all.py <tests.json> <out_dir> [jobs] [only_numbers...]
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = "/opt/sqlglot"


def run(test, out_dir):
    out = os.path.join(out_dir, f"{test['number']}.json")
    if os.path.exists(out):
        return test["number"], "cached", 0.0
    env = dict(os.environ)
    env.update(
        BR_OUT=out,
        PYTHONPATH=f"{ROOT}:/app/tools:/app",
        SKIP_INTEGRATION="0",
        PYTHONDONTWRITEBYTECODE="1",
        OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
    )
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "br_trace", "-p", "no:cacheprovider",
             "-q", "-x", "--no-header", "-rN", test["id"]],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
        )
        status = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
        if proc.returncode != 0:
            with open(out + ".log", "w") as f:
                f.write(proc.stdout[-5000:] + "\n---\n" + proc.stderr[-5000:])
    except subprocess.TimeoutExpired:
        status = "timeout"
    return test["number"], status, time.time() - t0


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out_dir = sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    only = {int(x) for x in sys.argv[4:]}
    if only:
        tests = [t for t in tests if t["number"] in only]
    os.makedirs(out_dir, exist_ok=True)
    # longest first so the tail is short
    tests.sort(key=lambda t: -t["seconds"])
    done = 0
    with ThreadPoolExecutor(jobs) as pool:
        for number, status, wall in pool.map(lambda t: run(t, out_dir), tests):
            done += 1
            if status != "ok" and status != "cached":
                print(f"{number} {status} {wall:.1f}s", flush=True)
            if done % 100 == 0:
                print(f"{done}/{len(tests)}", flush=True)


if __name__ == "__main__":
    main()
