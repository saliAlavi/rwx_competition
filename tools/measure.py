"""Run every test through br.runner on the pristine tree; record pass/fail and CPU.

    python /app/tools/measure.py /data/tests.json /out/measure.json [slots]
"""
import json
import sys
import time

sys.path.insert(0, "/app")
from br import runner  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
slots = int(sys.argv[3]) if len(sys.argv) > 3 else 2
runner.preimport()
t0 = time.time()
by_file = {}
for t in tests:
    path, cls, meth = t["id"].split("::")
    by_file.setdefault(t["file"], []).append((t["number"], t["file"], cls, meth))
jobs = []
for f, items in by_file.items():
    to = {num: 120.0 for num, *_ in items}
    jobs.append(runner.Job(key=f, files={}, tests=items, timeouts=to))
ex = runner.Executor(slots=slots)
ex.run(jobs)
res = {"cpu": {}, "failed": [], "notes": {}, "wall": time.time() - t0,
       "child_cpu": runner.child_cpu_seconds(), "self_cpu": runner.self_cpu_seconds()}
for j in jobs:
    res["cpu"].update({str(k): v for k, v in j.cpu.items()})
    res["failed"] += sorted(j.failed)
    if j.note or j.failed_files or j.failed_classes:
        res["notes"][j.key] = [j.note, sorted(j.failed_files), sorted(j.failed_classes)]
json.dump(res, open(sys.argv[2], "w"))
print("wall", res["wall"], "child_cpu", res["child_cpu"], "self", res["self_cpu"], "failed", res["failed"], res["notes"], file=sys.stderr)
