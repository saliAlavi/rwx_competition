"""Run selected sqlglot tests against patched trees, in forked children.

The parent pre-imports the heavy third-party modules once (pandas, duckdb,
numpy, unittest...) and then forks one child per job. A job is one commit's
list of tests. The child imports sqlglot and the test modules from a symlink
overlay of the pristine checkout in which only the patched files are real, runs
the tests with plain unittest (the suite uses no pytest features), and streams
one line per test back to the parent. The parent enforces per-test time limits
and restarts a job after a crash or a kill.

What counts as a failure follows the labels: a test fails when it errors,
fails, has a failing subtest, times out, or when its file cannot be imported
(then every test of that file fails). A setUpClass error fails every test of
its class.
"""

from __future__ import annotations

import json
import os
import resource
import selectors
import shutil
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field

SRC = os.environ.get("BR_SRC", "/opt/sqlglot")


def log(*args):
    print("[runner]", *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- tree


class Tree:
    """/tmp/wN: symlinks into the read-only checkout, with patched files made real."""

    def __init__(self, root: str):
        self.root = root
        self.materialized: set[str] = set()
        self.reset()

    def reset(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)
        for name in os.listdir(SRC):
            os.symlink(os.path.join(SRC, name), os.path.join(self.root, name))
        self.materialized = {""}

    def _materialize_dir(self, rel_dir: str) -> None:
        cur = ""
        for part in [p for p in rel_dir.split("/") if p]:
            cur = f"{cur}/{part}" if cur else part
            if cur in self.materialized:
                continue
            dst = os.path.join(self.root, cur)
            src = os.path.join(SRC, cur)
            if os.path.islink(dst):
                os.unlink(dst)
            os.makedirs(dst, exist_ok=True)
            if os.path.isdir(src):
                for name in os.listdir(src):
                    link = os.path.join(dst, name)
                    if not os.path.lexists(link):
                        os.symlink(os.path.join(src, name), link)
            self.materialized.add(cur)

    def write(self, rel: str, data: bytes | None) -> None:
        self._materialize_dir(os.path.dirname(rel))
        dst = os.path.join(self.root, rel)
        if os.path.lexists(dst):
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.unlink(dst)
        if data is not None:
            with open(dst, "wb") as f:
                f.write(data)


# --------------------------------------------------------------------------- child


class _Timeout(BaseException):
    pass


class ForkFailed(Exception):
    pass


# Case filter for the executor's TPC-DS test: only the selected queries run, the others
# return None, which is how the test itself skips queries it does not execute. Defined at
# module level so the in-process pool can pickle it by reference.
_CASES = {"sel": None, "k": 0, "orig": None}


def _filtered_mp_execute(expression, meta):
    k = _CASES["k"]
    _CASES["k"] += 1
    if _CASES["sel"] is not None and k not in _CASES["sel"]:
        return None
    return _CASES["orig"](expression, meta)


RSS_LIMIT_BYTES = int(float(os.environ.get("BR_RSS_LIMIT_GB", "1.4")) * (1 << 30))
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _rss(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * _PAGE
    except (OSError, ValueError, IndexError):
        return 0


def _child_main(tree_root: str, plan: list, wfd: int, timeouts: dict, probe: dict | None = None,
                case_filter: dict | None = None) -> None:
    import importlib
    import unittest
    from unittest.suite import _ErrorHolder

    from br import syncpool

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if not os.environ.get("BR_CHILD_STDERR"):
        os.dup2(devnull, 2)
    os.chdir(tree_root)
    sys.path.insert(0, tree_root)
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(*items):
        out.write(json.dumps(items) + "\n")

    def on_alarm(signum, frame):
        raise _Timeout("br per-test timeout")

    signal.signal(signal.SIGPROF, on_alarm)
    current = {"file": None, "cls_key": None}

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.failed_now = False
            self.t0 = 0.0

        def startTest(self, test):
            super().startTest(test)
            self.failed_now = False
            num = getattr(test, "_br_num", None)
            limit = timeouts.get(num, 10.0)
            emit("start", num)
            self._case_mod = None
            if case_filter and num in case_filter:
                mod = sys.modules.get(test.__class__.__module__)
                if mod is not None and hasattr(mod, "mp_execute"):
                    _CASES.update(sel=set(case_filter[num]), k=0, orig=mod.mp_execute)
                    mod.mp_execute = _filtered_mp_execute
                    self._case_mod = mod
            self.t0 = time.process_time()
            signal.setitimer(signal.ITIMER_PROF, limit)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_PROF, 0)
            num = getattr(test, "_br_num", None)
            if getattr(self, "_case_mod", None) is not None:
                self._case_mod.mp_execute = _CASES["orig"]
                _CASES.update(sel=None, k=0, orig=None)
                self._case_mod = None
            if syncpool.BROKEN["flag"]:
                syncpool.BROKEN["flag"] = False
                emit("clsfail", f"{test.__class__.__module__.replace('.', '/')}.py::{test.__class__.__name__}",
                     "process pool would break")
            emit("end", num, self.failed_now, round(time.process_time() - self.t0, 4))
            super().stopTest(test)

        def _holder(self, test, err):
            if isinstance(test, _ErrorHolder):
                # setUpClass / setUpModule (or their teardown) failed: every test of the
                # class (or file) fails, including ones we were not asked to run
                desc = test.description
                if "Module" in desc.split(" ")[0]:
                    emit("impfail", current["file"], f"module fixture: {repr(err[1])[:200]}")
                else:
                    emit("clsfail", current["cls_key"], f"{desc[:80]}: {repr(err[1])[:200]}")
                return True
            return False

        def addError(self, test, err):
            if not self._holder(test, err):
                self.failed_now = True
                if os.environ.get("BR_DEBUG"):
                    emit("why", getattr(test, "_br_num", None), repr(err[1])[:300])

        def addFailure(self, test, err):
            self.failed_now = True
            if os.environ.get("BR_DEBUG"):
                emit("why", getattr(test, "_br_num", None), repr(err[1])[:300])

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.failed_now = True
                if os.environ.get("BR_DEBUG"):
                    emit("why", getattr(test, "_br_num", None), repr(err[1])[:300])

        def addUnexpectedSuccess(self, test):
            self.failed_now = True

    if probe is not None:
        try:
            signal.setitimer(signal.ITIMER_PROF, 30.0)
            importlib.import_module("sqlglot")
            signal.setitimer(signal.ITIMER_PROF, 0)
        except BaseException as error:  # noqa: BLE001
            signal.setitimer(signal.ITIMER_PROF, 0)
            emit("allfail", repr(error)[:300])
            emit("done")
            out.flush()
            os._exit(0)
        for mod in probe.get("modules", ()):
            try:
                signal.setitimer(signal.ITIMER_PROF, 30.0)
                importlib.import_module(mod)
                signal.setitimer(signal.ITIMER_PROF, 0)
            except BaseException as error:  # noqa: BLE001
                signal.setitimer(signal.ITIMER_PROF, 0)
                emit("modfail", mod, repr(error)[:300])
        for file in probe.get("files", ()):
            try:
                signal.setitimer(signal.ITIMER_PROF, 30.0)
                importlib.import_module(file[:-3].replace("/", "."))
                signal.setitimer(signal.ITIMER_PROF, 0)
            except BaseException as error:  # noqa: BLE001
                signal.setitimer(signal.ITIMER_PROF, 0)
                emit("impfail", file, repr(error)[:300])
        emit("probed")

    for file, items in plan:
        modname = file[:-3].replace("/", ".")
        try:
            signal.setitimer(signal.ITIMER_PROF, 30.0)
            module = importlib.import_module(modname)
            signal.setitimer(signal.ITIMER_PROF, 0)
        except BaseException as error:  # noqa: BLE001 - any import problem fails the file
            signal.setitimer(signal.ITIMER_PROF, 0)
            emit("impfail", file, repr(error)[:300])
            continue
        by_class: dict[str, list] = {}
        for cls_name, method, num in items:
            by_class.setdefault(cls_name, []).append((method, num))
        for cls_name, tests in by_class.items():
            current.update(file=file, cls_key=f"{file}::{cls_name}")
            cls = getattr(module, cls_name, None)
            suite = unittest.TestSuite()
            for method, num in tests:
                try:
                    case = cls(method)
                except Exception:
                    emit("start", num)
                    emit("end", num, True, 0.0)
                    continue
                case._br_num = num
                suite.addTest(case)
            result = Result()
            try:
                suite.run(result)
            except _Timeout:
                emit("clsfail", f"{file}::{cls_name}", "timeout outside a test")
            pass
    emit("done")
    out.flush()
    os._exit(0)


# --------------------------------------------------------------------------- parent


@dataclass
class Job:
    key: str  # commit id
    files: dict  # rel path -> bytes | None
    tests: list  # [(num, file, cls, method)]
    timeouts: dict  # num -> seconds
    probe: dict | None = None  # {"modules": [...], "files": [...]} imported before the tests
    case_filter: dict | None = None  # test number -> case indices to run
    # results
    failed: set = field(default_factory=set)
    passed: set = field(default_factory=set)
    failed_files: set = field(default_factory=set)
    failed_classes: set = field(default_factory=set)
    failed_modules: set = field(default_factory=set)
    all_failed: bool = False
    probed: bool = False
    cpu: dict = field(default_factory=dict)
    aborted: bool = False
    note: str = ""


@dataclass
class _Active:
    job: Job
    pid: int
    rfd: int
    buf: bytes = b""
    current: int | None = None
    started_at: float = 0.0
    remaining: list = field(default_factory=list)


class Executor:
    def __init__(self, slots: int = 2, tmp: str = "/tmp"):
        self.trees = [Tree(os.path.join(tmp, f"w{i}")) for i in range(slots)]

    @staticmethod
    def _plan(tests):
        plan: list = []
        index: dict = {}
        for num, file, cls, method in tests:
            if file not in index:
                index[file] = len(plan)
                plan.append((file, []))
            plan[index[file]][1].append((cls, method, num))
        return plan

    def _spawn(self, slot: int, job: Job, tests: list) -> _Active:
        tree = self.trees[slot]
        tree.reset()
        for rel, data in job.files.items():
            tree.write(rel, data)
        rfd, wfd = os.pipe()
        pid = None
        for attempt in range(25):
            try:
                pid = os.fork()
                break
            except OSError as error:  # EAGAIN: process limit reached; wait for children to exit
                log(f"fork failed ({error}); retry {attempt}")
                time.sleep(0.05 * (attempt + 1))
        if pid is None:
            os.close(rfd)
            os.close(wfd)
            raise ForkFailed()
        if pid == 0:  # child
            try:
                os.close(rfd)
                _child_main(tree.root, self._plan(tests), wfd, job.timeouts,
                            job.probe if not job.probed else None, job.case_filter)
            except BaseException:
                pass
            os._exit(1)
        os.close(wfd)
        os.set_blocking(rfd, False)
        return _Active(job=job, pid=pid, rfd=rfd, remaining=list(tests))

    def run(self, jobs: list[Job], deadline: float | None = None, hard_factor: float = 1.5) -> None:
        queue: deque = deque((job, list(job.tests)) for job in jobs if job.tests or job.probe)
        active: dict[int, _Active] = {}
        sel = selectors.DefaultSelector()
        free = list(range(len(self.trees)))

        def finish(slot: int, a: _Active, killed_reason: str | None) -> None:
            sel.unregister(a.rfd)
            os.close(a.rfd)
            try:
                os.kill(a.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(a.pid, 0)
            except ChildProcessError:
                pass
            del active[slot]
            free.append(slot)
            job = a.job
            if job.probe and not job.probed:
                job.all_failed = True
                job.note += " probe-crash"
            if a.remaining:
                # the child died or was killed: the running test fails, the rest are re-run
                if a.current is not None:
                    job.failed.add(a.current)
                    job.note += f" crash@{a.current}:{killed_reason}"
                    for t in job.tests:
                        if t[0] == a.current and killed_reason != "timeout":
                            job.failed_classes.add(f"{t[1]}::{t[2]}")
                rest = [t for t in a.remaining if t[0] != a.current]
                if killed_reason == "timeout":
                    # a hang usually repeats in every test reaching the change: give up on the rest
                    job.failed.update(t[0] for t in rest)
                    job.aborted = True
                elif rest and a.current is not None:
                    queue.appendleft((job, rest))
                elif rest:
                    job.failed.update(t[0] for t in rest)
                    job.aborted = True

        def handle_line(a: _Active, msg: list) -> None:
            kind = msg[0]
            job = a.job
            if kind == "start":
                a.current = msg[1]
                a.started_at = time.monotonic()
            elif kind == "end":
                num, failed, cpu = msg[1], msg[2], msg[3]
                (job.failed if failed else job.passed).add(num)
                job.cpu[num] = cpu
                a.remaining = [t for t in a.remaining if t[0] != num]
                a.current = None
            elif kind == "impfail":
                job.failed_files.add(msg[1])
                a.remaining = [t for t in a.remaining if t[1] != msg[1]]
                job.note += f" impfail:{msg[1]}:{msg[2][:80]}"
            elif kind == "clsfail":
                job.failed_classes.add(msg[1])
                job.note += f" clsfail:{msg[1]}"
            elif kind == "allfail":
                job.all_failed = True
                job.probed = True
                a.remaining = []
                job.note += f" allfail:{msg[1][:80]}"
            elif kind == "modfail":
                job.failed_modules.add(msg[1])
                job.note += f" modfail:{msg[1]}:{msg[2][:80]}"
            elif kind == "probed":
                job.probed = True
            elif kind == "why":
                job.note += f" why{msg[1]}:{msg[2][:150]}"
            elif kind == "done":
                # never started or never finished (e.g. its setUpClass failed): count as failed
                for t in a.remaining:
                    job.failed.add(t[0])
                if a.remaining:
                    job.note += f" unreported:{len(a.remaining)}"
                a.remaining = []

        last_pidlog = 0.0
        while queue or active:
            if os.environ.get("BR_PIDLOG") and time.monotonic() - last_pidlog > 0.5:
                last_pidlog = time.monotonic()
                total, rows = task_count()
                if total > 60:
                    log(f"tasks={total} {rows[:6]} active={[a.job.key for a in active.values()]}")
            while free and queue:
                job, tests = queue.popleft()
                if deadline is not None and time.monotonic() > deadline:
                    job.failed.update(t[0] for t in tests)
                    job.aborted = True
                    job.note += " deadline"
                    continue
                slot = free.pop()
                try:
                    a = self._spawn(slot, job, tests)
                except ForkFailed:
                    free.append(slot)
                    job.failed.update(t[0] for t in tests)
                    if job.probe and not job.probed:
                        job.all_failed = True
                    job.aborted = True
                    job.note += " fork-failed"
                    continue
                active[slot] = a
                sel.register(a.rfd, selectors.EVENT_READ, slot)
            if not active:
                continue
            for key, _ in sel.select(timeout=0.25):
                slot = key.data
                a = active.get(slot)
                if a is None:
                    continue
                try:
                    chunk = os.read(a.rfd, 1 << 16)
                except BlockingIOError:
                    continue
                if not chunk:
                    finish(slot, a, "exit" if a.remaining else None)
                    continue
                a.buf += chunk
                *lines, a.buf = a.buf.split(b"\n")
                for line in lines:
                    if line:
                        handle_line(a, json.loads(line))
            now = time.monotonic()
            for slot, a in list(active.items()):
                if _rss(a.pid) > RSS_LIMIT_BYTES:
                    log(f"{a.job.key}: child over memory limit, killed")
                    finish(slot, a, "memory")
                    continue
                if a.current is not None:
                    limit = a.job.timeouts.get(a.current, 10.0) * hard_factor + 2.0
                    if now - a.started_at > limit:
                        finish(slot, a, "timeout")


def task_count():
    """Processes + threads visible in /proc (what --pids-limit counts)."""
    total, rows = 0, []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            n = len(os.listdir(f"/proc/{entry}/task"))
            comm = open(f"/proc/{entry}/comm").read().strip()
        except OSError:
            continue
        total += n
        rows.append((n, entry, comm))
    rows.sort(reverse=True)
    return total, rows


def child_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def self_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


PREIMPORT = (
    "unittest", "unittest.mock", "json", "sqlite3", "decimal", "datetime", "tempfile",
    "concurrent.futures", "concurrent.futures.process", "multiprocessing", "tracemalloc",
    "gc", "typing", "dataclasses", "re", "pathlib", "pkgutil", "importlib", "ast",
    "collections", "functools", "itertools", "math", "logging", "copy", "pickle", "textwrap",
    "zoneinfo", "numpy", "pandas", "pandas.testing", "duckdb", "dateutil", "pytz",
)


def preimport(heavy: bool = True) -> None:
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    from br import syncpool

    syncpool.limit_threads()
    import importlib

    for name in PREIMPORT:
        if not heavy and name.split(".")[0] in ("numpy", "pandas", "duckdb", "dateutil", "pytz"):
            continue
        try:
            importlib.import_module(name)
        except Exception as error:  # pragma: no cover
            log("preimport failed", name, error)
    syncpool.install()
    syncpool.limit_threads()
