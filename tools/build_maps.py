"""Invert per-test traces into the maps committed under maps/.

    python tools/build_maps.py <tests.json> <trace_dir> <sqlglot_checkout> <out_dir>

Everything is built from the pinned sqlglot commit only (coverage, imports,
classes used, files read). Test sets are bitmasks over the test ids listed in
maps/tests.json, written as hex strings and deduplicated.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import sys
from collections import defaultdict


def main() -> None:
    tests_json, trace_dir, checkout, out_dir = sys.argv[1:5]
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    n = len(ids)

    line_b: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    lines_a: dict[str, set[int]] = defaultdict(set)
    class_users: dict[str, int] = defaultdict(int)
    module_users: dict[str, int] = defaultdict(int)
    module_users_b: dict[str, int] = defaultdict(int)  # loaded only during the test
    open_a: dict[str, int] = defaultdict(int)
    open_b: dict[str, int] = defaultdict(int)
    missing = []
    for i, t in enumerate(tests):
        path = os.path.join(trace_dir, f"{t['number']}.json")
        try:
            d = json.load(open(path))
        except Exception:
            missing.append(t["id"])
            continue
        if d["exitstatus"] != 0:
            missing.append(t["id"])
        bit = 1 << i
        for f, ls in d["lines_b_func"].items():
            m = line_b[f]
            for ln in ls:
                m[ln] |= bit
        for f, ls in d["lines_a"].items():
            lines_a[f].update(ls)
        for c in d["classes"]:
            class_users[c] |= bit
        ma = set(d["modules_a"])
        for mod in set(d["modules_b"]) | ma:
            module_users[mod] |= bit
        for mod in set(d["modules_b"]) - ma:
            module_users_b[mod] |= bit
        for p in d["files_a"]:
            open_a[p] |= bit
        for p in d["files_b"]:
            open_b[p] |= bit

    # identifier -> tests that executed (phase B, function code) a statement mentioning it
    ident_users: dict[str, int] = defaultdict(int)
    for root in ("sqlglot", "tests"):
        for dirpath, _, filenames in os.walk(os.path.join(checkout, root)):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, checkout)
                lm = line_b.get(rel)
                if not lm:
                    continue
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.stmt) or isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        continue
                    # simple statements only: compound ones are covered via their children
                    if hasattr(node, "body") and isinstance(getattr(node, "body"), list):
                        span = range(node.lineno, node.body[0].lineno)
                    else:
                        span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
                    mask = 0
                    for ln in span:
                        mask |= lm.get(ln, 0)
                    if not mask:
                        continue
                    names = set()
                    for sub in ast.walk(node) if not hasattr(node, "body") else _header_nodes(node):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                        elif isinstance(sub, ast.Attribute):
                            names.add(sub.attr)
                        elif isinstance(sub, ast.alias):
                            names.add(sub.asname or sub.name.split(".")[0])
                    for name in names:
                        ident_users[name] |= mask

    sets: dict[int, int] = {}

    def ref(mask: int) -> int:
        if mask not in sets:
            sets[mask] = len(sets)
        return sets[mask]

    out = {
        "tests": ids,
        "missing": missing,
        "line_b": {f: {str(ln): ref(m) for ln, m in sorted(lm.items())} for f, lm in line_b.items()},
        "lines_a": {f: sorted(v) for f, v in lines_a.items()},
        "class_users": {k: ref(v) for k, v in class_users.items()},
        "module_users": {k: ref(v) for k, v in module_users.items()},
        "module_users_b": {k: ref(v) for k, v in module_users_b.items()},
        "open_a": {k: ref(v) for k, v in open_a.items()},
        "open_b": {k: ref(v) for k, v in open_b.items()},
        "ident_users": {k: ref(v) for k, v in ident_users.items()},
    }
    extra = {}
    comp_path = os.path.join(out_dir, "components.json")
    if os.path.exists(comp_path):
        extra["components"] = json.load(open(comp_path))
    src_readers = 0
    for i, tid in enumerate(ids):
        if tid.startswith("tests/test_typing.py::"):
            src_readers |= 1 << i
    extra["source_readers"] = format(src_readers, "x")
    out["extra"] = extra
    out["sets"] = [format(m, "x") for m, _ in sorted(sets.items(), key=lambda kv: kv[1])]
    os.makedirs(out_dir, exist_ok=True)
    with gzip.open(os.path.join(out_dir, "maps.json.gz"), "wt", compresslevel=9) as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"tests={n} missing={len(missing)} files={len(line_b)} sets={len(sets)} "
        f"classes={len(class_users)} idents={len(ident_users)}",
        file=sys.stderr,
    )


def _header_nodes(node):
    """Nodes in a compound statement's header (test/iter/items), not its body."""
    for field in ("test", "iter", "target", "items", "subject", "handlers"):
        value = getattr(node, field, None)
        if value is None:
            continue
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, ast.AST) and not isinstance(v, ast.ExceptHandler):
                yield from ast.walk(v)
            elif isinstance(v, ast.ExceptHandler) and v.type is not None:
                yield from ast.walk(v.type)


if __name__ == "__main__":
    main()
