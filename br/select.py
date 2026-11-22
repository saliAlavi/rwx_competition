"""Blast Radius selector: for each patch, which sqlglot tests to run and on how many workers.

Pipeline per commit:
  1. parse the patch and apply it to the pristine checkout (in memory)
  2. analyze it against the committed maps -> candidate tests (a superset of what can fail)
  3. pick the cheaper of
       A  run the candidates here and list only the ones that fail
       B  list every candidate without running anything
       H  run the candidates that are cheap to run, list the rest unrun
  4. run the chosen tests in forked children against a patched overlay tree
  5. print the failing/listed tests, packed onto the number of workers that minimises
     (busiest worker + 2 s per worker)
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp")

WORKER_MS = 2000
MAX_WORKERS = 8
P_ANY_FAIL_CODE = 0.55  # chance a code change with candidates breaks at least one of them
P_ANY_FAIL_FIXTURE = 0.95
JOB_OVERHEAD = 0.30  # fork + importing sqlglot and the test modules, seconds of CPU
FILE_OVERHEAD = 0.04


def log(*args) -> None:
    print("[select]", *args, file=sys.stderr, flush=True)


def ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def pack(nums: list[int], dur: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Best (cost_ms, workers) over 1..8 workers, longest-processing-time first."""
    if not nums:
        return 0, []
    order = sorted(nums, key=lambda n: (-dur[n], n))
    total = sum(dur[n] for n in nums)
    longest = dur[order[0]]
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        if best is not None and max(longest, total / k) + WORKER_MS * k >= best[0]:
            # lower bound already worse; larger k only adds worker cost once balanced
            if total / k <= longest:
                break
            continue
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += dur[n]
            bins[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best  # type: ignore[return-value]


def bits(mask: int):
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


class Plan:
    def __init__(self, commit: str):
        self.commit = commit
        self.cand: list[int] = []
        self.files: dict | None = {}
        self.mode = "B"
        self.run: list[int] = []
        self.listed: list[int] = []  # included without running
        self.job = None
        self.reasons: list[str] = []
        self.fixture_only = True
        self.gain = 0.0
        self.run_cpu = 0.0


def main() -> None:
    t_start = time.monotonic()
    answers_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # nothing but answers may reach the real stdout
    sys.stdout = sys.stderr

    sys.path.insert(0, APP)
    from br import analyze as A
    from br import index as I
    from br import patch as P

    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    commit_paths = sorted(glob.glob(os.path.join(DATA, "commits", "*.patch")))
    n_commits = len(commit_paths)
    num_by_id = {t["id"]: t["number"] for t in tests}
    dur = {t["number"]: ms(t["seconds"]) for t in tests}
    meta = {}
    by_file: dict[str, list[int]] = {}
    by_class: dict[str, list[int]] = {}
    for t in tests:
        parts = t["id"].split("::")
        cls = parts[1] if len(parts) >= 3 else ""
        meth = parts[-1]
        meta[t["number"]] = (t["file"], cls, meth)
        by_file.setdefault(t["file"], []).append(t["number"])
        by_class.setdefault(f"{t['file']}::{cls}", []).append(t["number"])

    wall_limit = 180.0 if n_commits <= 45 else 270.0
    deadline = t_start + wall_limit - 30.0

    try:
        idx = I.load(os.path.join(APP, "maps", "index.pkl"), os.path.join(APP, "maps", "maps.json.gz"))
    except Exception as error:
        idx = None
        log("no maps:", error)
    cpu_cost: dict[int, float] = {}
    try:
        raw = json.load(open(os.path.join(APP, "maps", "cpu.json")))
        for tid, c in raw.items():
            if tid in num_by_id:
                cpu_cost[num_by_id[tid]] = c
    except Exception as error:
        log("no cpu map:", error)
    for t in tests:
        cpu_cost.setdefault(t["number"], t["seconds"] * 1.5 + 0.02)

    all_nums = [t["number"] for t in tests]
    bit_to_num: list = []
    unmapped: set[int] = set(all_nums)
    if idx is not None:
        bit_to_num = [num_by_id.get(tid) for tid in idx.tests]
        unmapped -= {n for n in bit_to_num if n is not None}
        analyzer = A.Analyzer(idx, SRC)
    if unmapped:
        log(f"{len(unmapped)} tests are not in the maps; they are listed whenever their file is touched")

    plans: list[Plan] = []
    for path in commit_paths:
        commit = os.path.basename(path)[: -len(".patch")]
        plan = Plan(commit)
        plans.append(plan)
        try:
            raw_text = open(path, "rb").read().decode("utf-8", "surrogateescape")
            fps = P.parse(raw_text)
            verdict = A.Verdict()
            files: dict = {}
            for fp in fps:
                old = None
                if fp.old_path:
                    full = os.path.join(SRC, fp.old_path)
                    if os.path.isfile(full):
                        old = open(full, "rb").read()
                if fp.binary:
                    files = None  # cannot reproduce: list candidates instead of running
                    if idx is not None:
                        for p in filter(None, (fp.old_path, fp.new_path)):
                            verdict.add(idx.open_b.get(p, 0) | idx.open_a.get(p, 0), f"binary:{p}")
                    continue
                if fp.hunks:
                    new_text = P.apply(old.decode("utf-8", "surrogateescape") if old is not None else None, fp)
                    new = new_text.encode("utf-8", "surrogateescape") if new_text is not None else None
                else:
                    new = old if fp.new_path else None
                if files is not None:
                    if fp.old_path and fp.old_path != fp.new_path:
                        files[fp.old_path] = None
                    if fp.new_path:
                        files[fp.new_path] = new
                if idx is not None:
                    analyzer.analyze(fp, old, new, verdict)
                else:
                    verdict.global_change = True
            if idx is None:
                cand = set(all_nums)
            else:
                cand = {bit_to_num[i] for i in bits(verdict.mask) if bit_to_num[i] is not None}
                if unmapped and cand:
                    touched = {meta[n][0] for n in cand}
                    cand |= {n for n in unmapped if meta[n][0] in touched}
            plan.cand = sorted(cand)
            plan.files = files
            plan.reasons = verdict.reasons
            plan.fixture_only = verdict.fixture_only
            if verdict.global_change:
                plan.files = files  # still runnable, but it is a broad change
        except Exception as error:  # anything unexpected: list everything
            log(f"{commit}: analysis failed: {error!r}")
            plan.cand = list(all_nums)
            plan.files = None

    # ---- decide per commit
    for plan in plans:
        cand = plan.cand
        if not cand:
            plan.mode = "EMPTY"
            continue
        cost_b = pack(cand, dur)[0] / 1000
        if plan.files is None:
            plan.mode, plan.listed = "B", cand
            continue
        p_any = P_ANY_FAIL_FIXTURE if plan.fixture_only else P_ANY_FAIL_CODE
        n_files = len({meta[t][0] for t in cand})
        cpu_all = sum(cpu_cost[t] for t in cand)
        exp_fail = min(sum(dur[t] for t in cand) / 1000, 0.6)
        cost_a = cpu_all + JOB_OVERHEAD + FILE_OVERHEAD * n_files + p_any * (2.0 + exp_fail)
        cheap = [t for t in cand if cpu_cost[t] <= dur[t] / 1000 * 1.05 + 0.03]
        pricey = [t for t in cand if t not in set(cheap)]
        cost_h = math.inf
        if cheap and pricey:
            n_files_h = len({meta[t][0] for t in cheap})
            cost_h = (
                sum(cpu_cost[t] for t in cheap) + JOB_OVERHEAD + FILE_OVERHEAD * n_files_h
                + pack(pricey, dur)[0] / 1000 + 0.3 * exp_fail
            )
        best = min(cost_a, cost_b, cost_h)
        if best == cost_b:
            plan.mode, plan.listed = "B", cand
        elif best == cost_a:
            plan.mode, plan.run = "A", cand
            plan.run_cpu = cpu_all
        else:
            plan.mode, plan.run, plan.listed = "H", cheap, pricey
            plan.run_cpu = sum(cpu_cost[t] for t in cheap)
        plan.gain = cost_b - best
        log(f"{plan.commit}: cand={len(cand)} A={cost_a:.2f} B={cost_b:.2f} H={cost_h:.2f} -> {plan.mode}"
            f" | {'; '.join(plan.reasons[:3])}")

    # ---- CPU budget: two slots, keep well inside the wall-clock limit
    elapsed = time.monotonic() - t_start
    budget = max(0.0, (wall_limit - 45.0 - elapsed) * 2 * 0.8)
    runnable = [p for p in plans if p.mode in ("A", "H")]
    planned = sum(p.run_cpu + JOB_OVERHEAD for p in runnable)
    if planned > budget:
        for p in sorted(runnable, key=lambda p: p.gain / max(p.run_cpu, 0.01)):
            if planned <= budget:
                break
            planned -= p.run_cpu + JOB_OVERHEAD
            p.mode, p.listed, p.run = "B", p.cand, []
            log(f"{p.commit}: demoted to B for the CPU budget")

    # ---- run
    runnable = [p for p in plans if p.mode in ("A", "H") and p.run]
    if runnable:
        from br import runner as R

        R.preimport()
        jobs = []
        for p in runnable:
            items = [(t, *meta[t]) for t in sorted(p.run, key=lambda t: (meta[t][0], meta[t][1], t))]
            timeouts = {t: max(2.0, 4.0 * cpu_cost[t] + 1.0) for t in p.run}
            p.job = R.Job(key=p.commit, files=p.files, tests=items, timeouts=timeouts)
            jobs.append(p.job)
        # biggest first so the two slots finish together
        jobs.sort(key=lambda j: -sum(cpu_cost[t[0]] for t in j.tests))
        R.Executor(slots=2, tmp=TMP).run(jobs, deadline=deadline)

    # ---- answer
    total_ms = 0
    for p in plans:
        if p.mode == "EMPTY":
            out: list[int] = []
        elif p.mode == "B":
            out = list(p.listed)
        else:
            job = p.job
            failed = set(job.failed)
            for f in job.failed_files:
                failed.update(by_file.get(f, ()))
            for c in job.failed_classes:
                failed.update(by_class.get(c, ()))
            out = sorted(failed | set(p.listed))
            if job.note:
                log(f"{p.commit}: {job.note.strip()[:300]}")
        valid = sorted(set(n for n in out if n in dur))
        cost, workers = pack(valid, dur)
        total_ms += cost
        log(f"{p.commit}: mode={p.mode} cand={len(p.cand)} out={len(valid)} cost={cost/1000:.3f}")
        answers_out.write(json.dumps({"commit": p.commit, "workers": workers}) + "\n")
    answers_out.flush()
    log(f"done in {time.monotonic() - t_start:.1f}s, simulated (excl. misses) {total_ms/1000:.1f}s")


if __name__ == "__main__":
    main()
