"""Per-query coverage for the executor's TPC tests (offline, in the env image).

    python /app/tools/trace_cases.py /out/cases.json

For test_execute_tpch / test_execute_tpcds every query k is parsed, executed by
mp_execute (through the in-process pool) and compared in subtestHelper. Lines run
while handling query k are attributed to case k; everything else (setUpClass, the
result loop) is "base". At run time only the cases whose lines a patch touches are
executed, and the test is listed only if one of them fails.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, "/app")
sys.path.insert(0, "/opt/sqlglot")
os.chdir("/opt/sqlglot")
from br import syncpool  # noqa: E402

syncpool.limit_threads()
syncpool.install()

ROOT = "/opt/sqlglot/"
WATCH = (ROOT + "sqlglot/", ROOT + "tests/")
mon = sys.monitoring
TOOL = 4
state = {"bucket": None}


def on_line(code, line):
    fn = code.co_filename
    if not fn.startswith(WATCH):
        return mon.DISABLE
    b = state["bucket"]
    if b is not None:
        b.setdefault(fn[len(ROOT):], set()).add(line)
    return mon.DISABLE


def switch(bucket):
    state["bucket"] = bucket
    mon.restart_events()


mon.use_tool_id(TOOL, "br_cases")
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE)

import tests.test_executor as te  # noqa: E402

out = {}
for test_name, attr in (("test_execute_tpch", "tpch_sqls"), ("test_execute_tpcds", "tpcds_sqls")):
    base: dict = {}
    cases: list = []
    pending: dict = {}
    case_cpu: list = []
    counter = {"k": 0}
    orig_mp = te.mp_execute
    orig_helper = te.TestExecutor.subtestHelper

    def mp(expression, meta, _orig=orig_mp):
        k = counter["k"]
        counter["k"] += 1
        bucket = cases[k] if k < len(cases) else None
        for f, ls in pending.items():  # parse_one of this query ran just before
            bucket.setdefault(f, set()).update(ls)
        pending.clear()
        t0 = time.process_time()
        switch(bucket)
        try:
            return _orig(expression, meta)
        finally:
            case_cpu[k] += time.process_time() - t0
            switch(pending)

    def helper(self, i, table, tpch=True, _orig=orig_helper):
        t0 = time.process_time()
        switch(cases[i])
        try:
            return _orig(self, i, table, tpch=tpch)
        finally:
            case_cpu[i] += time.process_time() - t0
            switch(base)

    te.mp_execute = mp
    te.TestExecutor.subtestHelper = helper
    switch(base)
    if not hasattr(te.TestExecutor, attr):
        te.TestExecutor.setUpClass()
    n = len(getattr(te.TestExecutor, attr))
    cases.extend({} for _ in range(n))
    case_cpu.extend(0.0 for _ in range(n))
    result = unittest.TestResult()
    t0 = time.process_time()
    unittest.TestSuite([te.TestExecutor(test_name)]).run(result)
    total = time.process_time() - t0
    switch(None)
    te.mp_execute = orig_mp
    te.TestExecutor.subtestHelper = orig_helper
    for f, ls in pending.items():
        base.setdefault(f, set()).update(ls)
    print(test_name, "cases", n, "errors", len(result.errors), "failures", len(result.failures),
          "cpu", round(total, 2), "case cpu", round(sum(case_cpu), 2), file=sys.stderr)
    # invert: file -> line -> [case indices]
    inv: dict = {}
    for k, c in enumerate(cases):
        for f, ls in c.items():
            for ln in ls:
                inv.setdefault(f, {}).setdefault(str(ln), []).append(k)
    out[f"tests/test_executor.py::TestExecutor::{test_name}"] = {
        "n": n,
        "base": {f: sorted(ls) for f, ls in base.items()},
        "cases": inv,
        "case_cpu": [round(x, 4) for x in case_cpu],
        "total_cpu": round(total, 3),
    }

json.dump(out, open(sys.argv[1], "w"))
