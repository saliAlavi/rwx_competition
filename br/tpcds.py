"""Symbolic check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

For each TPC-DS query the test parses the SQL, optimizes it (sqlglot.executor.execute
calls ensure_schema + optimize), plans and runs it in the Python executor, and compares
with duckdb running `transpile(sql, write="duckdb")`. Everything sqlglot contributes
before planning is captured by two values per query: the optimized AST and the duckdb SQL
text. If a patch only touches code the test executes in that symbolic part, and neither
value changes for any query, the test's outcome cannot change. The pristine values are
fingerprinted at build time; at run time we recompute them under the patch.
"""

from __future__ import annotations

import hashlib
import json


def _h(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=10).hexdigest()


def fingerprints() -> list[str]:
    """One fingerprint per query of tests/fixtures/optimizer/tpc-ds/tpc-ds.sql (sqlglot and
    the tests package must be importable; mirrors TestExecutor._mp_execute + execute())."""
    import tests.test_executor as te
    from sqlglot import serde
    from sqlglot import executor as ex

    sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    out = []
    for meta, sql, _ in sqls:
        try:
            expression = te.parse_one(sql)
        except Exception as error:
            out.append("EXC-parse:" + type(error).__name__)
            continue
        if not meta.get("execute"):
            out.append("noexec")
            continue
        try:
            schema = ex.ensure_schema(te.TPCDS_SCHEMA, dialect=None)
            optimized = ex.optimize(expression, schema, leave_tables_isolated=True, dialect=None)
            f1 = _h(json.dumps(serde.dump(optimized), sort_keys=True, default=str)
                    + "|" + json.dumps(schema.mapping, sort_keys=True, default=str))
        except Exception as error:
            f1 = "EXC-opt:" + type(error).__name__
        try:
            f2 = _h(te.transpile(sql, write="duckdb")[0])
        except Exception as error:
            f2 = "EXC-tr:" + type(error).__name__
        out.append(f1 + "/" + f2)
    return out


def build(out_path: str, runs: int = 3) -> None:
    """Build time: pristine fingerprints, from several processes with different hash seeds."""
    import os
    import pickle
    import subprocess
    import sys

    results = []
    for seed in range(runs):
        env = dict(os.environ, PYTHONHASHSEED=str(seed * 104729 + 3), PYTHONPATH="/opt/sqlglot:/app")
        res = subprocess.run(
            [sys.executable, "-c",
             "import json,sys; from br import runner; runner.preimport(heavy=True);"
             "from br import tpcds; json.dump(tpcds.fingerprints(), sys.stdout)"],
            env=env, capture_output=True, text=True, check=True, cwd="/opt/sqlglot")
        results.append(json.loads(res.stdout))
    base = results[0]
    unstable = sorted({i for r in results[1:] for i in range(len(base)) if r[i] != base[i]})
    with open(out_path, "wb") as f:
        pickle.dump({"fp": base, "unstable": unstable}, f)
    print(f"tpcds: {len(base)} queries, {sum(1 for x in base if x != 'noexec')} executed, "
          f"unstable {unstable}", file=sys.stderr)


if __name__ == "__main__":
    import sys

    build(sys.argv[1])
