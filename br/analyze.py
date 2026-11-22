"""Decide which tests a patch could break (a superset), from the committed maps.

For each changed file:
  * Python file, comment/whitespace-only (same token stream): only tests that read
    the file as data can notice.
  * Python file, edit inside a function or lambda body: tests that executed the
    touched statement (or, for an insertion, the statements around it).
  * Python file, edit to a `def` header / decorator: tests that ran that function.
  * Python file, edit to a class body (attributes, dict entries...): tests that
    instantiated the class or a subclass, plus tests that ran code naming the
    changed attribute while the module was loaded.
  * Python file, other module-level edit: tests that ran code naming the
    assigned names, or (imports, calls...) every test that loaded the module.
  * Python file that no longer parses: every test that loads the module.
  * Other files (fixtures...): tests that opened them; tests that listed the
    directory when a file is added or removed.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize
from dataclasses import dataclass, field

from br.index import Index
from br.patch import FilePatch

PYTEST_CONFIG = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py"}


@dataclass
class Verdict:
    mask: int = 0
    reasons: list = field(default_factory=list)
    global_change: bool = False
    fixture_only: bool = True

    def add(self, mask: int, reason: str) -> None:
        self.mask |= mask
        if mask:
            self.reasons.append(reason)


def module_name(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


# --------------------------------------------------------------------------- tokens


def _tokens(src: str):
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        out.append((tok.type, tok.string))
    return out


_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def comment_only(old: str, new: str) -> bool:
    try:
        if _tokens(old) != _tokens(new):
            return False
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return False
    old_head = [l for l in old.split("\n")[:2] if _CODING.match(l)]
    new_head = [l for l in new.split("\n")[:2] if _CODING.match(l)]
    return old_head == new_head


# --------------------------------------------------------------------------- AST helpers


class Scopes:
    """Per-line lookup of the innermost statement and its enclosing scopes."""

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.stmts: list[tuple[int, int, ast.stmt, tuple]] = []  # (start, end, node, chain)
        self.lambdas: list[tuple[ast.Lambda, tuple]] = []
        self._walk(tree, ())

    def _walk(self, node, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                start = min([child.lineno] + [d.lineno for d in getattr(child, "decorator_list", [])])
                body = getattr(child, "body", None)
                if isinstance(body, list) and body and not isinstance(child, ast.Module):
                    end = body[0].lineno - 1
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        # docstring-less one-liners: header ends where the body starts
                        end = max(child.lineno, end)
                else:
                    end = child.end_lineno or child.lineno
                self.stmts.append((start, max(start, end), child, chain))
                self._walk(child, chain + (child,))
            else:
                if isinstance(child, ast.Lambda):
                    self.lambdas.append((child, chain))
                self._walk(child, chain)

    def stmt_at(self, line: int):
        best = None
        for start, end, node, chain in self.stmts:
            if start <= line <= end:
                if best is None or (end - start) <= (best[1] - best[0]):
                    best = (start, end, node, chain)
        return best

    def lambda_at(self, line: int, col_lo: int | None = None, col_hi: int | None = None):
        """Innermost lambda whose *body* contains the line (and column range if given)."""
        best = None
        for lam, chain in self.lambdas:
            body = lam.body
            if not (body.lineno <= line <= (body.end_lineno or body.lineno)):
                continue
            if col_lo is not None:
                lo = body.col_offset if body.lineno == line else 0
                hi = body.end_col_offset if body.end_lineno == line else 10**9
                if not (lo <= col_lo and col_hi <= hi):
                    continue
            span = (body.end_lineno or body.lineno) - body.lineno
            if best is None or span < best[0]:
                best = (span, lam, chain)
        return best


def _func_in_chain(chain: tuple):
    for node in reversed(chain):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _class_path(chain: tuple) -> list[str]:
    names = []
    for node in chain:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return names


def _assigned_names(node: ast.stmt) -> set[str]:
    names: set[str] = set()
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return names


def _changed_cols(old_line: str, new_line: str) -> tuple[int, int]:
    """Column range of old_line that differs from new_line (common prefix/suffix stripped)."""
    p = 0
    while p < len(old_line) and p < len(new_line) and old_line[p] == new_line[p]:
        p += 1
    s = 0
    while (
        s < len(old_line) - p
        and s < len(new_line) - p
        and old_line[len(old_line) - 1 - s] == new_line[len(new_line) - 1 - s]
    ):
        s += 1
    return p, max(p, len(old_line) - s)


# --------------------------------------------------------------------------- analysis


class Analyzer:
    def __init__(self, idx: Index, src_root: str):
        self.idx = idx
        self.src = src_root
        self.all = idx.all_mask()
        self._test_ids = idx.tests
        self._file_mask_cache: dict[str, int] = {}

    # ---- masks
    def lines_mask(self, path: str, lines) -> int:
        lm = self.idx.line_b.get(path)
        if not lm:
            return 0
        mask = 0
        for ln in lines:
            mask |= lm.get(ln, 0)
        return mask

    def module_users(self, mod: str | None) -> int:
        if not mod:
            return 0
        mask = self.idx.module_users.get(mod, 0)
        if not mask:
            # never loaded under that name: maybe a submodule of a loaded package
            for name, m in self.idx.module_users.items():
                if name.startswith(mod + "."):
                    mask |= m
        return mask

    def tests_in_file(self, path: str) -> int:
        if path not in self._file_mask_cache:
            mask = 0
            prefix = path + "::"
            for i, tid in enumerate(self._test_ids):
                if tid.startswith(prefix):
                    mask |= 1 << i
            self._file_mask_cache[path] = mask
        return self._file_mask_cache[path]

    def tests_in_class(self, path: str, cls: str) -> int:
        mask = 0
        prefix = f"{path}::{cls}::"
        for i, tid in enumerate(self._test_ids):
            if tid.startswith(prefix):
                mask |= 1 << i
        return mask

    def class_users(self, mod: str, qual: str) -> int:
        return self.idx.class_users.get(f"{mod}:{qual}", 0)

    def ident_users(self, names) -> int:
        mask = 0
        for name in names:
            mask |= self.idx.ident_users.get(name, 0)
        return mask

    def data_readers(self, path: str) -> int:
        """Tests that opened the file during the test other than by lazily importing it."""
        mask = self.idx.open_b.get(path, 0)
        mod = module_name(path)
        if mod and path.endswith(".py"):
            lazy = self.idx.module_users_b.get(mod, 0)
            src_readers = int(self.idx.extra.get("source_readers", "0"), 16)
            mask = (mask & ~lazy) | (mask & src_readers)
        return mask

    # ---- per file
    def analyze(self, fp: FilePatch, old: bytes | None, new: bytes | None, v: Verdict) -> None:
        path = fp.path
        old_path = fp.old_path
        new_path = fp.new_path
        base = os.path.basename(path)

        if base in PYTEST_CONFIG or (new_path and os.path.basename(new_path) in PYTEST_CONFIG):
            if base == "conftest.py" or self._pytest_config_touched(fp, old, new):
                v.global_change = True
                v.add(self.all, f"pytest-config:{path}")
                v.fixture_only = False
                return

        # renames/deletions/additions: whoever looks at either path or lists the directory
        if old_path != new_path:
            for p in filter(None, (old_path, new_path)):
                v.add(self.idx.open_b.get(p, 0), f"open:{p}")
                v.add(self.idx.open_a.get(p, 0), f"openA:{p}")
                v.add(self.idx.open_b.get(os.path.dirname(p), 0), f"listdir:{p}")
                if p.endswith(".py"):
                    mod = module_name(p)
                    v.add(self.module_users(mod), f"module:{mod}")
                    v.fixture_only = False
                    if os.path.basename(p) == "__init__.py":
                        pkg_dir = os.path.dirname(p)
                        v.add(self.module_users(module_name(pkg_dir + "/__init__.py")), f"pkg:{pkg_dir}")
            if old_path is None or new_path is None:
                return
            # renamed with edits: fall through to analyze content changes on the new path

        if path.endswith(".py"):
            v.fixture_only = False
            self._analyze_py(fp, old, new, v)
        else:
            v.add(self.idx.open_b.get(path, 0), f"open:{path}")
            # read while the test module was imported: every test of those files
            if path in self.idx.open_a:
                v.add(self.idx.open_a[path], f"openA:{path}")
            if fp.mode_change and not fp.hunks:
                return

    def _pytest_config_touched(self, fp: FilePatch, old, new) -> bool:
        text = (new or b"").decode("utf-8", "replace")
        changed = "\n".join(l for h in fp.hunks for l in h.lines if l[:1] in "+-")
        return "pytest" in text.lower() or "pytest" in changed.lower() or fp.path.endswith("pytest.ini")

    def _analyze_py(self, fp: FilePatch, old_b: bytes | None, new_b: bytes | None, v: Verdict) -> None:
        path = fp.path
        mod = module_name(path)
        is_test_file = path.startswith("tests/")
        readers = self.data_readers(path)
        v.add(readers, f"reads:{path}")

        if old_b is None or new_b is None:
            v.add(self.module_users(mod), f"module:{mod}")
            return
        old = old_b.decode("utf-8", "surrogateescape")
        new = new_b.decode("utf-8", "surrogateescape")
        if comment_only(old, new):
            v.reasons.append(f"comment-only:{path}")
            return
        try:
            old_tree = ast.parse(old)
        except SyntaxError:
            v.add(self.module_users(mod), f"module(old-unparsable):{mod}")
            return
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            # the module no longer imports: everything that loads it breaks
            v.add(self.module_users(mod), f"syntax-error:{mod}")
            if is_test_file:
                v.add(self.tests_in_file(path), f"file:{path}")
            return

        old_scopes = Scopes(old_tree)
        new_scopes = Scopes(new_tree)
        old_lines = old.split("\n")
        new_lines = new.split("\n")

        # Walk hunks: pair removed/added lines to get column ranges for 1:1 edits.
        for hunk in fp.hunks:
            o = hunk.old_start - 1 if hunk.old_len else hunk.old_start
            n = hunk.new_start - 1 if hunk.new_len else hunk.new_start
            removed: list[int] = []
            added: list[int] = []

            def flush():
                if not removed and not added:
                    return
                self._region(path, mod, is_test_file, old_scopes, new_scopes, old_lines, new_lines,
                             list(removed), list(added), o_before, v)
                removed.clear()
                added.clear()

            o_before = o
            for line in hunk.lines:
                tag = line[:1]
                if tag == " ":
                    flush()
                    o += 1
                    n += 1
                    o_before = o
                elif tag == "-":
                    o += 1
                    removed.append(o)
                elif tag == "+":
                    n += 1
                    added.append(n)
            flush()

    def _region(self, path, mod, is_test_file, old_scopes: Scopes, new_scopes: Scopes,
                old_lines, new_lines, removed: list[int], added: list[int], o_before: int,
                v: Verdict) -> None:
        """One contiguous block of removed (old numbering) and added (new numbering) lines."""
        targets = []  # (line, scopes, side, col_lo, col_hi)
        if removed and added and len(removed) == len(added):
            for ro, an in zip(removed, added):
                lo, hi = _changed_cols(old_lines[ro - 1], new_lines[an - 1])
                targets.append((ro, "old", lo, hi))
        else:
            for ro in removed:
                targets.append((ro, "old", None, None))
            for an in added:
                targets.append((an, "new", None, None))

        for line, side, lo, hi in targets:
            scopes = old_scopes if side == "old" else new_scopes
            text = (old_lines if side == "old" else new_lines)[line - 1]
            stripped = text.strip()
            found = scopes.stmt_at(line)
            if found is None:
                if not stripped or stripped.startswith("#"):
                    continue  # blank/comment line between statements
                # e.g. a decorator-less continuation we failed to place: be safe
                v.add(self.module_users(mod), f"unplaced:{path}:{line}")
                continue
            start, end, node, chain = found
            self._classify(path, mod, is_test_file, side, line, lo, hi, start, end, node, chain,
                           scopes, old_scopes, o_before, v)

    def _neighbor_mask(self, path: str, old_scopes: Scopes, line_old_before: int, func) -> int:
        """Tests that executed old statements adjacent to an insertion point inside func."""
        lm = self.idx.line_b.get(path, {})
        lo_bound = func.lineno
        hi_bound = func.end_lineno or func.lineno
        mask = 0
        # previous executed line at or before the insertion point
        ln = line_old_before
        while ln >= lo_bound:
            if ln in lm:
                mask |= lm[ln]
                break
            ln -= 1
        ln = line_old_before + 1
        while ln <= hi_bound:
            if ln in lm:
                mask |= lm[ln]
                break
            ln += 1
        if not mask:
            mask = self.lines_mask(path, range(lo_bound, hi_bound + 1))
        return mask

    def _old_func_by_path(self, old_scopes: Scopes, names: list[str]):
        """Find the def/class in the old tree with the same nesting path."""
        node = old_scopes.tree
        for name in names:
            nxt = None
            for child in ast.walk(node) if node is old_scopes.tree else ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == name:
                    nxt = child
                    break
            if nxt is None:
                return None
            node = nxt
        return node

    def _classify(self, path, mod, is_test_file, side, line, lo, hi, start, end, node, chain,
                  scopes, old_scopes, o_before, v: Verdict) -> None:
        func = _func_in_chain(chain)
        # a def header (signature, decorators): behaves like an edit inside that function
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_node = node
            if side == "old":
                mask = self.lines_mask(path, range(func_node.lineno, (func_node.end_lineno or func_node.lineno) + 1))
            else:
                old_f = self._old_func_by_path(old_scopes, _class_path(chain) + [func_node.name])
                mask = (self.lines_mask(path, range(old_f.lineno, (old_f.end_lineno or old_f.lineno) + 1))
                        if old_f is not None else 0)
                if old_f is None:
                    # a brand-new def: at class level it may override a base method
                    self._class_level(path, mod, is_test_file, chain, {func_node.name}, v, f"newdef:{path}:{line}")
            v.add(mask, f"def:{path}:{func_node.name}")
            if func is None and not mask:
                # never ran in a test: a new override or decorator change may still matter
                self._class_level(path, mod, is_test_file, chain, {func_node.name}, v, f"defhdr:{path}:{line}")
            return

        if func is not None:
            if side == "old":
                v.add(self.lines_mask(path, range(start, end + 1)), f"stmt:{path}:{line}")
                lam = scopes.lambda_at(line, lo, hi) if lo is not None else scopes.lambda_at(line)
                if lam is not None:
                    _, lam_node, _ = lam
                    v.add(self.lines_mask(path, range(lam_node.lineno, (lam_node.end_lineno or lam_node.lineno) + 1)),
                          f"lambda:{path}:{line}")
            else:
                old_f = self._old_func_by_path(old_scopes, _class_path(chain[: chain.index(func) + 1]))
                if old_f is None:
                    v.add(self.module_users(mod), f"newfunc:{path}:{line}")
                    return
                v.add(self._neighbor_mask(path, old_scopes, o_before, old_f), f"insert:{path}:{line}")
            if self.idx.lines_a.get(path) and func.lineno in self.idx.lines_a.get(path, ()):  # pragma: no cover
                pass
            # the function also ran at import time (e.g. a factory building class tables)
            body_lines = range(func.lineno + 1, (func.end_lineno or func.lineno) + 1)
            a_lines = self.idx.lines_a.get(path, ())
            if side == "old" and any(ln in a_lines for ln in range(start, end + 1)):
                v.add(self.lines_mask(path, body_lines), f"importtime-func:{path}:{func.name}")
                self._class_level(path, mod, is_test_file, chain[: chain.index(func)], {func.name}, v,
                                  f"importtime-func-users:{path}:{func.name}")
            return

        # class body or module level, but maybe only inside a lambda's body
        if lo is not None:
            lam = scopes.lambda_at(line, lo, hi)
            if lam is not None:
                _, lam_node, _ = lam
                mask = self.lines_mask(path, range(lam_node.lineno, (lam_node.end_lineno or lam_node.lineno) + 1))
                v.add(mask, f"lambda:{path}:{line}")
                return

        names = _assigned_names(node)
        if any(isinstance(c, ast.ClassDef) for c in chain) or isinstance(node, ast.ClassDef):
            self._class_level(path, mod, is_test_file, chain + ((node,) if isinstance(node, ast.ClassDef) else ()),
                              names, v, f"class:{path}:{line}")
            return

        # module level
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and names:
            v.add(self.ident_users(names), f"names:{','.join(sorted(names))}")
            v.add(self._derived_users(names), f"derived:{','.join(sorted(names))}")
            if is_test_file:
                v.add(self.tests_in_file(path), f"testmodule:{path}")
            return
        v.add(self.module_users(mod), f"module-stmt:{path}:{line}")
        if is_test_file:
            v.add(self.tests_in_file(path), f"testmodule:{path}")

    def _derived_users(self, names) -> int:
        derived = self.idx.extra.get("derived", {})
        mask = 0
        for name in names:
            for cls_key in derived.get(name, ()):
                mask |= self.idx.class_users.get(cls_key, 0)
        return mask

    def _class_level(self, path, mod, is_test_file, chain, names, v: Verdict, reason: str) -> None:
        classes = [c for c in chain if isinstance(c, ast.ClassDef)]
        if not classes:
            # module-level def/assignment
            v.add(self.ident_users(names), reason + ":names")
            v.add(self._derived_users(names), reason + ":derived")
            return
        mask = 0
        components = self.idx.extra.get("components", {})
        for depth in range(len(classes), 0, -1):  # the class and every class it is nested in
            qual = ".".join(_class_path(chain[: chain.index(classes[depth - 1]) + 1]))
            key = f"{mod}:{qual}"
            m = self.class_users(mod, qual)
            # a dialect's tokenizer/parser/generator class is read through the dialect
            # (e.g. dialect.tokenizer_class.NUMERIC_ESCAPES) without being instantiated
            for dialect_key in components.get(key, ()):
                m |= self.idx.class_users.get(dialect_key, 0)
            v.add(m, reason + f":users:{qual}")
            mask |= m
        if names:
            # static access as `ClassName.attr` from code that never instantiates it
            cls_name = classes[-1].name
            named = self.ident_users([cls_name])
            if bin(named).count("1") < len(self.idx.tests) // 4:
                v.add(named & self.ident_users(names) & self.module_users(mod), reason + ":static")
            v.add(self._derived_users(names), reason + ":derived")
        if is_test_file:
            v.add(self.tests_in_class(path, classes[0].name), reason + ":testclass")
        if not mask:
            # never instantiated in any test: fall back to everyone who loads the module
            v.add(self.module_users(mod), reason + ":module-fallback")
