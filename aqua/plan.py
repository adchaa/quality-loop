"""PLAN: choose which ISO characteristic to improve next, then which finding.

Characteristic choice is a multi-armed bandit solved with Thompson sampling
(Russo et al., "A Tutorial on Thompson Sampling", 2018). Each arm keeps a
Beta(alpha, beta) posterior over "a round spent on this characteristic yields an
accepted, measured improvement". The sample is divided by the arm's relative cost
(mean seconds per round) so cheap, successful arms are preferred.
"""
import random

SEVERITY = {"ERROR": 3, "WARNING": 2, "INFO": 1, 2: 2, 1: 1, "error": 2, "warning": 1}


def finding_priority(finding, failures):
    if finding["probe"] == "lizard":
        gain = 1 + min(10, finding.get("severity", 1)) / 5   # CCN excess, capped
    else:
        gain = SEVERITY.get(finding.get("severity"), 1)
    return gain / (1 + failures)


class Planner:
    def __init__(self, knowledge, max_failures, seed=None, override=None):
        self.k = knowledge
        self.max_failures = max_failures
        self.rng = random.Random(seed)
        self.override = override
        self.turn = 0

    def eligible(self, findings, skip_ids):
        return [f for f in findings
                if f["id"] not in skip_ids and self.k.failures(f["id"]) < self.max_failures]

    def choose_arm(self, findings):
        arms = sorted({f["iso_characteristic"] for f in findings if f["iso_characteristic"] in self.k.bandit})
        if not arms:
            return None, {}
        if self.override == "random":
            return self.rng.choice(arms), {}
        if self.override == "round_robin":
            self.turn += 1
            return arms[(self.turn - 1) % len(arms)], {}
        mean_cost = self._mean_cost()
        draws = {}
        for arm in arms:
            state = self.k.bandit[arm]
            theta = self.rng.betavariate(state["alpha"], state["beta"])
            cost = (state["seconds"] / state["pulls"]) / mean_cost if state["pulls"] and mean_cost else 1.0
            draws[arm] = {"theta": round(theta, 4), "cost": round(cost, 3),
                          "score": round(theta / max(cost, 0.1), 4),
                          "alpha": state["alpha"], "beta": state["beta"]}
        best = max(arms, key=lambda a: draws[a]["score"])
        return best, draws

    def _mean_cost(self):
        pulls = sum(s["pulls"] for s in self.k.bandit.values())
        seconds = sum(s["seconds"] for s in self.k.bandit.values())
        return seconds / pulls if pulls else 0.0

    def choose(self, findings, skip_ids):
        """Returns (finding or None, decision record for the event log)."""
        pool = self.eligible(findings, skip_ids)
        arm, draws = self.choose_arm(pool)
        if arm is None:
            return None, {"reason": "no eligible findings"}
        in_arm = [f for f in pool if f["iso_characteristic"] == arm]
        ranked = sorted(in_arm, key=lambda f: (-finding_priority(f, self.k.failures(f["id"])), f["path"], f.get("line") or 0))
        finding = ranked[0]
        return finding, {"arm": arm, "draws": draws, "policy": self.override or "thompson",
                         "candidates_in_arm": len(in_arm), "finding_id": finding["id"]}
