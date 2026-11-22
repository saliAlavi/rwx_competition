"""Generate sample-like patches against the pinned sqlglot checkout (offline validation only).

    python tools/mutants.py <checkout> <out_dir> <count> [seed]

Kinds, roughly like the sample: code (token flips in sqlglot/*.py, inside
functions or class bodies), fixture (token edits in tests/fixtures/**/*.sql),
comment (a comment line added to Python or SQL fixtures), multi (2-3 of these).
Patches are written in `git diff` format.
"""

from __future__ import annotations

import difflib
import io
import os
import random
import re
import sys
import tokenize

SWAPS = [
    (" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "),
    (" is not ", " is "), (" is ", " is not "), (" is None", " == None"),
    (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
    ("True", "False"), ("False", "True"), (" + 1", " - 1"), (" - 1", " + 1"),
    ("if not ", "if "), ("return True", "return False"), ("return False", "return True"),
    ("copy=False", "copy=True"), ("copy=True", "copy=False"), (" in ", " not in "),
    ("[0]", "[1]"), ("[1]", "[0]"), ("[-1]", "[0]"), ("(args, 0)", "(args, 1)"),
    ("(args, 1)", "(args, 0)"), ("(args, 2)", "(args, 1)"), (".upper()", ".lower()"),
    ("max(", "min("), ("min(", "max("), ("any(", "all("), ("all(", "any("),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Intentionally not cached",
            "# Keep in sync with the generator", "# Mirrors the behavior of the reference implementation",
            "# TODO: revisit", "# Fast path"]
SQL_EDITS = [lambda s: s.replace(" FROM ", "  ", 1), lambda s: s.replace(" AS ", " ", 1),
             lambda s: s + " 42", lambda s: s.replace("(", "( 1", 1), lambda s: s.replace(",", " ,x", 1),
             lambda s: s.replace(" = ", " <> ", 1), lambda s: s.replace("SELECT", "SELECT 0", 1),
             lambda s: s.replace(";", " 42 ;", 1)]


def py_files(root):
    out = []
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def sql_fixtures(root):
    out = []
    for d, _, fs in os.walk(os.path.join(root, "tests", "fixtures")):
        for f in fs:
            if f.endswith(".sql"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def code_lines(text):
    """Line numbers that are real code (not inside strings/comments)."""
    ok = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER) and tok.start[0] == tok.end[0]:
                ok.add(tok.start[0])
    except Exception:
        pass
    return ok


def mutate_code(rng, root):
    files = py_files(root)
    for _ in range(200):
        rel = rng.choice(files)
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = text.split("\n")
        ok = code_lines(text)
        cands = []
        for i, line in enumerate(lines, 1):
            if i not in ok or line.lstrip().startswith(("#", "import ", "from ", "def ", "class ", "@")):
                continue
            for a, b in SWAPS:
                if a in line:
                    cands.append((i, a, b))
            if re.search(r"\b\d+\b", line) and "=" in line:
                cands.append((i, "NUM", None))
        if not cands:
            continue
        i, a, b = rng.choice(cands)
        line = lines[i - 1]
        if a == "NUM":
            nums = list(re.finditer(r"\b\d+\b", line))
            m = rng.choice(nums)
            new = line[: m.start()] + str(int(m.group()) + rng.choice([1, -1, 1])) + line[m.end():]
        else:
            idxs = [m.start() for m in re.finditer(re.escape(a), line)]
            p = rng.choice(idxs)
            new = line[:p] + b + line[p + len(a):]
        if new == line:
            continue
        lines[i - 1] = new
        new_text = "\n".join(lines)
        try:
            compile(new_text, rel, "exec")
        except SyntaxError:
            continue
        return {rel: new_text}
    return None


def mutate_fixture(rng, root):
    files = sql_fixtures(root)
    for _ in range(100):
        rel = rng.choice(files)
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = text.split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
        if not idx:
            continue
        i = rng.choice(idx)
        new = rng.choice(SQL_EDITS)(lines[i])
        if new == lines[i]:
            continue
        lines[i] = new
        return {rel: "\n".join(lines)}
    return None


def add_comment(rng, root):
    if rng.random() < 0.8:
        files = py_files(root)
        rel = rng.choice(files)
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = text.split("\n")
        ok = sorted(code_lines(text))
        if not ok:
            return None
        i = rng.choice(ok)
        indent = re.match(r"\s*", lines[i - 1]).group()
        if lines[i - 1].rstrip().endswith((",", "(", "[", "{", "\\")):
            return None
        lines.insert(i, indent + rng.choice(COMMENTS))
        new_text = "\n".join(lines)
        try:
            compile(new_text, rel, "exec")
        except SyntaxError:
            return None
        return {rel: new_text}
    files = sql_fixtures(root)
    rel = rng.choice(files)
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    lines = text.split("\n")
    i = rng.randrange(len(lines))
    lines.insert(i, "-- " + rng.choice(COMMENTS)[2:])
    return {rel: "\n".join(lines)}


def diff(root, changes):
    out = []
    for rel, new in sorted(changes.items()):
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        a = old.splitlines(keepends=True)
        b = new.splitlines(keepends=True)
        if a and not a[-1].endswith("\n"):
            a[-1] += "\n\\ No newline at end of file\n"
        if b and not b[-1].endswith("\n"):
            b[-1] += "\n\\ No newline at end of file\n"
        body = "".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3))
        out.append(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body)
    return "".join(out)


def main():
    root, out_dir, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    rng = random.Random(seed)
    os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
    kinds = {}
    made = 0
    while made < count:
        r = rng.random()
        if r < 0.55:
            kind, ch = "code", mutate_code(rng, root)
        elif r < 0.75:
            kind, ch = "fixture", mutate_fixture(rng, root)
        elif r < 0.87:
            kind, ch = "comment", add_comment(rng, root)
        else:
            kind, ch = "multi", {}
            for _ in range(rng.choice([2, 3])):
                part = rng.choice([mutate_code, mutate_fixture, add_comment])(rng, root)
                if part:
                    for k, v in part.items():
                        if k not in ch:
                            ch[k] = v
            if len(ch) < 2:
                ch = None
        if not ch:
            continue
        cid = f"m{seed:02d}{made:04d}"
        open(os.path.join(out_dir, "commits", f"{cid}.patch"), "w").write(diff(root, ch))
        kinds[cid] = kind
        made += 1
    import json
    json.dump(kinds, open(os.path.join(out_dir, "kinds.json"), "w"))


if __name__ == "__main__":
    main()
