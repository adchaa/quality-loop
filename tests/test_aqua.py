"""Offline tests for AQUA's deterministic parts (no probes, no models).

    python3 -m unittest discover -s loop/tests -v
"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aqua"))

import probes  # noqa: E402
from agents import AgentError, parse_json_object  # noqa: E402
from analyze import analyze  # noqa: E402
from cascade import Cascade, Rejected, apply_edits  # noqa: E402
from knowledge import Knowledge  # noqa: E402
from plan import Planner  # noqa: E402

REL = "browser/extensions/newtab/lib/Thing.sys.mjs"
CFG = {
    "scope": {"component": "browser/extensions/newtab", "extensions": [".mjs", ".js", ".jsx"],
              "exclude": ["browser/extensions/newtab/test/"]},
    "characteristics": {"maintainability": {"measures": ["lint_maintainability", "ccn_excess"]},
                        "security": {"measures": ["lint_security", "semgrep_findings"]},
                        "interaction_capability": {"measures": ["lint_a11y"]}},
    "probes": {"ccn_limit": 15, "semgrep": False, "timeout_seconds": 10},
    "cascade": {"max_changed_lines": 80, "pareto_epsilon": {}, "tests": {"enabled": False}},
}
SOURCE = "export function f(a) {\n  return Object.assign({}, a);\n}\n"


def q(**kw):
    base = {m: 0 for m in probes.MEASURES}
    base.update(kw)
    return base


def snap(qv, findings=(), functions=()):
    return {"q": qv, "findings": list(findings), "functions": list(functions)}


SEM = {"probe": "semgrep", "path": REL, "rule": "js.object-assign", "message": "m", "line": 2,
       "iso_characteristic": "security"}


class CascadeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / REL).parent.mkdir(parents=True)
        (root / REL).write_text(SOURCE)
        kn = Knowledge(root / "k", root / "fp.yml", CFG["characteristics"])
        self.cascade = Cascade(CFG, root, kn)
        self.root = root
        self.before = snap(q(semgrep_findings=1), [SEM])
        self.finding = dict(SEM, id="x")

    def tearDown(self):
        self.tmp.cleanup()

    def evaluate(self, after, edits):
        self.cascade.snapshot = lambda rel, log_dir: after
        return self.cascade.evaluate(self.finding, self.before, edits, self.root)

    def test_accepts_measured_improvement_and_restores_file(self):
        edits = [{"search": "Object.assign({}, a)", "replace": "{ ...a }"}]
        res = self.evaluate(snap(q()), edits)
        self.assertTrue(res["passed"], res)
        self.assertEqual([f["id"] for f in res["filters"]], ["F1", "F2", "F3", "F4", "F5"])
        self.assertEqual(res["filters"][3]["status"], "not_run")
        self.assertEqual((self.root / REL).read_text(), SOURCE)   # controller applies winners later

    def test_pareto_rejects_regression_in_other_characteristic(self):
        edits = [{"search": "Object.assign({}, a)", "replace": "{ ...a }"}]
        res = self.evaluate(snap(q(lint_a11y=1)), edits)
        self.assertEqual(res["failed_at"], "F3")
        self.assertIn("lint_a11y", res["note"])

    def test_target_still_present(self):
        edits = [{"search": "Object.assign({}, a)", "replace": "Object.assign({}, a, {})"}]
        res = self.evaluate(snap(q(semgrep_findings=1), [SEM]), edits)
        self.assertEqual(res["failed_at"], "F2")

    def test_suppression_comment_is_gaming(self):
        edits = [{"search": "  return", "replace": "  // nosemgrep\n  return"}]
        res = self.evaluate(snap(q()), edits)
        self.assertEqual(res["failed_at"], "F5")

    def test_removed_export_is_rejected(self):
        edits = [{"search": "export function f(a)", "replace": "function f(a)"}]
        res = self.evaluate(snap(q()), edits)
        self.assertEqual(res["failed_at"], "F5")

    def test_parse_error_fails_f1(self):
        edits = [{"search": "return Object.assign({}, a);", "replace": "return {{ ...a ;"}]
        res = self.evaluate(snap(q()), edits)
        self.assertEqual(res["failed_at"], "F1")

    def test_lizard_rename_is_rejected(self):
        self.finding = {"probe": "lizard", "path": REL, "rule": "lizard-ccn", "function": "f",
                        "iso_characteristic": "maintainability", "id": "y"}
        self.before = snap(q(ccn_excess=3), functions=[{"path": REL, "function": "f", "ccn": 18}])
        edits = [{"search": "function f(a)", "replace": "function g(a)"}]
        after = snap(q(), functions=[{"path": REL, "function": "g", "ccn": 2}])
        CFG_exports = self.evaluate(after, edits)
        self.assertEqual(CFG_exports["failed_at"], "F2")

    def test_apply_edits_requires_unique_match(self):
        with self.assertRaises(Rejected):
            apply_edits("a a", [{"search": "a", "replace": "b"}])
        with self.assertRaises(Rejected):
            apply_edits("a", [{"search": "a", "replace": "a"}])
        self.assertEqual(apply_edits("a b", [{"search": "b", "replace": "c"}]), "a c")


class PlannerKnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "fp.yml").write_text("- rule: unsafe-formatstring\n  reason: console only\n")
        self.k = Knowledge(root / "k", root / "fp.yml", CFG["characteristics"])

    def tearDown(self):
        self.tmp.cleanup()

    def findings(self):
        raw = [
            {"probe": "lizard", "path": "browser/extensions/newtab/lib/A.sys.mjs", "rule": "lizard-ccn",
             "function": "a", "severity": 5, "message": "m", "iso_characteristic": "maintainability"},
            dict(SEM),
            dict(SEM, rule="js.unsafe-formatstring.x"),
            dict(SEM, path="browser/extensions/newtab/test/jest/T.test.jsx"),
        ]
        return analyze(raw, CFG["scope"], self.k)

    def test_analyze_drops_fp_and_out_of_scope(self):
        findings, dropped = self.findings()
        self.assertEqual(len(findings), 2)
        self.assertEqual(dropped["false_positive"], 1)
        self.assertEqual(dropped["out_of_scope"], 1)

    def test_thompson_prefers_successful_arm(self):
        findings, _ = self.findings()
        for _ in range(20):
            self.k.reward("security", True, 10)
            self.k.reward("maintainability", False, 10)
        planner = Planner(self.k, max_failures=2, seed=1)
        arms = [planner.choose(findings, set())[1]["arm"] for _ in range(50)]
        self.assertGreater(arms.count("security"), 45)

    def test_failed_findings_are_parked(self):
        findings, _ = self.findings()
        sec = [f for f in findings if f["iso_characteristic"] == "security"][0]
        self.k.record_attempt(sec["id"], False, ["F3"])
        self.k.record_attempt(sec["id"], False, ["F3"])
        planner = Planner(self.k, max_failures=2, seed=1)
        for _ in range(10):
            self.assertEqual(planner.choose(findings, set())[1]["arm"], "maintainability")

    def test_human_reject_penalizes_arm(self):
        self.k.reward("security", True, 5)
        self.k.add_candidate("c1", {"characteristic": "security"})
        self.k.human_feedback("c1", "reject")
        self.assertEqual((self.k.bandit["security"]["alpha"], self.k.bandit["security"]["beta"]), (2, 2))


class ParseTest(unittest.TestCase):
    def test_last_matching_object(self):
        text = 'thinking {"a": 1}\n```json\n{"veto": false, "reason": "ok"}\n```'
        self.assertEqual(parse_json_object(text, ["veto", "reason"])["reason"], "ok")
        with self.assertRaises(AgentError):
            parse_json_object("no json", ["veto"])


if __name__ == "__main__":
    random.seed(0)
    unittest.main()
