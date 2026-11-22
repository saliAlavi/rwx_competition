"""pytest plugin that records what one test touches. Used offline to build maps.

Run one test per fresh process:
    BR_OUT=/out/12.json python -m pytest -p br_trace -p no:cacheprovider <nodeid>

Records, separately for collection (phase A: pytest start-up and importing the
test module) and for the test itself (phase B: setup, call and teardown):
  * lines executed in /opt/sqlglot/{sqlglot,tests}/*.py, split by whether the
    code object was a function or a module/class body
  * sqlglot/tests classes instantiated (with their MRO) during phase B
  * files opened / directories listed under the sqlglot checkout
  * sqlglot/tests modules present in sys.modules after each phase
  * the test's outcome
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.environ.get("BR_APP", "/app"))
from br import syncpool  # noqa: E402

syncpool.install()
syncpool.limit_threads()

ROOT = os.environ.get("BR_ROOT", "/opt/sqlglot") + "/"
WATCH = (ROOT + "sqlglot/", ROOT + "tests/")
OUT = os.environ.get("BR_OUT")

mon = sys.monitoring
TOOL = 3  # sys.monitoring.OPTIMIZER_ID is unused by anything else here
CO_NEWLOCALS = 0x2

state = {"phase": "A"}
lines = {"A": {}, "B_func": {}, "B_body": {}}
files = {"A": set(), "B": set()}
classes: set = set()
modules = {}
outcome = {"when": None, "outcome": "passed", "longrepr": ""}

_DISABLE = mon.DISABLE


def _on_line(code, line):
    filename = code.co_filename
    if not filename.startswith(WATCH):
        return _DISABLE
    if state["phase"] == "A":
        bucket = lines["A"]
    elif code.co_flags & CO_NEWLOCALS:
        bucket = lines["B_func"]
    else:
        bucket = lines["B_body"]
    s = bucket.get(filename)
    if s is None:
        s = bucket[filename] = set()
    s.add(line)
    return _DISABLE


def _on_call(code, offset, callable_, arg0):
    if state["phase"] != "B":
        return
    if not isinstance(callable_, type):
        owner = getattr(callable_, "__self__", None)
        if isinstance(owner, type):
            callable_ = owner  # classmethod called on a class
        else:
            return
    module = getattr(callable_, "__module__", "") or ""
    if module.startswith(("sqlglot", "tests")):
        classes.add(callable_)


def _audit(event, args):
    if event == "open":
        path = args[0]
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
        path = args[0] if args else None
    else:
        return
    if isinstance(path, bytes):
        try:
            path = path.decode()
        except Exception:
            return
    if not isinstance(path, str):
        return
    if not path.startswith("/"):
        path = os.path.join(os.getcwd(), path)
    path = os.path.normpath(path)
    if path.startswith(ROOT) or path + "/" == ROOT:
        files["A" if state["phase"] == "A" else "B"].add(path[len(ROOT):] or ".")


mon.use_tool_id(TOOL, "br_trace")
mon.register_callback(TOOL, mon.events.LINE, _on_line)
mon.register_callback(TOOL, mon.events.CALL, _on_call)
mon.set_events(TOOL, mon.events.LINE | mon.events.CALL)
sys.addaudithook(_audit)


def _snapshot_modules():
    return sorted(m for m in list(sys.modules) if m.startswith(("sqlglot", "tests")))


def pytest_runtest_setup(item):
    if state["phase"] == "A":
        modules["A"] = _snapshot_modules()
        state["phase"] = "B"
        state["t0"] = time.process_time()
        mon.restart_events()


def pytest_runtest_logreport(report):
    if report.outcome == "failed" or (report.when == "call" and report.outcome == "skipped"):
        if outcome["outcome"] != "failed":
            outcome.update(
                when=report.when,
                outcome=report.outcome,
                longrepr=str(report.longrepr)[-2000:],
            )
    # pytest 9 subtests: a failed subtest is reported with outcome "failed" too.


def pytest_sessionfinish(session, exitstatus):
    state["phase"] = "done"
    mon.set_events(TOOL, 0)
    if not OUT:
        return
    mro = set()
    for klass in classes:
        for base in klass.__mro__:
            module = getattr(base, "__module__", "") or ""
            if module.startswith(("sqlglot", "tests")):
                mro.add(f"{module}:{base.__qualname__}")
    rel = lambda d: {k[len(ROOT):]: sorted(v) for k, v in d.items()}
    data = {
        "exitstatus": int(exitstatus),
        "collected": session.testscollected,
        "outcome": outcome,
        "cpu_b": time.process_time() - state.get("t0", time.process_time()),
        "lines_a": rel(lines["A"]),
        "lines_b_func": rel(lines["B_func"]),
        "lines_b_body": rel(lines["B_body"]),
        "files_a": sorted(files["A"]),
        "files_b": sorted(files["B"]),
        "classes": sorted(mro),
        "modules_a": modules.get("A", []),
        "modules_b": _snapshot_modules(),
    }
    with open(OUT, "w") as f:
        json.dump(data, f)
