"""Fingerprints of sqlglot's import-time state (class attributes and module globals).

A patch whose edited code runs while sqlglot is imported (class bodies, metaclasses,
templates parsed at import, module constants) can change derived state anywhere. Rather
than assuming every test that imports the module is affected, we import the patched
sqlglot, fingerprint every class attribute and module global, and compare with the
pristine fingerprints computed at build time. Only the attributes that actually differ
need their users tested.

Fingerprints ignore line numbers, so moving code (e.g. adding a comment above it) does
not count as a change. Dict attributes are fingerprinted per key so a changed entry of a
large table (TRANSFORMS, KEYWORDS...) is reported with its keys.
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import pkgutil
import sys
import types

_MAX_DEPTH = 12


def _code_fp(code: types.CodeType, depth: int) -> str:
    consts = []
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            consts.append(_code_fp(c, depth + 1) if depth < _MAX_DEPTH else c.co_name)
        else:
            consts.append(repr(c))
    return f"code:{code.co_name}:{code.co_code.hex()}:{code.co_names}:{consts}"


def fp(value, depth: int = 0, seen=None) -> str:
    """A stable text fingerprint (hashed by the caller)."""
    if depth > _MAX_DEPTH:
        return "<deep>"
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (bool, int, float, complex, str, bytes)):
        return f"{type(value).__name__}:{value!r}"
    if isinstance(value, enum.Enum):
        return f"enum:{type(value).__qualname__}.{value.name}"
    if isinstance(value, type):
        return f"type:{getattr(value, '__module__', '')}:{value.__qualname__}"
    if isinstance(value, (types.FunctionType, types.LambdaType)):
        parts = [_code_fp(value.__code__, depth)]
        if value.__defaults__:
            parts.append(fp(value.__defaults__, depth + 1, seen))
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    parts.append(fp(cell.cell_contents, depth + 1, seen))
                except ValueError:
                    parts.append("<empty cell>")
        return "fn:" + "|".join(parts)
    if isinstance(value, (types.BuiltinFunctionType, types.MethodType, types.ModuleType)):
        return f"{type(value).__name__}:{getattr(value, '__qualname__', getattr(value, '__name__', ''))}"
    if isinstance(value, (staticmethod, classmethod)):
        return f"{type(value).__name__}:" + fp(value.__func__, depth + 1, seen)
    if isinstance(value, property):
        return "property:" + "|".join(fp(f, depth + 1, seen) for f in (value.fget, value.fset) if f)
    oid = id(value)
    if oid in seen:
        return "<cycle>"
    seen = seen | {oid}
    if isinstance(value, (list, tuple)):
        # order-insensitive: many tuples are built from sets, whose order varies per process
        return f"{type(value).__name__}[" + ",".join(sorted(fp(v, depth + 1, seen) for v in value)) + "]"
    if isinstance(value, (set, frozenset)):
        return f"{type(value).__name__}{{" + ",".join(sorted(fp(v, depth + 1, seen) for v in value)) + "}"
    if isinstance(value, dict):
        items = sorted((fp(k, depth + 1, seen), fp(v, depth + 1, seen)) for k, v in value.items())
        return "dict{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    # sqlglot expressions (templates parsed at import) and other objects
    mod = type(value).__module__ or ""
    if mod.startswith("sqlglot"):
        try:
            return f"obj:{type(value).__qualname__}:{value!r}"
        except Exception:
            pass
        try:
            return f"obj:{type(value).__qualname__}:" + fp(vars(value), depth + 1, seen)
        except TypeError:
            return f"obj:{type(value).__qualname__}"
    return f"obj:{type(value).__module__}.{type(value).__qualname__}"


def _h(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=10).hexdigest()


def import_all() -> list[str]:
    import sqlglot

    names = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        names.append(info.name)
    for name in sorted(names):
        try:
            importlib.import_module(name)
        except Exception:
            pass
    return sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))


def snapshot() -> dict:
    """{key: hash} where key is 'mod:Qual.Name.attr' or 'mod:attr' (module global),
    plus 'mod:Qual.Name.attr[keyfp]' entries for dict attributes."""
    out: dict[str, str] = {}
    mods = import_all()
    seen_classes = set()

    def visit_class(mod_name: str, cls: type, qual: str) -> None:
        if id(cls) in seen_classes:
            return
        seen_classes.add(id(cls))
        for attr, value in list(vars(cls).items()):
            if attr.startswith("__") and attr.endswith("__") and attr not in ("__slots__",):
                continue
            if isinstance(value, (staticmethod, classmethod, property)):
                continue  # methods: edits there are covered by line coverage
            if isinstance(value, types.FunctionType) and value.__closure__ is None and \
                    value.__qualname__ == f"{cls.__qualname__}.{value.__name__}":
                continue  # a method defined right here: covered by line coverage
            if isinstance(value, type):
                if value.__qualname__.startswith(cls.__qualname__ + ".") and value.__module__ == cls.__module__:
                    visit_class(mod_name, value, value.__qualname__)
                continue
            key = f"{mod_name}:{qual}.{attr}"
            try:
                if isinstance(value, dict) and len(value) > 8:
                    for k, v in value.items():
                        out[f"{key}[{_h(fp(k))}]"] = _h(fp(v))
                    out[key + "[]"] = _h(",".join(sorted(fp(k) for k in value)))
                else:
                    out[key] = _h(fp(value))
            except Exception as error:  # pragma: no cover
                out[key] = f"err:{type(error).__name__}"

    for mod_name in mods:
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        for name, value in list(vars(module).items()):
            if name.startswith("__"):
                continue
            if isinstance(value, type):
                if getattr(value, "__module__", None) == mod_name:
                    visit_class(mod_name, value, value.__qualname__)
                continue
            if isinstance(value, (types.ModuleType, types.BuiltinFunctionType)):
                continue
            if isinstance(value, types.FunctionType) and value.__closure__ is None and \
                    value.__name__ != "<lambda>":
                continue  # a plain def: covered by line coverage
            try:
                out[f"{mod_name}:{name}"] = _h(fp(value))
            except Exception as error:  # pragma: no cover
                out[f"{mod_name}:{name}"] = f"err:{type(error).__name__}"
    return out


def diff(pristine: dict, current: dict) -> list[str]:
    keys = set(pristine) | set(current)
    return sorted(k for k in keys if pristine.get(k) != current.get(k))


def build(out_path: str, runs: int = 4) -> None:
    """Build time: snapshot the pristine checkout in several fresh processes (different hash
    seeds) and keep only the keys whose fingerprint never varies."""
    import json
    import os
    import pickle
    import subprocess

    snaps = []
    for seed in range(runs):
        env = dict(os.environ, PYTHONHASHSEED=str(seed * 7919 + 1))
        # same pre-imports as the runtime child: some globals record what is already loaded
        res = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0, '/app');"
                              "from br import runner; runner.preimport(heavy=True);"
                              "from br import state; json.dump(state.snapshot(), sys.stdout)"],
                             env=env, capture_output=True, text=True, check=True)
        snaps.append(json.loads(res.stdout))
    base = snaps[0]
    unstable = {k for s in snaps[1:] for k in set(base) | set(s) if base.get(k) != s.get(k)}
    stable = {k: v for k, v in base.items() if k not in unstable}
    with open(out_path, "wb") as f:
        pickle.dump({"stable": stable, "unstable": sorted(unstable)}, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"state: {len(stable)} stable entries, {len(unstable)} unstable: {sorted(unstable)[:10]}",
          file=sys.stderr)


def changed_keys(pristine: dict, current: dict) -> list[str]:
    stable = pristine["stable"]
    unstable = set(pristine["unstable"])
    keys = (set(stable) | set(current)) - unstable
    return sorted(k for k in keys if stable.get(k) != current.get(k))


if __name__ == "__main__":  # build time: python -m br.state <out.pkl> (sqlglot on sys.path)
    build(sys.argv[1])
