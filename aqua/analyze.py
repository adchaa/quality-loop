"""ANALYZE: turn raw probe findings into the eligible issue set.

normalize paths -> keep scope -> drop confirmed false positives -> dedupe -> stable id.
Every finding already carries its ISO/IEC 25010:2023 characteristic from probes.py.
"""
import hashlib
import json
from pathlib import PurePosixPath


def finding_id(f):
    key = {k: f.get(k) for k in ("probe", "path", "rule", "function", "message")}
    if f["probe"] != "lizard":
        key["line"] = f.get("line")
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def in_scope(path, scope):
    pure = PurePosixPath(path)
    component = scope["component"].rstrip("/") + "/"
    if pure.is_absolute() or ".." in pure.parts or not path.startswith(component):
        return False
    if any(path.startswith(prefix) for prefix in scope.get("exclude", [])):
        return False
    return pure.suffix in scope.get("extensions", [".js", ".jsx", ".mjs"])


def analyze(findings, scope, knowledge):
    seen, out, dropped = set(), [], {"out_of_scope": 0, "false_positive": 0, "duplicate": 0, "parse_error": 0}
    for f in findings:
        if not in_scope(f["path"], scope):
            dropped["out_of_scope"] += 1
            continue
        if f.get("fatal"):
            dropped["parse_error"] += 1   # a broken file is a blocker for a human, not a lint fix
            continue
        if knowledge.is_false_positive(f):
            dropped["false_positive"] += 1
            continue
        f = dict(f, id=finding_id(f))
        if f["id"] in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(f["id"])
        out.append(f)
    return out, dropped


def characteristic_counts(findings):
    counts = {}
    for f in findings:
        counts[f["iso_characteristic"]] = counts.get(f["iso_characteristic"], 0) + 1
    return counts
