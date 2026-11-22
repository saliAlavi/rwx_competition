"""Ground truth for generated patches: run sqlglot's full suite with pytest, like the labels.

    python tools/ground_truth.py <data_dir with commits/ and tests.json> <out_dir> <concurrency> [cpus_each]
Writes <out_dir>/<cid>.json and finally <out_dir>/answers.json in the sample format.
"""
import json, os, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
from br import patch as P

SRC = "/opt/sqlglot"

def run(cid, data, out, cpus, slot):
    res_path = os.path.join(out, f"{cid}.json")
    if os.path.exists(res_path):
        return cid, "cached"
    work = f"/tmp/gt/{cid}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(SRC, work, symlinks=True)
    text = open(os.path.join(data, "commits", f"{cid}.patch"), encoding="utf-8", errors="surrogateescape").read()
    for fp in P.parse(text):
        old = None
        if fp.old_path and os.path.exists(os.path.join(work, fp.old_path)):
            old = open(os.path.join(work, fp.old_path), encoding="utf-8", errors="surrogateescape").read()
        new = P.apply(old, fp)
        if fp.old_path and fp.old_path != fp.new_path and os.path.exists(os.path.join(work, fp.old_path)):
            os.remove(os.path.join(work, fp.old_path))
        if new is not None:
            os.makedirs(os.path.dirname(os.path.join(work, fp.new_path)), exist_ok=True)
            open(os.path.join(work, fp.new_path), "w", encoding="utf-8", errors="surrogateescape").write(new)
    env = dict(os.environ, PYTHONPATH=f"{work}:/app/tools", SKIP_INTEGRATION="0", BR_GT_OUT=res_path + ".tmp",
               PYTHONDONTWRITEBYTECODE="1")
    allowed = sorted(os.sched_getaffinity(0))
    cpuset = set(allowed[(slot * cpus) % len(allowed):][:cpus]) or set(allowed)
    t0 = time.time()
    try:
        subprocess.run([sys.executable, "-m", "pytest", "-p", "gt_plugin", "-p", "no:cacheprovider", "-q",
                        "-n", str(cpus), "--timeout=120", "tests"], cwd=work, env=env, capture_output=True,
                       timeout=1500, preexec_fn=lambda: os.sched_setaffinity(0, cpuset))
    except subprocess.TimeoutExpired:
        pass
    shutil.rmtree(work, ignore_errors=True)
    if os.path.exists(res_path + ".tmp"):
        os.rename(res_path + ".tmp", res_path)
        return cid, f"ok {time.time()-t0:.0f}s"
    return cid, "no result"

def main():
    data, out, conc = sys.argv[1], sys.argv[2], int(sys.argv[3])
    cpus = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    os.makedirs(out, exist_ok=True)
    cids = sorted(f[:-6] for f in os.listdir(os.path.join(data, "commits")) if f.endswith(".patch"))
    with ThreadPoolExecutor(conc) as pool:
        futs = [pool.submit(run, cid, data, out, cpus, i % conc) for i, cid in enumerate(cids)]
        for f in futs:
            print(*f.result(), flush=True)
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    kinds = json.load(open(os.path.join(data, "kinds.json"))) if os.path.exists(os.path.join(data, "kinds.json")) else {}
    answers = {}
    for cid in cids:
        p = os.path.join(out, f"{cid}.json")
        if not os.path.exists(p):
            continue
        r = json.load(open(p))
        failing = {num[n] for n in r["failed"] if n in num}
        for b in r["broken"]:
            failing.update(by_file.get(b, []))
        unknown = [n for n in r["failed"] if n not in num]
        answers[cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?"), "unknown": unknown[:5]}
    json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"))

if __name__ == "__main__":
    main()
