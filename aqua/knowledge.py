"""KNOWLEDGE: the state that persists across rounds and runs (the K in MAPE-K).

  ledger.jsonl        quality vector q after every round (evidence trail)
  bandit.json         Beta(alpha, beta) posterior + cost per ISO characteristic
  playbook.jsonl      accepted fixes per rule, retrieved as examples for REPAIR
  attempts.json       per-finding failure history (cool-down / unresolved)
  candidates.json     accepted candidates awaiting human feedback
  fp-suggestions.jsonl  localizer/reviewer claims of false positives, for a human to confirm
The confirmed false-positive base stays in loop/accepted-false-positives.yml.
"""
import datetime as dt
import json
from pathlib import Path

import yaml


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Knowledge:
    def __init__(self, root, fp_file, arms, prior=(1, 1)):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fp_file = Path(fp_file)
        self.arms = list(arms)
        self.prior = tuple(prior)
        self.bandit = self._load("bandit.json", {})
        for arm in self.arms:
            self.bandit.setdefault(arm, {"alpha": prior[0], "beta": prior[1], "pulls": 0,
                                         "accepted": 0, "seconds": 0.0})
        self.attempts = self._load("attempts.json", {})
        self.candidates = self._load("candidates.json", {})

    # ---- persistence -------------------------------------------------------
    def _load(self, name, default):
        path = self.root / name
        return json.loads(path.read_text()) if path.exists() else default

    def _save(self, name, value):
        tmp = self.root / (name + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True))
        tmp.replace(self.root / name)

    def _append(self, name, row):
        with (self.root / name).open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")

    def _rows(self, name):
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def save(self):
        self._save("bandit.json", self.bandit)
        self._save("attempts.json", self.attempts)
        self._save("candidates.json", self.candidates)

    # ---- ledger ------------------------------------------------------------
    def record_quality(self, run_id, round_no, q, open_findings, note=""):
        self._append("ledger.jsonl", {"time": now(), "run": run_id, "round": round_no,
                                      "q": q, "open_findings": open_findings, "note": note})

    def ledger(self):
        return self._rows("ledger.jsonl")

    # ---- bandit ------------------------------------------------------------
    def reward(self, arm, success, seconds):
        state = self.bandit[arm]
        state["pulls"] += 1
        state["seconds"] += seconds
        if success:
            state["alpha"] += 1
            state["accepted"] += 1
        else:
            state["beta"] += 1

    def human_feedback(self, candidate_id, verdict):
        cand = self.candidates.get(candidate_id)
        if not cand:
            raise KeyError(candidate_id)
        if cand.get("human"):
            raise ValueError(f"{candidate_id} already has feedback: {cand['human']}")
        cand["human"] = verdict
        cand["human_time"] = now()
        if verdict == "reject":
            # The cascade already paid alpha += 1; a human rejection adds evidence of failure.
            self.bandit[cand["characteristic"]]["beta"] += 1
        self.save()

    # ---- false positives ---------------------------------------------------
    def false_positive_rules(self):
        if not self.fp_file.exists():
            return []
        entries = yaml.safe_load(self.fp_file.read_text()) or []
        return [e for e in entries if isinstance(e, dict)]

    def is_false_positive(self, finding):
        for entry in self.false_positive_rules():
            if entry.get("probe") and entry["probe"] != finding.get("probe"):
                continue
            if entry.get("path") and entry["path"] != finding.get("path"):
                continue
            if entry.get("rule") and entry["rule"] not in str(finding.get("rule", "")):
                continue
            if entry.get("function") and entry["function"] != finding.get("function"):
                continue
            if any(entry.get(k) for k in ("probe", "path", "rule", "function")):
                return True
        return False

    def suggest_false_positive(self, finding, reason, source):
        self._append("fp-suggestions.jsonl", {"time": now(), "finding": finding,
                                              "reason": reason, "source": source})

    def confirm_false_positive(self, finding, reason):
        entry = {k: finding[k] for k in ("probe", "path", "rule", "function") if finding.get(k)}
        entry["reason"] = reason
        with self.fp_file.open("a") as stream:
            stream.write("\n" + yaml.safe_dump([entry], sort_keys=False))

    # ---- attempts ----------------------------------------------------------
    def failures(self, finding_id):
        return self.attempts.get(finding_id, {}).get("failures", 0)

    def record_attempt(self, finding_id, success, reasons):
        row = self.attempts.setdefault(finding_id, {"failures": 0, "history": []})
        if not success:
            row["failures"] += 1
        row["history"].append({"time": now(), "success": success, "reasons": reasons[-3:]})

    # ---- playbook ----------------------------------------------------------
    def add_play(self, finding, hypothesis, diff):
        self._append("playbook.jsonl", {"time": now(), "probe": finding["probe"],
                                        "rule": finding["rule"], "hypothesis": hypothesis,
                                        "diff": diff[:4000]})

    def plays_for(self, finding, limit=2):
        rows = [r for r in self._rows("playbook.jsonl")
                if r["rule"] == finding["rule"] and r["probe"] == finding["probe"]]
        return rows[-limit:]

    # ---- candidates for human review ---------------------------------------
    def add_candidate(self, candidate_id, record):
        self.candidates[candidate_id] = record
