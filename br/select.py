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

HEAVY_TEST_FILES = {"tests/test_optimizer.py", "tests/test_executor.py", "tests/dialects/test_duckdb.py",
                    "tests/test_optimize_cross_joins.py"}
WORKER_MS = 2000
MAX_WORKERS = 8
P_ANY_FAIL_CODE = 0.55  # chance a code change with candidates breaks at least one of them
P_ANY_FAIL_FIXTURE = 0.95
JOB_OVERHEAD = 0.25  # fork + importing sqlglot and the test modules, seconds of CPU
FILE_OVERHEAD = 0.03


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


GAMBLE = os.environ.get("BR_GAMBLE", "1") == "1"
USE_STATE = os.environ.get("BR_STATE", "0") == "1"  # diff import-time state (opt-in)
GIANT_MS = 5000
GAMBLE_MISS_P = 0.01  # chance a giant fails while every other candidate passes (0 of 82 seen)


PROBE_CPU = 0.45  # fork + importing sqlglot and every test module


P_EQUIV_FAIL = 0.05  # chance an is->== style rewrite breaks a test at all


def decide(cand, runnable, fixture_only, cpu_cost, dur, meta, gamble=None, probe=False, equiv=False):
    """Choose how to handle one commit. Returns mode, tests to run, tests to list."""
    gamble = GAMBLE if gamble is None else gamble
    out = {"mode": "EMPTY", "run": [], "listed": [], "cond": [], "run_cpu": 0.0, "gain": 0.0, "costs": ""}
    if not cand:
        return out
    cost_b = pack(cand, dur)[0] / 1000
    if not runnable:
        out.update(mode="B", listed=list(cand))
        return out
    p_any = P_ANY_FAIL_FIXTURE if fixture_only else P_ANY_FAIL_CODE

    def run_cost(tests):
        if not tests:
            return 0.0
        return (sum(cpu_cost[t] for t in tests) + (0.0 if probe else JOB_OVERHEAD)
                + FILE_OVERHEAD * len({meta[t][0] for t in tests}))

    exp_fail = min(sum(dur[t] for t in cand) / 1000, 0.6)
    cost_a = run_cost(cand) + p_any * (2.0 + exp_fail)
    cheap = [t for t in cand if cpu_cost[t] <= dur[t] / 1000 * 1.05 + 0.03]
    cheap_set = set(cheap)
    pricey = [t for t in cand if t not in cheap_set]
    cost_h = math.inf
    if cheap and pricey:
        cost_h = run_cost(cheap) + pack(pricey, dur)[0] / 1000 + 0.3 * exp_fail
    cost_g = math.inf
    # Giants: slow to list *and* to run (test_execute_tpcds, test_max_nodes). On the sample
    # and on 289 generated patches they never failed unless another candidate failed too, so
    # run every other candidate and list the giants only if one of those fails.
    giants = [t for t in cand if dur[t] >= GIANT_MS and cpu_cost[t] >= 0.8 * dur[t] / 1000]
    if equiv:
        # an is->== rewrite: guard with the cheap candidates, list the rest only if one fails
        p_any = P_EQUIV_FAIL
        giants = [t for t in cand if t not in cheap_set]
    rest = [t for t in cand if t not in set(giants)]
    if gamble and giants and rest:
        cost_g = run_cost(rest) + p_any * (pack(giants, dur)[0] / 1000 + exp_fail) + GAMBLE_MISS_P * 300
    gamble_set = giants
    best = min(cost_a, cost_b, cost_h, cost_g)
    forced = os.environ.get("BR_MODE")
    if forced == "A":
        best = cost_a
    elif forced == "B":
        best = cost_b
    out["costs"] = f"A={cost_a:.2f} B={cost_b:.2f} H={cost_h:.2f} G={cost_g:.2f}"
    if best == cost_b:
        out.update(mode="B", listed=list(cand))
    elif best == cost_a:
        out.update(mode="A", run=list(cand), run_cpu=run_cost(cand))
    elif best == cost_h:
        out.update(mode="H", run=cheap, listed=pricey, run_cpu=run_cost(cheap))
    else:
        out.update(mode="G", run=rest, cond=gamble_set, run_cpu=run_cost(rest))
    out["gain"] = cost_b - best
    return out


def state_users(keys, idx, analyzer, components) -> int:
    """Tests that can observe the changed import-time state: users of the class (or of the
    dialects wiring it in) that executed code reading the attribute; for a changed entry of a
    dict keyed by an expression class, the tests that built that expression."""
    mask = 0
    for key, label in keys:
        mod, rest = key.split(":", 1)
        base = rest.split("[", 1)[0]
        if "." in base:
            qual, attr = base.rsplit(".", 1)
            ckey = f"{mod}:{qual}"
            users = idx.class_users.get(ckey, 0)
            for dialect_key in components.get(ckey, ()):
                users |= idx.class_users.get(dialect_key, 0)
            if not users:
                users = analyzer.module_users(mod)
            narrowed = 0
            if label and label.startswith("type:"):
                narrowed = idx.class_users.get(label[5:], 0)
            if narrowed:
                mask |= users & narrowed
            else:
                readers = idx.ident_users.get(attr, 0) | idx.pair_users.get(f"{qual.split('.')[-1]}.{attr}", 0)
                mask |= (users & readers) if readers else users
        else:
            readers = idx.ident_users.get(base, 0)
            mask |= readers if readers else analyzer.module_users(mod)
    return mask


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
        self.cond: list[int] = []  # included without running, only if a run test failed
        self.job = None
        self.reasons: list[str] = []
        self.fixture_only = True
        self.gain = 0.0
        self.run_cpu = 0.0
        self.needs_probe = False
        self.equiv = False
        self.need_state = False
        self.state_mask = 0
        self.job1 = None
        self.probe_modules: list[str] = []
        self.case_filter: dict = {}
        self.cpu_override: dict = {}


def main() -> None:
    answers_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # nothing but answers may reach the real stdout
    sys.stdout = sys.stderr
    written: set[str] = set()
    try:
        _main(answers_out, written)
    except BaseException as error:  # never leave a commit unanswered
        import traceback

        log(f"fatal: {error!r}\n{traceback.format_exc()}")
        _fallback(answers_out, written)
    answers_out.flush()


def _fallback(answers_out, written: set) -> None:
    """Answer every remaining commit by listing every test (never a miss, never rejected)."""
    try:
        tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
        dur = {t["number"]: ms(t["seconds"]) for t in tests}
        workers = pack(sorted(dur), dur)[1]
    except Exception:
        workers = []
    for path in sorted(glob.glob(os.path.join(DATA, "commits", "*.patch"))):
        commit = os.path.basename(path)[: -len(".patch")]
        if commit not in written:
            answers_out.write(json.dumps({"commit": commit, "workers": workers}) + "\n")
            written.add(commit)


def _main(answers_out, written: set) -> None:
    t_start = time.monotonic()

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

    wall_limit = float(os.environ.get("BR_WALL_LIMIT", 180.0 if n_commits <= 45 else 270.0))
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
    case_maps = {}
    try:
        import gzip

        with gzip.open(os.path.join(APP, "maps", "cases.json.gz"), "rt") as f:
            for tid, cm in json.load(f).items():
                if tid in num_by_id:
                    case_maps[num_by_id[tid]] = {
                        "base": {p: set(v) for p, v in cm["base"].items()},
                        "cases": {p: {int(l): ks for l, ks in lm.items()} for p, lm in cm["cases"].items()},
                        "cpu": cm["case_cpu"],
                        "n": cm["n"],
                    }
    except Exception as error:
        log("no case map:", error)

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
            plan.equiv = verdict.equiv and not verdict.nonequiv
            plan.need_state = verdict.need_state
            plan.state_mask = verdict.state_mask
            plan.needs_probe = verdict.needs_probe and files is not None
            # TPC-DS executor test: run only the queries whose execution touches the change
            if idx is not None and case_maps:
                pos_of = {n: i for i, n in enumerate(bit_to_num) if n is not None}
                for t in list(plan.cand):
                    cm = case_maps.get(t)
                    if cm is None or t not in pos_of or (verdict.other_mask >> pos_of[t]) & 1:
                        continue
                    sel, full = set(), False
                    for hit_path, hit_lines in verdict.line_hits:
                        base = cm["base"].get(hit_path, ())
                        lines_map = cm["cases"].get(hit_path, {})
                        for ln in hit_lines:
                            if ln in base:
                                full = True
                                break
                            sel.update(lines_map.get(ln, ()))
                        if full:
                            break
                    if full or not sel or len(sel) >= cm["n"] * 0.9:
                        continue
                    plan.case_filter[t] = sorted(sel)
                    plan.cpu_override[t] = 1.8 + sum(cm["cpu"][k] for k in sel)
            plan.probe_modules = sorted(verdict.probe_modules)
            if verdict.global_change:
                plan.files = files  # still runnable, but it is a broad change
        except Exception as error:  # anything unexpected: list everything
            log(f"{commit}: analysis failed: {error!r}")
            plan.cand = list(all_nums)
            plan.files = None

    import resource as _res

    # ---- phase 1: import-time edits. Import the patched sqlglot, diff its state against the
    # pristine fingerprints, and keep only the users of what actually changed.
    state_plans = [p for p in plans if p.need_state]
    if state_plans and idx is not None and not USE_STATE:
        # cheaper on every set we measured: keep the conservative import-time candidates
        for p in state_plans:
            extra = {bit_to_num[i] for i in bits(p.state_mask) if bit_to_num[i] is not None}
            p.cand = sorted(set(p.cand) | extra)
        state_plans = []
    if state_plans and idx is not None:
        pristine = None
        try:
            import pickle

            with open(os.path.join(APP, "maps", "state.pkl"), "rb") as f:
                pristine = pickle.load(f)
        except Exception as error:
            log("no pristine state:", error)
        runnable1 = [p for p in state_plans if p.files is not None and pristine is not None]
        if runnable1:
            from br import runner as R

            R.preimport(heavy=True)
            R.PRISTINE_STATE["state"] = pristine
            jobs1 = []
            for p in runnable1:
                p.job1 = R.Job(key=p.commit, files=p.files, tests=[], timeouts={},
                               probe={"modules": p.probe_modules, "files": sorted(by_file), "state": True})
                jobs1.append(p.job1)
            try:
                R.Executor(slots=2, tmp=TMP).run(jobs1, deadline=deadline)
            except Exception as error:
                log(f"state phase failed: {error!r}")
        components = idx.extra.get("components", {})
        for p in state_plans:
            keys = p.job1.state_keys if p.job1 is not None else None
            if keys is None or (p.job1 is not None and p.job1.all_failed):
                extra_mask = p.state_mask
                log(f"{p.commit}: no state diff, fallback to {bin(extra_mask).count('1')} tests")
            else:
                extra_mask = state_users(keys, idx, analyzer, components)
                log(f"{p.commit}: state diff {len(keys)} keys {[k for k, _ in keys[:4]]} -> "
                    f"{bin(extra_mask).count('1')} tests (fallback was {bin(p.state_mask).count('1')})")
            extra = {bit_to_num[i] for i in bits(extra_mask) if bit_to_num[i] is not None}
            p.cand = sorted(set(p.cand) | extra)
            if p.job1 is not None and p.job1.probed:
                p.needs_probe = False  # already done

    def _cpu(who=_res.RUSAGE_SELF):
        u = _res.getrusage(who)
        return u.ru_utime + u.ru_stime

    log(f"cpu after analysis: self={_cpu():.2f}s")
    # ---- decide per commit
    for plan in plans:
        from collections import ChainMap

        plan_cpu = ChainMap(plan.cpu_override, cpu_cost) if plan.cpu_override else cpu_cost
        d = decide(plan.cand, plan.files is not None, plan.fixture_only, plan_cpu, dur, meta,
                   probe=plan.needs_probe, equiv=plan.equiv)
        if plan.case_filter:
            log(f"{plan.commit}: case filter {[(t, len(v)) for t, v in plan.case_filter.items()]}")
        plan.mode, plan.run, plan.listed, plan.cond = d["mode"], d["run"], d["listed"], d["cond"]
        plan.run_cpu, plan.gain = d["run_cpu"], d["gain"]
        if plan.cand:
            log(f"{plan.commit}: cand={len(plan.cand)} {d['costs']} -> {plan.mode}"
                f" | {'; '.join(plan.reasons[:3])}")

    # ---- CPU budget: two slots, keep well inside the wall-clock limit
    elapsed = time.monotonic() - t_start
    budget = max(0.0, (wall_limit - 45.0 - elapsed) * 2 * 0.8)
    runnable = [p for p in plans if p.mode in ("A", "H", "G")]
    planned = sum(p.run_cpu + JOB_OVERHEAD for p in runnable)
    if planned > budget:
        for p in sorted(runnable, key=lambda p: p.gain / max(p.run_cpu, 0.01)):
            if planned <= budget:
                break
            planned -= p.run_cpu + JOB_OVERHEAD
            p.case_filter = {}
            p.mode, p.listed, p.run, p.cond = "B", p.cand, [], []
            log(f"{p.commit}: demoted to B for the CPU budget")

    # ---- run
    runnable = [p for p in plans if (p.mode in ("A", "H", "G") and p.run) or p.needs_probe]
    all_test_files = sorted(by_file)
    if runnable:
        from br import runner as R

        heavy = any(p.needs_probe for p in runnable) or any(
            meta[t][0] in HEAVY_TEST_FILES for p in runnable for t in p.run)
        R.preimport(heavy=heavy)
        jobs = []
        for p in runnable:
            items = [(t, *meta[t]) for t in sorted(p.run, key=lambda t: (meta[t][0], meta[t][1], t))]
            if p.mode not in ("A", "H", "G"):
                items = []
            timeouts = {t: max(2.0, 4.0 * p.cpu_override.get(t, cpu_cost[t]) + 1.0) for t, *_ in items}
            probe = {"modules": p.probe_modules, "files": all_test_files} if p.needs_probe else None
            case_filter = {t: v for t, v in p.case_filter.items() if any(t == i[0] for i in items)}
            p.job = R.Job(key=p.commit, files=p.files, tests=items, timeouts=timeouts, probe=probe,
                          case_filter=case_filter or None)
            jobs.append(p.job)
        # biggest first so the two slots finish together
        jobs.sort(key=lambda j: -sum(cpu_cost[t[0]] for t in j.tests if not (j.case_filter and t[0] in j.case_filter)))
        log(f"cpu after preimport: self={_cpu():.2f}s")
        try:
            R.Executor(slots=2, tmp=TMP).run(jobs, deadline=deadline)
        except Exception as error:  # list what we could not finish running
            import traceback

            log(f"executor failed: {error!r}\n{traceback.format_exc()}")
            for j in jobs:
                done = j.failed | j.passed
                j.failed.update(t[0] for t in j.tests if t[0] not in done)
                if j.probe and not j.probed:
                    j.all_failed = True
        log(f"cpu after run: self={_cpu():.2f}s children={_cpu(_res.RUSAGE_CHILDREN):.2f}s")
        for j in jobs:
            log(f"  job {j.key}: tests={len(j.tests)} test_cpu={sum(j.cpu.values()):.2f}s")

    # ---- answer
    total_ms = 0
    for p in plans:
        out: list[int] = list(p.listed) if p.mode in ("B", "H") else []
        for job in (p.job1, p.job):
            if job is None:
                continue
            failed = set(job.failed)
            for f in job.failed_files:
                failed.update(by_file.get(f, ()))
            for c in job.failed_classes:
                failed.update(by_class.get(c, ()))
            for mod in job.failed_modules:
                if idx is not None:
                    failed.update(bit_to_num[i] for i in bits(analyzer.module_users(mod))
                                  if bit_to_num[i] is not None)
            if job.all_failed:
                failed.update(all_nums)
            if p.mode == "G" and (failed & set(p.run)):
                failed.update(p.cond)
            out = sorted(set(out) | failed)
            if job.note:
                log(f"{p.commit}: {job.note.strip()[:300]}")
        valid = sorted(set(n for n in out if n in dur))
        cost, workers = pack(valid, dur)
        total_ms += cost
        log(f"{p.commit}: mode={p.mode} cand={len(p.cand)} out={len(valid)} cost={cost/1000:.3f}")
        answers_out.write(json.dumps({"commit": p.commit, "workers": workers}) + "\n")
        written.add(p.commit)
    answers_out.flush()
    log(f"done in {time.monotonic() - t_start:.1f}s, simulated (excl. misses) {total_ms/1000:.1f}s")


if __name__ == "__main__":
    main()
