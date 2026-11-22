"""Run in the env image: which dialect classes use which tokenizer/parser/generator classes.
    python tools/dialect_components.py > maps/components.json"""
import importlib, json, pkgutil, sys
sys.path.insert(0, "/opt/sqlglot")
import sqlglot
from sqlglot.dialects.dialect import Dialect

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass

def key(k):
    return f"{k.__module__}:{k.__qualname__}"

def subclasses(c):
    out, todo = set(), [c]
    while todo:
        x = todo.pop()
        for s in x.__subclasses__():
            if s not in out:
                out.add(s); todo.append(s)
    return out

comp = {}
for d in subclasses(Dialect) | {Dialect}:
    for attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
        c = getattr(d, attr, None)
        if not isinstance(c, type):
            continue
        for base in c.__mro__:
            if base.__module__.startswith("sqlglot"):
                comp.setdefault(key(base), set()).add(key(d))
json.dump({k: sorted(v) for k, v in comp.items()}, sys.stdout, indent=0)
