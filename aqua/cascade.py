"""VALIDATE: the assurance cascade (Assured LLM-based SE, Alshahwan et al. 2024).

A candidate is accepted only if every filter passes, cheapest first:
  F1 edits apply, stay in scope and size, and the file still parses
  F2 the target finding is gone (same probe, before vs after)
  F3 Pareto non-regression: no measure of q gets worse; the target characteristic improves
  F4 tests pass (flaky-aware re-run), or "not_run" when disabled
  F5 anti-gaming guards: no suppression comments, removed exports or gutted code
Only F1-F5 survivors reach the reviewer agent (session.py), and only reviewed
survivors can be committed. Each failure yields a structured note that is fed
back to the next implementer delegation (REFLECT).
"""
import difflib
import re
import subprocess
from pathlib import Path

import probes

SUPPRESSIONS = re.compile(
    r"eslint-disable|nosemgrep|#lizard forgives|lizard forgives|istanbul ignore|@ts-ignore|@ts-nocheck"
    r"|prettier-ignore|jshint ignore|NOLINT", re.I)
EXPORT = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)?\s*([A-Za-z_$][\w$]*)?")


class Rejected(Exception):
    def __init__(self, filter_id, reason, detail=None):
        super().__init__(reason)
        self.filter_id, self.reason, self.detail = filter_id, reason, detail or {}


def apply_edits(source, edits):
    if not isinstance(edits, list) or not edits:
        raise Rejected("F1", "no edits returned")
    out = source
    for i, edit in enumerate(edits):
        search, replace = edit.get("search"), edit.get("replace")
        if not isinstance(search, str) or not isinstance(replace, str) or not search:
            raise Rejected("F1", f"edit {i} is malformed")
        count = out.count(search)
        if count != 1:
            raise Rejected("F1", f"edit {i}: search text matches {count} times (must be exactly 1)",
                           {"search_head": search[:160]})
        out = out.replace(search, replace, 1)
    if out == source:
        raise Rejected("F1", "edits do not change the file")
    return out


def unified_diff(rel, old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{rel}", f"b/{rel}", n=3))


def diff_lines(diff):
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    return added, removed


def node_parse_check(path):
    """Cheap syntax check for ES modules. JSX is checked later via ESLint fatal errors."""
    if not str(path).endswith(".mjs"):
        return None
    result = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    return None if result.returncode == 0 else result.stderr.strip()[-600:]


def target_status(finding, snap, ccn_limit):
    """How many instances of the target finding the snapshot contains (lizard: CCN of the function)."""
    rel = finding["path"]
    if finding["probe"] == "lizard":
        name = finding.get("function", "")
        ccns = [f["ccn"] for f in snap["functions"] if f["path"] == rel and f["function"] == name]
        return max(ccns) if ccns else None
    return sum(1 for f in snap["findings"]
               if f["path"] == rel and f["probe"] == finding["probe"] and f["rule"] == finding["rule"])


def target_present(finding, snap, ccn_limit):
    value = target_status(finding, snap, ccn_limit)
    if finding["probe"] == "lizard":
        return value is not None and value > ccn_limit
    return bool(value)


class Cascade:
    def __init__(self, cfg, worktree, knowledge):
        self.cfg = cfg
        self.worktree = Path(worktree)
        self.ccn_limit = cfg["probes"]["ccn_limit"]
        self.eps = cfg["cascade"].get("pareto_epsilon") or {}
        self.max_lines = cfg["cascade"]["max_changed_lines"]
        self.k = knowledge
        self.measures_of = {c: v["measures"] for c, v in cfg["characteristics"].items()}

    def snapshot(self, rel, log_dir):
        p = self.cfg["probes"]
        return probes.snapshot(self.worktree, rel, self.ccn_limit, p["timeout_seconds"], log_dir, p["semgrep"])

    def evaluate(self, finding, before, edits, log_dir):
        """Run F1-F5 on one candidate. Always restores the file. Returns a result dict."""
        rel = finding["path"]
        path = self.worktree / rel
        original = path.read_text()
        filters = []
        result = {"filters": filters, "passed": False}
        try:
            # F1 — applies, in scope, bounded size, parses
            new = apply_edits(original, edits)
            diff = unified_diff(rel, original, new)
            added, removed = diff_lines(diff)
            result["diff"], result["changed_lines"] = diff, len(added) + len(removed)
            if result["changed_lines"] > self.max_lines:
                raise Rejected("F1", f"diff changes {result['changed_lines']} lines (> {self.max_lines})")
            path.write_text(new)
            parse_error = node_parse_check(path)
            if parse_error:
                raise Rejected("F1", "file no longer parses", {"node": parse_error})
            after = self.snapshot(rel, log_dir)
            result["q_after"] = after["q"]
            if after["q"]["parse_errors"] > before["q"]["parse_errors"]:
                raise Rejected("F1", "ESLint reports a parse error", {
                    "errors": [f["message"] for f in after["findings"] if f.get("fatal")][:3]})
            filters.append({"id": "F1", "status": "pass", "changed_lines": result["changed_lines"]})

            # F2 — target finding is gone
            b, a = target_status(finding, before, self.ccn_limit), target_status(finding, after, self.ccn_limit)
            if finding["probe"] == "lizard":
                if a is None:
                    raise Rejected("F2", f"function '{finding.get('function')}' no longer exists; "
                                   "keep the function (extract helpers instead of renaming it)", {"before": b})
                if a > self.ccn_limit:
                    raise Rejected("F2", f"CCN of {finding.get('function')} is {a}, still above {self.ccn_limit}",
                                   {"before": b, "after": a})
            elif a >= b:
                raise Rejected("F2", f"rule {finding['rule']} still reported {a} time(s) (was {b})",
                               {"before": b, "after": a})
            filters.append({"id": "F2", "status": "pass", "before": b, "after": a})

            # F3 — Pareto non-regression over every measure of q
            regressions = {m: {"before": before["q"][m], "after": after["q"][m]}
                           for m in before["q"] if after["q"][m] > before["q"][m] + self.eps.get(m, 0)}
            target_measures = self.measures_of.get(finding["iso_characteristic"], [])
            gain = sum(before["q"][m] - after["q"][m] for m in target_measures)
            delta = {m: after["q"][m] - before["q"][m] for m in before["q"]}
            result["delta"] = delta
            if regressions:
                new_findings = _new_findings(before, after)
                raise Rejected("F3", "measures got worse: " + ", ".join(
                    f"{m} {v['before']}→{v['after']} ({probes.MEASURES[m]})" for m, v in regressions.items()),
                    {"regressions": regressions, "new_findings": new_findings[:5]})
            if gain <= 0:
                raise Rejected("F3", f"{finding['iso_characteristic']} did not improve on this file", {"delta": delta})
            filters.append({"id": "F3", "status": "pass", "delta": delta, "gain": gain})

            # F4 — tests
            filters.append(self.run_tests(log_dir))
            if filters[-1]["status"] == "fail":
                raise Rejected("F4", "tests failed", filters.pop())

            # F5 — anti-gaming guards on the diff
            self.guards(added, removed, original, new)
            filters.append({"id": "F5", "status": "pass"})
            result["passed"] = True
            result["new_source"] = new
        except Rejected as rej:
            filters.append({"id": rej.filter_id, "status": "fail", "reason": rej.reason, **rej.detail})
            result["failed_at"] = rej.filter_id
            result["note"] = f"{rej.filter_id} failed: {rej.reason}"
            if rej.detail.get("new_findings"):
                result["note"] += " | new findings: " + "; ".join(
                    f"{f['rule']}@{f.get('line')}: {f['message'][:90]}" for f in rej.detail["new_findings"])
        except probes.ProbeError as exc:
            filters.append({"id": "probe", "status": "error", "reason": str(exc)})
            result["failed_at"] = "probe"
            result["note"] = f"probe error: {exc}"
        finally:
            path.write_text(original)
        return result

    def run_tests(self, log_dir):
        tests = self.cfg["cascade"]["tests"]
        if not tests.get("enabled"):
            return {"id": "F4", "status": "not_run", "reason": "tests disabled in aqua.yml"}
        for attempt in range(1 + int(tests.get("retries", 1))):
            proc = subprocess.run(tests["command"], shell=True, cwd=self.worktree, text=True, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=int(tests.get("timeout_seconds", 1800)))
            (Path(log_dir) / f"tests-try{attempt}.log").write_text(proc.stdout + proc.stderr)
            if proc.returncode == 0:
                return {"id": "F4", "status": "pass", "tries": attempt + 1}
        return {"id": "F4", "status": "fail", "reason": f"exit {proc.returncode} after {attempt + 1} tries"}

    def guards(self, added, removed, original, new):
        bad = [l.strip() for l in added if SUPPRESSIONS.search(l)]
        if bad:
            raise Rejected("F5", "adds a suppression comment instead of fixing the code", {"lines": bad[:3]})
        removed_exports = {m.group(1) for l in removed if (m := EXPORT.match(l)) and m.group(1)}
        added_exports = {m.group(1) for l in added if (m := EXPORT.match(l)) and m.group(1)}
        if removed_exports - added_exports:
            raise Rejected("F5", "removes exported API", {"exports": sorted(removed_exports - added_exports)})
        code = lambda s: [l for l in s.splitlines() if l.strip() and not l.strip().startswith(("//", "*", "/*"))]
        if len(code(new)) < 0.7 * len(code(original)) and len(removed) > 20:
            raise Rejected("F5", "deletes more than 30% of the file's code")


def _new_findings(before, after):
    key = lambda f: (f["probe"], f["rule"], f["message"])
    seen = {}
    for f in before["findings"]:
        seen[key(f)] = seen.get(key(f), 0) + 1
    out = []
    for f in after["findings"]:
        if seen.get(key(f), 0):
            seen[key(f)] -= 1
        else:
            out.append(f)
    return out
