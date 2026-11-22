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
    line_a: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    lines_a: dict[str, set[int]] = defaultdict(set)
    lines_import: dict[str, set[int]] = defaultdict(set)  # module/class bodies run by lazy imports
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
            m = line_a[f]
            for ln in ls:
                m[ln] |= bit
        for f, ls in d["lines_b_body"].items():
            lines_import[f].update(ls)
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

    # classes instantiated while a test file was being collected: every test of that file
    class_users_a: dict[str, int] = defaultdict(int)
    collect_dir = os.environ.get("BR_COLLECT_DIR")
    if collect_dir and os.path.isdir(collect_dir):
        file_mask: dict[str, int] = defaultdict(int)
        for i, t in enumerate(tests):
            file_mask[t["file"]] |= 1 << i
        per_file = {}
        for fn in os.listdir(collect_dir):
            if not fn.endswith(".json"):
                continue
            f = fn[:-5].replace("__", "/")
            per_file[f] = set(json.load(open(os.path.join(collect_dir, fn))).get("classes_a", ()))
        # what `import sqlglot` alone builds is shared by everyone; keep only file-specific ones
        big = [v for v in per_file.values() if len(v) > 50]
        baseline = set.intersection(*big) if big else set()
        for f, classes_a in per_file.items():
            for c in classes_a - baseline:
                class_users_a[c] |= file_mask.get(f, 0)

    # identifier -> tests that executed (phase B, function code) a statement mentioning it
    ident_users: dict[str, int] = defaultdict(int)
    pair_users: dict[str, int] = defaultdict(int)  # "ClassName.attr" written literally
    ident_users_a: dict[str, int] = defaultdict(int)  # read while collecting (import time)
    for root in ("sqlglot", "tests"):
        for dirpath, _, filenames in os.walk(os.path.join(checkout, root)):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, checkout)
                lm = line_b.get(rel, {})
                lma = line_a.get(rel, {})
                if not lm and not lma:
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
                    mask_a = 0
                    for ln in span:
                        mask |= lm.get(ln, 0)
                        mask_a |= lma.get(ln, 0)
                    if not mask and not mask_a:
                        continue
                    names = set()
                    for sub in ast.walk(node) if not hasattr(node, "body") else _header_nodes(node):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                        elif isinstance(sub, ast.Attribute):
                            names.add(sub.attr)
                            base = sub.value
                            base_name = base.id if isinstance(base, ast.Name) else (
                                base.attr if isinstance(base, ast.Attribute) else None)
                            if base_name and base_name[:1].isupper() and mask:
                                pair_users[f"{base_name}.{sub.attr}"] |= mask
                        elif isinstance(sub, ast.alias):
                            names.add(sub.asname or sub.name.split(".")[0])
                    for name in names:
                        if mask:
                            ident_users[name] |= mask
                        if mask_a:
                            ident_users_a[name] |= mask_a

    importtime = importtime_idents(checkout, lines_a, lines_import)

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
        "line_a": {f: {str(ln): ref(m) for ln, m in sorted(lm.items())} for f, lm in line_a.items()},
        "class_users": {k: ref(v) for k, v in class_users.items()},
        "module_users": {k: ref(v) for k, v in module_users.items()},
        "module_users_b": {k: ref(v) for k, v in module_users_b.items()},
        "open_a": {k: ref(v) for k, v in open_a.items()},
        "open_b": {k: ref(v) for k, v in open_b.items()},
        "ident_users": {k: ref(v) for k, v in ident_users.items()},
        "pair_users": {k: ref(v) for k, v in pair_users.items()},
        "ident_users_a": {k: ref(v) for k, v in ident_users_a.items()},
        "class_users_a": {k: ref(v) for k, v in class_users_a.items()},
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
    extra["importtime_idents"] = sorted(importtime)
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


META_FUNCS = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__"}


def importtime_idents(checkout, lines_a, lines_import):
    """Identifiers *read* by code that runs at import time: module and class bodies,
    metaclass __new__/__init__, __init_subclass__, and any function line executed while
    collecting. Class attributes read there may feed derived state."""
    out = set()
    for root in ("sqlglot", "tests"):
        for dirpath, _, filenames in os.walk(os.path.join(checkout, root)):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, checkout)
                executed = lines_a.get(rel, set()) | lines_import.get(rel, set())
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                meta_spans = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and any(
                        isinstance(b, ast.Name) and b.id == "type" for b in node.bases
                    ):
                        meta_spans.append((node.lineno, node.end_lineno))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in META_FUNCS:
                        meta_spans.append((node.lineno, node.end_lineno))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.stmt) or isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        continue
                    span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
                    in_meta = any(a <= node.lineno <= b for a, b in meta_spans)
                    if not in_meta and not any(ln in executed for ln in span):
                        continue
                    for sub in _walk_import_time(node, in_meta):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                            out.add(sub.attr)
                        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                            out.add(sub.id)
    return out


def _walk_import_time(node, keep_all):
    """ast.walk, but not into lambda bodies: they run when called, not when defined."""
    todo = [node]
    while todo:
        cur = todo.pop()
        yield cur
        for child in ast.iter_child_nodes(cur):
            if isinstance(cur, ast.Lambda) and child is cur.body and not keep_all:
                continue
            if isinstance(child, ast.stmt):
                continue  # nested statements are visited (and checked) on their own
            todo.append(child)


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
