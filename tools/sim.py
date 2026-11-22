"""Offline: realized cost of the decision policy on a labeled set (assumes the runner is exact).
    python tools/sim.py <data_dir> <answers.json> <checkout> [--gamble] [--verbose]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import patch as P, index, analyze
from br import select as S

data, answers, checkout = sys.argv[1:4]
gamble = "--gamble" in sys.argv
verbose = "--verbose" in sys.argv
idx = index.load("/nonexistent", "maps/maps.json.gz")
tests = json.load(open(f"{data}/tests.json"))["tests"]
pos = {tid: i for i, tid in enumerate(idx.tests)}
bit_num = {pos[t["id"]]: t["number"] for t in tests if t["id"] in pos}
dur = {t["number"]: S.ms(t["seconds"]) for t in tests}
meta = {t["number"]: (t["file"], *t["id"].split("::")[1:]) for t in tests}
cpu = json.load(open("maps/cpu.json"))
cpu_cost = {t["number"]: cpu.get(t["id"], t["seconds"] * 1.5) for t in tests}
ans = json.load(open(answers))["commits"]
A = analyze.Analyzer(idx, checkout)
tot = {"B": 0.0, "pol": 0.0, "oracle": 0.0}
misses_c = misses_p = 0
by_kind = {}
for cid in sorted(ans):
    text = open(f"{data}/commits/{cid}.patch", encoding="utf-8", errors="surrogateescape").read()
    v = analyze.Verdict()
    try:
        for fp in P.parse(text):
            old = open(os.path.join(checkout, fp.old_path), "rb").read() if fp.old_path and os.path.exists(os.path.join(checkout, fp.old_path)) else None
            new = P.apply(old.decode("utf-8", "surrogateescape") if old is not None else None, fp)
            new = new.encode("utf-8", "surrogateescape") if new is not None else None
            A.analyze(fp, old, new, v)
        cand = sorted({bit_num[i] for i in S.bits(v.mask) if i in bit_num})
    except Exception as e:
        print(cid, "ANALYSIS ERROR", e); cand = sorted(dur)
    F = set(ans[cid]["failing"])
    d = S.decide(cand, True, v.fixture_only, cpu_cost, dur, meta, gamble=gamble, probe=v.needs_probe)
    miss_c = bool(F - set(cand))
    run = set(d["run"]); F_run = F & run
    out = F_run | set(d["listed"]) | (set(d["cond"]) if F_run else set())
    miss_p = bool(F - out)
    overhead = S.PROBE_CPU if v.needs_probe else (S.JOB_OVERHEAD if run else 0)
    n_probe = globals().setdefault("n_probe", [0]); n_probe[0] += v.needs_probe
    cost = sum(cpu_cost[t] for t in run) + overhead + S.pack(sorted(out), dur)[0] / 1000 + 300 * miss_p
    cb = S.pack(cand, dur)[0] / 1000 + 300 * miss_c
    co = S.pack(sorted(F), dur)[0] / 1000
    tot["B"] += cb; tot["pol"] += cost; tot["oracle"] += co
    misses_c += miss_c; misses_p += miss_p
    k = ans[cid].get("kind", "?")
    bk = by_kind.setdefault(k, [0, 0.0, 0.0, 0])
    bk[0] += 1; bk[1] += cost; bk[2] += co; bk[3] += miss_p
    if verbose or miss_p:
        print(f"{cid} {k:8s} F={len(F):3d} cand={len(cand):4d} mode={d['mode']} cost={cost:7.2f} oracle={co:6.2f}"
              f"{' MISS(cand)' if miss_c else ''}{' MISS(policy)' if miss_p and not miss_c else ''}"
              f" {sorted(F - out)[:4] if miss_p else ''} {v.reasons[:2] if miss_p else ''}")
n = len(ans)
print("probes:", globals().get("n_probe", [0])[0])
print(f"n={n} cand-misses={misses_c} policy-misses={misses_p} | policy={tot['pol']:.1f} ({tot['pol']/n:.2f}/commit)"
      f" B-only={tot['B']:.1f} oracle={tot['oracle']:.1f} ({tot['oracle']/n:.2f}/commit)")
for k, (c, cost, co, m) in sorted(by_kind.items()):
    print(f"  {k:8s} n={c:3d} policy={cost/c:6.2f}/commit oracle={co/c:6.2f}/commit misses={m}")
