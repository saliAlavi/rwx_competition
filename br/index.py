"""Load the committed maps (maps/maps.json.gz) as Python-int bitmasks over tests."""

from __future__ import annotations

import gzip
import json
import pickle


class Index:
    tests: list[str]
    line_b: dict
    lines_a: dict
    class_users: dict
    module_users: dict
    module_users_b: dict
    open_a: dict
    open_b: dict
    ident_users: dict
    extra: dict

    def all_mask(self) -> int:
        return (1 << len(self.tests)) - 1


def _from_json(path: str) -> Index:
    with gzip.open(path, "rt") as f:
        raw = json.load(f)
    sets = [int(h, 16) for h in raw["sets"]]
    idx = Index()
    idx.tests = raw["tests"]
    idx.line_b = {f: {int(ln): sets[r] for ln, r in lm.items()} for f, lm in raw["line_b"].items()}
    idx.lines_a = {f: frozenset(v) for f, v in raw["lines_a"].items()}
    idx.line_a = {f: {int(ln): sets[r] for ln, r in lm.items()} for f, lm in raw.get("line_a", {}).items()}
    for key in ("class_users", "module_users", "module_users_b", "open_a", "open_b", "ident_users",
                "pair_users", "ident_users_a", "class_users_a"):
        setattr(idx, key, {k: sets[r] for k, r in raw.get(key, {}).items()})
    idx.extra = raw.get("extra", {})
    return idx


def prepare(maps_dir: str, out_path: str) -> None:
    idx = _from_json(f"{maps_dir}/maps.json.gz")
    with open(out_path, "wb") as f:
        pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)


def load(pkl_path: str, json_path: str) -> Index:
    try:
        with open(pkl_path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return _from_json(json_path)
