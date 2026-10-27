"""Offline tests for the control plane's invariants (no probes, no models, no git).

    python3 -m unittest discover -s loop/tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aqua"))

import probes  # noqa: E402
import report  # noqa: E402
import agents  # noqa: E402
from agents import AgentError  # noqa: E402
from knowledge import Knowledge  # noqa: E402
from plan import Planner  # noqa: E402
from runtime import Events  # noqa: E402
from session import Session, SessionError  # noqa: E402

REL = "browser/extensions/newtab/lib/Thing.sys.mjs"
SOURCE = "export function f(a) {\n  return Object.assign({}, a);\n}\n"
CFG = {
    "scope": {"component": "browser/extensions/newtab", "extensions": [".mjs"], "exclude": []},
    "characteristics": {"maintainability": {"measures": ["lint_maintainability", "ccn_excess"]},
                        "security": {"measures": ["lint_security", "semgrep_findings"]}},
    "probes": {"ccn_limit": 15, "semgrep": False, "timeout_seconds": 10},
    "cascade": {"max_changed_lines": 80, "pareto_epsilon": {}, "tests": {"enabled": False}},
    "bandit": {"prior": [1, 1], "seed": 1, "arm_override": None},
    "agents": {
        "escalation_budget": 1, "retries": 0,
        "localizer": {"model": "loc"},
        "implementers": [{"model": "impl-a"}, {"model": "impl-b"}],
        "reviewers": [{"model": "impl-a"}, {"model": "rev"}],
        "reviewer_escalation": {"model": "big"},
        "reporter": {"model": "rep"},
    },
    "session": {"rounds": 2, "attempts_per_round": 2, "candidates": 1, "max_candidates": 3,
                "max_agent_calls": 10, "max_failures_per_finding": 2, "keep_worktree": True},
}
FINDING = {"id": "fsem", "probe": "semgrep", "path": REL, "rule": "js.object-assign", "message": "m", "line": 2,
           "iso_characteristic": "security", "severity": "WARNING"}


def q(**kw):
    base = {m: 0 for m in probes.MEASURES}
    base.update(kw)
    return base


class FakeAgents:
    """Replies per role; records prompts."""

    def __init__(self, replies):
        self.replies, self.prompts = replies, []
        self.usage = {"calls": 0, "failed": 0, "seconds": 0.0, "by_model": {}}

    def call(self, role, spec, prompt, call_id, cwd=None):
        self.usage["calls"] += 1
        self.prompts.append((role, spec["model"], prompt))
        reply = self.replies[role]
        reply = reply.pop(0) if isinstance(reply, list) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply, {"model": spec["model"], "seconds": 1.0, "tool_calls": []}


class FakeCascade:
    def __init__(self, passed=True):
        self.passed = passed

    def snapshot(self, rel, log_dir):
        return {"q": q(semgrep_findings=1), "findings": [dict(FINDING)], "functions": []}

    def evaluate(self, finding, before, edits, log_dir):
        if not self.passed:
            return {"passed": False, "failed_at": "F3", "note": "F3 failed: lint_a11y 0→1", "filters": []}
        return {"passed": True, "diff": "--- a\n+++ b\n-x\n+y\n", "changed_lines": 2, "delta": q(semgrep_findings=-1),
                "filters": [{"id": f"F{i}", "status": "pass"} for i in range(1, 6)], "new_source": "NEW\n"}


def make_session(tmp, replies, passed=True):
    root = Path(tmp)
    (root / "wt" / REL).parent.mkdir(parents=True)
    (root / "wt" / REL).write_text(SOURCE)
    k = Knowledge(root / "k", root / "fp.yml", CFG["characteristics"])
    s = Session(CFG, root / "ff", k, root / "art", quiet=True)
    s.run_id, s.run_dir, s.worktree = "run1", root / "art" / "run1", root / "wt"
    s.run_dir.mkdir(parents=True)
    s.events = Events(s.run_dir / "events.jsonl", quiet=True)
    s.rounds_budget, s.started, s.baseline = 2, 0, {"revision": "abc" * 5, "overlay_sha256": "0" * 64}
    s.agents, s.cascade = FakeAgents(replies), FakeCascade(passed)
    s.planner = Planner(k, 2, seed=1)
    s.snap = {"findings": [dict(FINDING)], "functions": []}
    s.q = s.q_start = q(semgrep_findings=1)
    s.findings = [dict(FINDING)]
    s.dropped = {}
    s.fingerprint = "clean"
    s._fingerprint = lambda: "clean"
    return s


IMPL = {"hypothesis": "use spread", "edits": [{"search": "Object.assign({}, a)", "replace": "{ ...a }"}]}
LOC = {"region": {"start": 1, "end": 3}, "summary": "s", "strategy": "spread", "risks": ["keep shallow copy"],
       "false_positive": False}


def review(verdict, **kw):
    return {"verdict": verdict, "category": "none", "summary": f"{verdict} it", "comments": [], **kw}


class InvariantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, replies, passed=True):
        s = make_session(self.tmp.name, replies, passed)
        s.open_round("fsem", "test")
        return s

    def test_commit_needs_measurement_and_approval(self):          # I2
        s = self.session({"implementer": IMPL, "reviewer": [review("request_changes",
                                                                    comments=[{"line": 2, "comment": "name it"}]),
                                                             review("approve")]})
        cid = s.delegate("implementer")["candidates"][0]["candidate_id"]
        with self.assertRaises(SessionError):
            s.commit(cid)                                           # not measured
        s.evaluate()
        with self.assertRaises(SessionError):
            s.commit(cid)                                           # measured, not reviewed
        s.delegate("reviewer", candidate_id=cid)
        with self.assertRaises(SessionError):
            s.commit(cid)                                           # changes requested
        # the review comments reach the next implementer call (REFLECT)
        cid2 = s.delegate("implementer")["candidates"][0]["candidate_id"]
        self.assertIn("name it", s.agents.prompts[-1][2])
        s.evaluate()
        s.delegate("reviewer", candidate_id=cid2)
        out = s.commit(cid2)
        self.assertEqual(out["committed"], "run1-r01")
        self.assertEqual((s.worktree / REL).read_text(), "NEW\n")
        self.assertTrue(Path(out["patch"]).exists())
        self.assertIsNone(s.round)
        self.assertEqual(s.k.bandit["security"]["alpha"], 2)

    def test_failed_cascade_cannot_be_reviewed(self):
        s = self.session({"implementer": IMPL, "reviewer": review("approve")}, passed=False)
        cid = s.delegate("implementer")["candidates"][0]["candidate_id"]
        self.assertFalse(s.evaluate()["results"][0]["passed"])
        with self.assertRaises(SessionError):
            s.delegate("reviewer", candidate_id=cid)

    def test_reviewer_independence(self):                           # I3
        s = self.session({"implementer": IMPL, "reviewer": review("approve")})
        cid = s.delegate("implementer", models=["impl-a"])["candidates"][0]["candidate_id"]
        s.evaluate()
        self.assertEqual(s.delegate("reviewer", candidate_id=cid)["model"], "rev")   # a different model wins
        cand = s.candidates[cid]
        only_same = {**CFG["agents"], "reviewers": [{"model": "impl-a"}]}
        s.cfg = {**CFG, "agents": only_same}                       # single model: fresh context, same model
        self.assertEqual(s._reviewer_spec(cand, escalate=False)["model"], "impl-a")
        s.cfg = {**CFG, "agents": {**only_same, "require_different_reviewer_model": True}}
        with self.assertRaises(SessionError):
            s._reviewer_spec(cand, escalate=False)

    def test_escalations_are_rationed(self):                        # I4
        s = self.session({"implementer": IMPL, "reviewer": review("approve")})
        cid = s.delegate("implementer")["candidates"][0]["candidate_id"]
        s.evaluate()
        s.delegate("reviewer", candidate_id=cid)
        self.assertEqual(s.delegate("reviewer", candidate_id=cid, escalate=True)["model"], "big")
        with self.assertRaises(SessionError):                       # escalation_budget = 1
            s.delegate("reviewer", candidate_id=cid, escalate=True)

    def test_tamper_stops_the_session(self):                        # I1
        s = self.session({"implementer": IMPL})
        s.delegate("implementer")
        s._fingerprint = lambda: "someone-edited"
        with self.assertRaises(SessionError):
            s.evaluate()

    def test_budgets(self):                                         # I4
        s = self.session({"implementer": IMPL})
        s.delegate("implementer")
        s.delegate("implementer")
        with self.assertRaises(SessionError):
            s.delegate("implementer")                               # attempts_per_round = 2
        s.give_up("test")
        with self.assertRaises(SessionError):
            s.open_round("fsem", "again")                           # already tried this session

    def test_unavailable_reviewer_fails_closed(self):
        s = self.session({"implementer": IMPL, "reviewer": AgentError("timeout")})
        cid = s.delegate("implementer")["candidates"][0]["candidate_id"]
        s.evaluate()
        self.assertEqual(s.delegate("reviewer", candidate_id=cid)["verdict"], "unavailable")
        with self.assertRaises(SessionError):
            s.commit(cid)

    def test_report_appends_evidence_and_flags_omissions(self):     # I5
        s = self.session({"implementer": IMPL, "reviewer": review("approve"), "reporter": "# Report\nAll good."})
        cid = s.delegate("implementer")["candidates"][0]["candidate_id"]
        s.evaluate()
        s.delegate("reviewer", candidate_id=cid)
        s.commit(cid)
        s.close = lambda: None
        s.finish()
        out = s.delegate("reporter")
        self.assertEqual(out["candidates_not_mentioned_by_reporter"], ["run1-r01"])
        text = Path(out["report"]).read_text()
        self.assertIn("Evidence (generated by the controller", text)
        self.assertIn("| semgrep_findings | security | 1 | 1 |", text)   # fake cascade re-measures q
        text, missing = report.compose(None, s.evidence, "down")
        self.assertIn("reporter agent was unavailable", text)


class SubagentIsolationTest(unittest.TestCase):
    def test_global_mcp_servers_are_disabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "opencode.jsonc"
        path.write_text('{\n  // mine\n  "mcp": {"browser": {"type": "local", "command": ["x"], "enabled": true},}\n}')
        self.assertEqual(agents.foreign_mcp([path, Path(tmp.name) / "missing.json"]),
                         {"browser": {"type": "local", "command": ["x"], "enabled": False}})


if __name__ == "__main__":
    unittest.main()
