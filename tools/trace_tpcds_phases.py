"""Lines test_execute_tpcds runs OUTSIDE its symbolic part (parse_one / ensure_schema /
optimize / transpile). Offline, in the env image: python tools/trace_tpcds_phases.py out.json"""
import json, os, sys, unittest
sys.path.insert(0, "/app"); sys.path.insert(0, "/opt/sqlglot"); os.chdir("/opt/sqlglot")
from br import runner
runner.preimport()
ROOT = "/opt/sqlglot/"; WATCH = (ROOT + "sqlglot/", ROOT + "tests/")
mon = sys.monitoring; TOOL = 4
state = {"bucket": None}
def on_line(code, line):
    fn = code.co_filename
    if not fn.startswith(WATCH):
        return mon.DISABLE
    b = state["bucket"]
    if b is not None:
        b.setdefault(fn[len(ROOT):], set()).add(line)
    return mon.DISABLE
def switch(b):
    state["bucket"] = b; mon.restart_events()
mon.use_tool_id(TOOL, "br_ph"); mon.register_callback(TOOL, mon.events.LINE, on_line); mon.set_events(TOOL, mon.events.LINE)
import tests.test_executor as te
import sqlglot.executor as ex
S, NS = {}, {}
def wrap(fn):
    def inner(*a, **k):
        prev = state["bucket"]; switch(S)
        try:
            return fn(*a, **k)
        finally:
            switch(prev)
    return inner
te.parse_one = wrap(te.parse_one); te.transpile = wrap(te.transpile)
ex.optimize = wrap(ex.optimize); ex.ensure_schema = wrap(ex.ensure_schema)
switch(NS)
res = unittest.TestResult()
unittest.TestSuite([te.TestExecutor("test_execute_tpcds")]).run(res)
switch(None)
print("errors", len(res.errors), "failures", len(res.failures), "S lines", sum(map(len, S.values())), "NS lines", sum(map(len, NS.values())), file=sys.stderr)
json.dump({"ns": {f: sorted(v) for f, v in NS.items()}, "s": {f: sorted(v) for f, v in S.items()}}, open(sys.argv[1], "w"))
