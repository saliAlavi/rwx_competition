"""pytest plugin: write failing node ids (incl. failed subtests) and broken files to $BR_GT_OUT."""
import json, os
_failed, _broken = set(), set()

def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid.split(" ")[0].split("[")[0])

def pytest_collectreport(report):
    if report.failed:
        _broken.add(report.nodeid)

def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_GT_OUT")
    if out:
        json.dump({"failed": sorted(_failed), "broken": sorted(_broken), "exit": int(exitstatus)}, open(out, "w"))
