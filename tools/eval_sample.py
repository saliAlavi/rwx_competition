"""Offline: candidate recall/cost of br.analyze on a labeled set.
    python tools/eval_sample.py <data_dir> <answers.json> <checkout>"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import patch as P, index, analyze

data, answers, checkout = sys.argv[1:4]
idx = index.load("/nonexistent", "maps/maps.json.gz")
tests = json.load(open(f"{data}/tests.json"))["tests"]
pos = {tid: i for i, tid in enumerate(idx.tests)}
num_of_bit = {pos[t["id"]]: t["number"] for t in tests if t["id"] in pos}
sec = {t["number"]: t["seconds"] for t in tests}
ans = json.load(open(answers))["commits"]
A = analyze.Analyzer(idx, checkout)
tot_c = 0; misses = 0
for cid in sorted(ans):
    text = open(f"{data}/commits/{cid}.patch", encoding="utf-8", errors="surrogateescape").read()
    v = analyze.Verdict()
    for fp in P.parse(text):
        old = open(os.path.join(checkout, fp.old_path), "rb").read() if fp.old_path else None
        new = P.apply(old.decode("utf-8", "surrogateescape") if old is not None else None, fp)
        new = new.encode("utf-8", "surrogateescape") if new is not None else None
        A.analyze(fp, old, new, v)
    cand = {num_of_bit[i] for i in range(len(idx.tests)) if v.mask >> i & 1}
    fail = set(ans[cid]["failing"])
    miss = fail - cand
    misses += bool(miss)
    cs = sum(sec[n] for n in cand)
    tot_c += cs
    print(f"{cid} {ans[cid]['kind']:8s} fail={len(fail):3d} cand={len(cand):5d} sec={cs:7.2f} {'MISS '+str(sorted(miss)[:5]) if miss else ''} {v.reasons[:4]}")
print("misses", misses, "total cand sec", round(tot_c, 1))
