"""The AQUA control plane: every action the orchestrator agent can take.

The orchestrator (an LLM agent the human picks and starts, running the `aqua`
skill in loop/skills/aqua/SKILL.md) decides WHAT to do next. This module decides whether the
action is ALLOWED and what the EVIDENCE says. The guarantees are enforced here,
in code, not in any prompt:

  I1 sole writer   only the controller writes to the candidate worktree;
                   a fingerprint check refuses to continue if anything else did
  I2 two keys      a candidate is committed only if F1-F5 passed on exactly that
                   content AND an independent reviewer approved exactly that candidate
  I3 independence  the reviewer is a separate agent with a fresh context; a different
                   model from the implementer whenever one is configured
  I4 budgets       rounds, implementer delegations per round, agent calls, escalations
  I5 grounding     every number in the human report comes from measurements
                   (the controller appends the evidence tables to the reporter's text)

One session = one run directory under artifacts/aqua-runs/<run-id>/ and one
detached worktree. One round = one finding, opened by the orchestrator and closed
by commit or give_up.
"""
import datetime as dt
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

import probes
import report
from agents import AgentError, Agents
from analyze import analyze, characteristic_counts
from cascade import Cascade, target_present
from plan import Planner, finding_priority
from runtime import Events, LoopError, candidate_worktree, checkout_state, git

SOURCE_WINDOW = 60
ROLES = ("localizer", "implementer", "reviewer", "reporter")


class SessionError(LoopError):
    """A refused action. The message tells the orchestrator what to do instead."""


# ---- MONITOR helpers -----------------------------------------------------
def monitor(cfg, repo, log_dir):
    p = cfg["probes"]
    return probes.snapshot(repo, cfg["scope"]["component"], p["ccn_limit"], p["timeout_seconds"], log_dir, p["semgrep"])


def load_or_monitor(cfg, repo, knowledge, log_dir, reuse, events):
    """Full-scope MONITOR, or reuse the cached one if it measured the same code."""
    cache = knowledge.root / "monitor-latest.json"
    state = checkout_state(repo)
    if reuse and cache.exists():
        cached = json.loads(cache.read_text())
        same = all(cached.get(k) == state[k] for k in ("revision", "overlay_sha256"))
        if same and cached.get("ccn_limit") == cfg["probes"]["ccn_limit"]:
            events.emit("monitor", "reused", revision=state["revision"][:12])
            return cached["snapshot"]
        events.emit("monitor", "cache_stale")
    events.emit("monitor", "running", target=cfg["scope"]["component"])
    started = time.monotonic()
    snap = monitor(cfg, repo, log_dir)
    events.emit("monitor", "done", seconds=round(time.monotonic() - started), findings=len(snap["findings"]))
    cache.write_text(json.dumps({"revision": state["revision"], "overlay_sha256": state["overlay_sha256"],
                                 "ccn_limit": cfg["probes"]["ccn_limit"], "time": dt.datetime.now().isoformat(),
                                 "snapshot": snap}))
    return snap


def global_q(snap, ccn_limit):
    lint = [f for f in snap["findings"] if f["probe"] == "eslint"]
    sem = [f for f in snap["findings"] if f["probe"] == "semgrep"]
    return probes.vector(lint, sem, snap["functions"], ccn_limit)


# ---- prompt helpers ------------------------------------------------------
def finding_view(f):
    return {k: f.get(k) for k in ("id", "probe", "path", "line", "rule", "function", "complexity",
                                  "severity", "message", "iso_characteristic", "iso_subcharacteristic")
            if f.get(k) is not None}


def numbered_excerpt(source, start, end):
    lines = source.splitlines()
    start, end = max(1, start), min(len(lines), end)
    return start, end, "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))


def nested_agents_md(worktree, rel, limit=8000):
    """AGENTS.md files between the repo root (loaded by OpenCode itself) and the target."""
    found, current = [], Path(worktree)
    for part in PurePosixPath(rel).parent.parts:
        current = current / part
        doc = current / "AGENTS.md"
        if doc.is_file():
            found.append(f"--- {doc.relative_to(worktree)} ---\n{doc.read_text()}")
    return "\n".join(found)[:limit]


def _fence(value):
    return "```json\n" + json.dumps(value, indent=2, default=str) + "\n```"


class Session:
    def __init__(self, cfg, firefox_root, knowledge, artifacts, driver="agent", quiet=False):
        self.cfg, self.firefox_root, self.k = cfg, firefox_root, knowledge
        self.artifacts, self.driver, self.quiet = Path(artifacts), driver, quiet
        self.s = cfg["session"]
        self.run_id = None
        self.finished = False
        self.closed = False
        self.round = None              # the open round, or None
        self.rounds = []               # closed rounds
        self.candidates = {}           # cid -> candidate record (this session)
        self.tried = set()
        self.last_plan = None
        self.reported = None
        self.escalations = 0           # reviewer escalations used (I4)

    # ======================================================================
    # lifecycle
    # ======================================================================
    def start(self, rounds=None, fresh_monitor=False):
        if self.run_id:
            raise SessionError(f"session {self.run_id} is already running; run `aqua status`")
        self.rounds_budget = int(rounds or self.s["rounds"])
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.run_dir = self.artifacts / "aqua-runs" / self.run_id
        self.run_dir.mkdir(parents=True)
        self.events = Events(self.run_dir / "events.jsonl", self.quiet)
        self.events.emit("session", "start", run=self.run_id, driver=self.driver, rounds=self.rounds_budget)
        self.started = time.monotonic()
        try:
            snap = load_or_monitor(self.cfg, self.firefox_root, self.k, self.run_dir / "monitor",
                                   not fresh_monitor, self.events)
            self.stack = ExitStack()
            self.worktree, state = self.stack.enter_context(
                candidate_worktree(self.firefox_root, self.run_dir, bool(self.s.get("keep_worktree"))))
        except Exception:
            self.run_id = None
            raise
        self.baseline = {"revision": state["revision"], "overlay_sha256": state["overlay_sha256"]}
        self.agents = Agents(self.cfg, self.worktree, self.run_dir, self.events)
        self.cascade = Cascade(self.cfg, self.worktree, self.k)
        self.planner = Planner(self.k, self.s["max_failures_per_finding"], self.cfg["bandit"].get("seed"),
                               self.cfg["bandit"].get("arm_override"))
        self.snap = snap
        self.q = self.q_start = global_q(snap, self.cfg["probes"]["ccn_limit"])
        self.findings, self.dropped = analyze(snap["findings"], self.cfg["scope"], self.k)
        self.k.record_quality(self.run_id, 0, self.q, characteristic_counts(self.findings), "MONITOR baseline")
        self.fingerprint = self._fingerprint()
        return self.status()

    def close(self):
        """Remove the worktree (unless keep_worktree). Idempotent."""
        if self.run_id and not self.closed:
            self.closed = True
            self.stack.close()
            self.events.emit("session", "closed", worktree_kept=bool(self.s.get("keep_worktree")))

    # ======================================================================
    # read-only views
    # ======================================================================
    def status(self):
        if not self.run_id:
            return {"session": None, "next": "aqua start"}
        usage = self.agents.usage
        return {
            "run_id": self.run_id, "run_dir": str(self.run_dir), "driver": self.driver,
            "finished": self.finished,
            "q": probes.by_characteristic(self.q),
            "q_delta_so_far": {m: self.q[m] - self.q_start[m] for m in self.q if self.q[m] != self.q_start[m]},
            "eligible_findings": characteristic_counts(self.findings),
            "dropped": self.dropped,
            "budgets": {"rounds_left": self.rounds_budget - len(self.rounds) - (1 if self.round else 0),
                        "agent_calls_left": int(self.s["max_agent_calls"]) - usage["calls"],
                        "escalations_left": self.escalations_left()},
            "open_round": self._round_view() if self.round else None,
            "closed_rounds": [{"round": r["round"], "finding": r["finding"]["id"], "status": r["status"],
                               "candidate_id": r.get("candidate_id")} for r in self.rounds],
        }

    def list_findings(self, characteristic=None, limit=10):
        self._need_session()
        pool = [f for f in self.findings if not characteristic or f["iso_characteristic"] == characteristic]
        rows = []
        for f in pool:
            failures = self.k.failures(f["id"])
            parked = failures >= self.s["max_failures_per_finding"]
            rows.append({**finding_view(f), "priority": round(finding_priority(f, failures), 3),
                         "failures": failures, "available": not parked and f["id"] not in self.tried})
        rows.sort(key=lambda r: (not r["available"], -r["priority"]))
        return {"total": len(rows), "findings": rows[:max(1, min(int(limit), 50))]}

    def plan(self):
        self._need_session()
        finding, decision = self.planner.choose(self.findings, self.tried)
        self.last_plan = {"finding_id": finding["id"] if finding else None, **decision}
        self.events.emit("plan", "suggested" if finding else "nothing_left",
                         arm=decision.get("arm"), finding=decision.get("finding_id"))
        return {"suggestion": finding_view(finding) if finding else None, "decision": decision}

    def candidate(self, cid):
        cand = self._candidate(cid)
        return {k: v for k, v in cand.items() if k not in ("new_source", "edits")}

    # ======================================================================
    # rounds
    # ======================================================================
    def open_round(self, finding_id, rationale):
        self._need_session(open_round=False)
        if len(self.rounds) >= self.rounds_budget:
            raise SessionError(f"round budget ({self.rounds_budget}) is used up; run `aqua finish`")
        finding = next((f for f in self.findings if f["id"] == finding_id), None)
        if not finding:
            raise SessionError(f"{finding_id} is not an eligible finding; see `aqua findings` or `aqua plan`")
        if finding_id in self.tried:
            raise SessionError(f"{finding_id} was already tried in this session; pick another")
        if self.k.failures(finding_id) >= self.s["max_failures_per_finding"]:
            raise SessionError(f"{finding_id} failed {self.k.failures(finding_id)} times before and is parked")
        self.tried.add(finding_id)
        no = len(self.rounds) + 1
        work = self.run_dir / "rounds" / f"r{no:02d}"
        work.mkdir(parents=True, exist_ok=True)
        (work / "finding.json").write_text(json.dumps(finding, indent=2))
        followed = bool(self.last_plan and self.last_plan.get("finding_id") == finding_id)
        self.round = {"round": no, "finding": finding, "work": work, "started": time.monotonic(),
                      "rationale": str(rationale)[:1000], "plan": self.last_plan, "followed_plan": followed,
                      "localization": None, "implement_calls": 0, "candidates": [], "notes": [], "reviews": []}
        self.events.emit("round", "open", round=no, finding=finding_id, followed_plan=followed)
        before = self.cascade.snapshot(finding["path"], work / "probe-before")
        self.round["before"] = before
        if not target_present(finding, before, self.cfg["probes"]["ccn_limit"]):
            self._close_round("not_reproduced", "the finding's probe no longer reports it", reward=False)
            return {"round": no, "reproduced": False, "next": "the round was closed; plan another finding"}
        return {"round": no, "reproduced": True, "finding": finding_view(finding),
                "file_q": {m: v for m, v in before["q"].items() if v},
                "next": "delegate a localizer, then implementers"}

    def give_up(self, reason):
        self._need_session(open_round=True)
        self._close_round("unresolved", str(reason)[:500], reward=True)
        return {"closed": True, "rounds_left": self.rounds_budget - len(self.rounds)}

    def _close_round(self, status, reason, reward, committed=None):
        rd = self.round
        seconds = round(time.monotonic() - rd["started"], 1)
        finding = rd["finding"]
        success = status == "committed"
        if reward:
            self.k.reward(finding["iso_characteristic"], success, seconds)
            self.k.record_attempt(finding["id"], success, rd["notes"])
        record = {"round": rd["round"], "finding": finding_view(finding), "status": status, "reason": reason,
                  "seconds": seconds, "rationale": rd["rationale"], "plan": rd["plan"],
                  "followed_plan": rd["followed_plan"], "localization": rd["localization"],
                  "implement_calls": rd["implement_calls"], "candidates": rd["candidates"],
                  "reviews": rd["reviews"], "q_file_before": rd.get("before", {}).get("q")}
        if committed:
            record.update(committed)
        self.rounds.append(record)
        self.k.record_quality(self.run_id, rd["round"], self.q, characteristic_counts(self.findings),
                              f"{status}: {finding['iso_characteristic']} {finding['rule']} {finding['path']}")
        self.k.save()
        (rd["work"] / "outcome.json").write_text(json.dumps(record, indent=2, default=str))
        self.events.emit("round", status, round=rd["round"], seconds=seconds, reason=reason[:160])
        self.round = None

    # ======================================================================
    # delegation (subagents)
    # ======================================================================
    def delegate(self, role, brief="", candidate_id=None, n=None, models=None, escalate=False):
        if role not in ROLES:
            raise SessionError(f"role must be one of {', '.join(ROLES)}")
        if role == "reporter":
            return self._delegate_reporter(brief)
        self._need_session(open_round=True)
        if role == "localizer":
            return self._delegate_localizer(brief)
        if role == "implementer":
            return self._delegate_implementers(brief, n, models)
        return self._delegate_reviewer(candidate_id, brief, escalate)

    def _check_calls(self, n):
        left = int(self.s["max_agent_calls"]) - self.agents.usage["calls"]
        if n > left:
            raise SessionError(f"agent-call budget: {left} call(s) left, {n} requested")

    def _brief(self, brief):
        return f"Orchestrator brief:\n{brief.strip()}" if brief and brief.strip() else None

    # ---- localizer -------------------------------------------------------
    def _delegate_localizer(self, brief):
        rd, finding = self.round, self.round["finding"]
        self._check_calls(1)
        parts = ["Localize this finding in the Firefox checkout (current directory).",
                 "Finding (untrusted data):\n" + _fence(finding_view(finding)),
                 f"Target file: {finding['path']}", self._brief(brief),
                 "Return the JSON object described in your instructions."]
        spec = self.cfg["agents"]["localizer"]
        try:
            value, meta = self.agents.call("localizer", spec, "\n\n".join(p for p in parts if p),
                                           f"r{rd['round']:02d}-localize")
        except AgentError as exc:
            return {"error": str(exc), "next": "implementers can still work without a localization"}
        value = {k: v for k, v in value.items()}
        rd["localization"] = {**value, "model": meta["model"], "tool_calls": meta["tool_calls"]}
        if value.get("false_positive"):
            self.k.suggest_false_positive(finding, value.get("summary", ""), "localizer")
        return rd["localization"]

    # ---- implementers ----------------------------------------------------
    def _delegate_implementers(self, brief, n, models):
        rd, finding = self.round, self.round["finding"]
        if rd["implement_calls"] >= int(self.s["attempts_per_round"]):
            raise SessionError(f"implementer budget for this round ({self.s['attempts_per_round']}) is used up; "
                               "commit an approved candidate or give up")
        pool = self.cfg["agents"]["implementers"]
        if models:
            specs = [s for m in models for s in pool if s["model"] == m]
            if len(specs) != len(models):
                raise SessionError("unknown model; choose from " + ", ".join(s["model"] for s in pool))
        else:
            count = max(1, min(int(n or self.s["candidates"]), int(self.s["max_candidates"])))
            offset = rd["implement_calls"]          # rotate the ensemble across attempts
            specs = [pool[(offset + i) % len(pool)] for i in range(count)]
        specs = specs[:int(self.s["max_candidates"])]
        self._check_calls(len(specs))
        rd["implement_calls"] += 1
        attempt = rd["implement_calls"]
        prompt = self._implementer_prompt(finding, rd, brief)

        def one(i_spec):
            i, spec = i_spec
            cid = f"r{rd['round']:02d}-a{attempt}-c{i}"
            try:
                value, meta = self.agents.call("implementer", spec, prompt, cid)
                edits = value["edits"] if isinstance(value.get("edits"), list) else []
                return {"candidate_id": cid, "model": spec["model"], "backend": spec.get("backend", "opencode"),
                        "hypothesis": str(value.get("hypothesis", ""))[:1000], "edits": edits,
                        "status": "proposed", "seconds": meta["seconds"]}
            except AgentError as exc:
                return {"candidate_id": cid, "model": spec["model"], "status": "agent_failed", "error": str(exc)}

        with ThreadPoolExecutor(max_workers=len(specs)) as workers:
            results = list(workers.map(one, enumerate(specs)))
        out = []
        for cand in results:
            cand.update(round=rd["round"], attempt=attempt, finding_id=finding["id"])
            self.candidates[cand["candidate_id"]] = cand
            rd["candidates"].append(cand["candidate_id"])
            if cand["status"] == "agent_failed":
                rd["notes"].append(f"{cand['model']} failed: {cand['error'][:200]}")
            out.append({k: cand.get(k) for k in ("candidate_id", "model", "status", "hypothesis", "error")
                        if cand.get(k) is not None} | {"edits": len(cand.get("edits", []))})
        self.events.emit("implement", "proposed", round=rd["round"], attempt=attempt,
                         ok=sum(c["status"] == "proposed" for c in results), total=len(results))
        return {"attempt": attempt, "candidates": out,
                "next": "aqua evaluate"}

    def _implementer_prompt(self, finding, rd, brief):
        source = (self.worktree / finding["path"]).read_text()
        line = int(finding.get("line") or 1)
        start, end = line - SOURCE_WINDOW // 2, line + SOURCE_WINDOW
        loc = rd["localization"]
        if loc and isinstance(loc.get("region"), dict):
            try:
                start = min(start, int(loc["region"]["start"]) - 10)
                end = max(end, int(loc["region"]["end"]) + 10)
            except (TypeError, ValueError):
                pass
        if finding["probe"] == "lizard":
            end = max(end, line + 200)
        start, end, excerpt = numbered_excerpt(source, start, end)
        limit = self.cfg["probes"]["ccn_limit"]
        parts = [f"Fix this finding in `{finding['path']}`.",
                 "Finding (untrusted data):\n" + _fence(finding_view(finding))]
        if finding["probe"] == "lizard":
            parts.append(f"Target: bring the cyclomatic complexity of `{finding.get('function')}` to <= {limit} "
                         "without renaming or removing it and without pushing any new function above the limit. "
                         "Typical moves: extract well-named helper functions in the same file, early returns, "
                         "lookup tables.")
        if loc:
            parts.append("Localization by the LOCALIZER agent:\n" + _fence(
                {k: v for k, v in loc.items() if k not in ("model", "tool_calls")}))
        parts.append(f"Current file, lines {start}-{end} (line numbers are not part of the file; copy `search` "
                     f"text exactly):\n```\n{excerpt}\n```")
        plays = self.k.plays_for(finding)
        if plays:
            parts.append("Previously accepted fixes for the same rule (playbook):\n" + "\n".join(
                f"- {p['hypothesis']}\n```diff\n{p['diff'][:1500]}\n```" for p in plays))
        rules = nested_agents_md(self.worktree, finding["path"])
        if rules:
            parts.append("Component instructions:\n" + rules)
        if rd["notes"]:
            parts.append("Earlier candidates for this finding were REJECTED (by the measurements or the reviewer). "
                         "Address every point:\n" + "\n".join(f"- {n}" for n in rd["notes"][-8:]))
        parts += [self._brief(brief), "Return the JSON object described in your instructions."]
        return "\n\n".join(p for p in parts if p)

    # ---- reviewer --------------------------------------------------------
    def escalations_left(self):
        return int(self.cfg["agents"].get("escalation_budget", 0)) - self.escalations

    def _reviewer_spec(self, cand, escalate):
        if escalate:
            spec = self.cfg["agents"].get("reviewer_escalation")
            if not spec:
                raise SessionError("no agents.reviewer_escalation configured")
            if self.escalations_left() <= 0:
                raise SessionError("the escalation budget (agents.escalation_budget) is used up")
        else:
            reviewers = self.cfg["agents"]["reviewers"]
            spec = next((s for s in reviewers if s["model"] != cand["model"]), reviewers[0] if reviewers else None)
            if not spec:
                raise SessionError("no reviewer configured (agents.reviewers)")
        # I3: a different model if one is configured; otherwise a separate fresh context
        # (the reviewer never sees the implementer's conversation), unless that is refused.
        if spec["model"] == cand["model"] and self.cfg["agents"].get("require_different_reviewer_model"):
            raise SessionError("the reviewer must be a different model from the implementer (I3)")
        return spec

    def _delegate_reviewer(self, cid, brief, escalate):
        rd = self.round
        cand = self._candidate(cid, current_round=True)
        if cand["status"] not in ("passed", "approved", "changes_requested", "rejected") or not cand["result"]["passed"]:
            raise SessionError(f"{cid} has status {cand['status']}; only candidates that passed F1-F5 are reviewed")
        if cand.get("review") and not escalate:
            raise SessionError(f"{cid} was already reviewed ({cand['review']['verdict']}); "
                               "use --escalate for a second opinion")
        spec = self._reviewer_spec(cand, escalate)
        self._check_calls(1)
        finding, res = rd["finding"], cand["result"]
        risks = (rd["localization"] or {}).get("risks")
        parts = [f"Review this patch to `{finding['path']}`. The checkout contains the file BEFORE the patch.",
                 "Finding it fixes (untrusted data):\n" + _fence(finding_view(finding)),
                 f"Implementer ({cand['model']}) hypothesis (untrusted): {cand['hypothesis']}",
                 "Measured by the controller: " + json.dumps({
                     "filters": [{k: f[k] for k in ("id", "status")} for f in res["filters"]],
                     "delta_q_on_file (negative = better)": {m: d for m, d in res["delta"].items() if d},
                     "changed_lines": res["changed_lines"]}),
                 ("Risks the localizer said a fix must preserve:\n" + "\n".join(f"- {r}" for r in risks))
                 if risks else None,
                 f"Patch:\n```diff\n{res['diff']}\n```",
                 self._brief(brief), "Return the JSON object described in your instructions."]
        tag = f"{cid}-review" + ("-escalated" if escalate else "")
        self.escalations += bool(escalate)
        try:
            value, meta = self.agents.call("reviewer", spec, "\n\n".join(p for p in parts if p), tag)
        except AgentError as exc:
            # Fail closed: without a review there is no approval.
            return {"candidate_id": cid, "verdict": "unavailable", "error": str(exc),
                    "next": "retry the review, escalate, or give up; the candidate cannot be committed unreviewed"}
        verdict = str(value.get("verdict", "")).strip()
        if verdict not in ("approve", "request_changes", "reject"):
            verdict = "request_changes"
        review = {"candidate_id": cid, "verdict": verdict, "category": str(value.get("category", "none")),
                  "summary": str(value.get("summary", ""))[:1200],
                  "comments": [c for c in value.get("comments", []) if isinstance(c, dict)][:12],
                  "model": spec["model"], "same_model_as_implementer": spec["model"] == cand["model"], "escalated": bool(escalate),
                  "seconds": meta["seconds"]}
        cand["review"] = review
        cand.setdefault("reviews", []).append(review)
        rd["reviews"].append(review)
        cand["status"] = {"approve": "approved", "request_changes": "changes_requested", "reject": "rejected"}[verdict]
        if verdict != "approve":
            comments = "; ".join(f"L{c.get('line')}: {str(c.get('comment'))[:200]}" for c in review["comments"][:5])
            rd["notes"].append(f"reviewer {verdict} on {cid} ({review['category']}): {review['summary'][:300]}"
                               + (f" | {comments}" if comments else "") + f" (tried: {cand['hypothesis'][:160]})")
        if review["category"] == "false_positive":
            self.k.suggest_false_positive(rd["finding"], review["summary"], f"reviewer:{spec['model']}")
        (rd["work"] / f"{cid}.review.json").write_text(json.dumps(review, indent=2))
        self.events.emit("review", verdict, candidate=cid, model=spec["model"], escalated=bool(escalate))
        nxt = {"approve": "aqua commit <candidate>",
               "request_changes": "delegate implementers again (the comments are passed on), or give up",
               "reject": "try another candidate or give up"}[verdict]
        return {**review, "next": nxt}

    # ---- reporter --------------------------------------------------------
    def _delegate_reporter(self, brief):
        if not self.finished:
            raise SessionError("run `aqua finish` before delegating the reporter")
        if self.reported:
            return {"report": str(self.reported), "note": "already written"}
        evidence = self.evidence
        compact = report.for_reporter(evidence)
        prompt = "\n\n".join(p for p in [
            "Session evidence (JSON, authoritative):\n" + _fence(compact),
            self._brief(brief), "Write the report described in your instructions."] if p)
        narrative, meta, error = None, None, None
        try:
            narrative, meta = self.agents.call("reporter", self.cfg["agents"]["reporter"], prompt, "reporter",
                                               cwd=self.worktree if not self.closed else self.run_dir)
        except AgentError as exc:
            error = str(exc)
        text, missing = report.compose(narrative, evidence, error)
        path = self.run_dir / "report.md"
        path.write_text(text)
        self.reported = path
        evidence["agent_usage"] = self.agents.usage
        (self.run_dir / "decision.json").write_text(json.dumps(evidence, indent=2, default=str))
        self.events.emit("report", "written" if narrative else "fallback", path=str(path), missing=missing)
        self.close()
        return {"report": str(path), "reporter_model": meta and meta["model"], "error": error,
                "candidates_not_mentioned_by_reporter": missing,
                "next": "tell the human where the report is; they accept/reject with "
                        "`python3 loop/aqua_loop.py feedback <candidate-id> accept|reject`"}

    # ======================================================================
    # validation and commit
    # ======================================================================
    def evaluate(self, candidate_ids=None):
        self._need_session(open_round=True)
        rd = self.round
        ids = candidate_ids or [c for c in rd["candidates"] if self.candidates[c]["status"] == "proposed"]
        if not ids:
            raise SessionError("no proposed candidates to evaluate; delegate implementers first")
        self._check_fingerprint()
        out = []
        for cid in ids:
            cand = self._candidate(cid, current_round=True)
            if cand["status"] != "proposed":
                out.append({"candidate_id": cid, "status": cand["status"], "note": "already evaluated or failed"})
                continue
            cdir = rd["work"] / cid
            cdir.mkdir(exist_ok=True)
            base = (self.worktree / rd["finding"]["path"]).read_text()
            res = self.cascade.evaluate(rd["finding"], rd["before"], cand["edits"], cdir)
            cand["new_source"] = res.pop("new_source", None)
            cand["base_sha"] = hashlib.sha256(base.encode()).hexdigest()
            cand["result"] = res
            cand["status"] = "passed" if res["passed"] else "failed"
            (cdir / "result.json").write_text(json.dumps(res, indent=2))
            if res.get("diff"):
                (cdir / "candidate.diff").write_text(res["diff"])
            if not res["passed"]:
                rd["notes"].append(res["note"][:500] + f" (tried: {cand['hypothesis'][:160]})")
            self.events.emit("validate", "pass" if res["passed"] else f"fail_{res.get('failed_at')}",
                             candidate=cid, model=cand["model"], note=res.get("note", "")[:200])
            out.append({"candidate_id": cid, "model": cand["model"], "passed": res["passed"],
                        "failed_at": res.get("failed_at"), "note": res.get("note"),
                        "changed_lines": res.get("changed_lines"),
                        "delta_q_on_file": {m: d for m, d in (res.get("delta") or {}).items() if d}})
        self._check_fingerprint()
        passed = sorted((o for o in out if o.get("passed")),
                        key=lambda o: (sum(o["delta_q_on_file"].values()), o["changed_lines"]))
        return {"results": out, "ranking": [o["candidate_id"] for o in passed],
                "next": "delegate the reviewer for the best-ranked candidate" if passed
                else "delegate implementers again (rejection notes are passed on) or give up"}

    def commit(self, cid):
        self._need_session(open_round=True)
        rd = self.round
        cand = self._candidate(cid, current_round=True)
        res = cand.get("result") or {}
        # I2: two keys, on exactly this content
        if not res.get("passed") or cand.get("new_source") is None:
            raise SessionError(f"{cid} did not pass F1-F5")
        if cand["status"] != "approved":
            raise SessionError(f"{cid} is '{cand['status']}': it needs an 'approve' review first")
        self._check_fingerprint()
        path = self.worktree / rd["finding"]["path"]
        if hashlib.sha256(path.read_text().encode()).hexdigest() != cand["base_sha"]:
            raise SessionError("the file changed since the candidate was measured")
        path.write_text(cand["new_source"])
        cand["status"] = "committed"
        finding, no = rd["finding"], rd["round"]
        patch = self.run_dir / "series" / f"{no:02d}-{finding['id']}.diff"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text(res["diff"])
        human_id = f"{self.run_id}-r{no:02d}"
        self.k.add_play(finding, cand["hypothesis"], res["diff"])
        self.k.add_candidate(human_id, {
            "run": self.run_id, "finding": finding, "characteristic": finding["iso_characteristic"],
            "patch": str(patch), "delta": res["delta"], "model": cand["model"], "hypothesis": cand["hypothesis"],
            "filters": res["filters"], "review": cand["review"]})
        # incremental MONITOR: q is additive over files, so swap in the re-measured file
        after = self.cascade.snapshot(finding["path"], rd["work"] / "probe-after")
        rel = finding["path"]
        self.snap = {"findings": [f for f in self.snap["findings"] if f["path"] != rel] + after["findings"],
                     "functions": [f for f in self.snap["functions"] if f["path"] != rel] + after["functions"]}
        q_before = self.q
        self.q = global_q(self.snap, self.cfg["probes"]["ccn_limit"])
        self.findings, _ = analyze(self.snap["findings"], self.cfg["scope"], self.k)
        self.fingerprint = self._fingerprint()
        delta = {m: self.q[m] - q_before[m] for m in self.q if self.q[m] != q_before[m]}
        self._close_round("committed", f"{cid} committed", reward=True, committed={
            "candidate_id": human_id, "session_candidate": cid, "model": cand["model"],
            "hypothesis": cand["hypothesis"], "diff": res["diff"], "patch": str(patch),
            "filters": res["filters"], "review": cand["review"], "delta_q_global": delta})
        return {"committed": human_id, "patch": str(patch), "delta_q_global": delta,
                "q": probes.by_characteristic(self.q),
                "next": "plan the next round, or `aqua finish` when the budget is used"}

    def finish(self):
        self._need_session()
        if self.finished:
            return {"finished": True, "next": "delegate the reporter"}
        if self.round:
            self._close_round("unresolved", "session finished with the round still open", reward=True)
        self.finished = True
        self.evidence = report.evidence(self)
        (self.run_dir / "decision.json").write_text(json.dumps(self.evidence, indent=2, default=str))
        self.events.emit("session", "finished", committed=len(self.evidence["committed"]))
        return {"finished": True, "committed": [c["candidate_id"] for c in self.evidence["committed"]],
                "q_delta": {m: d for m, d in self.evidence["q_delta"].items() if d},
                "next": "delegate the reporter (role=reporter) to write the human report"}

    # ======================================================================
    # guards
    # ======================================================================
    def _need_session(self, open_round=None):
        if not self.run_id:
            raise SessionError("no session; run `aqua start` first")
        if self.finished:
            raise SessionError("the session is finished; only the reporter can run now")
        if open_round is True and not self.round:
            raise SessionError("no open round; run `aqua open-round` first")
        if open_round is False and self.round:
            raise SessionError(f"round {self.round['round']} is still open; commit or give up first")

    def _candidate(self, cid, current_round=False):
        cand = self.candidates.get(cid)
        if not cand:
            raise SessionError(f"unknown candidate {cid}")
        if current_round and (not self.round or cand["round"] != self.round["round"]):
            raise SessionError(f"{cid} does not belong to the open round")
        return cand

    def _fingerprint(self):
        """Hash of the component's state in the worktree (tracked diff + untracked files)."""
        comp = self.cfg["scope"]["component"]
        diff = git(["diff", "HEAD", "--", comp], self.worktree, 300).stdout
        status = git(["status", "--porcelain", "--untracked-files=all", "--", comp], self.worktree, 300).stdout
        return hashlib.sha256((diff + "\0" + status).encode()).hexdigest()

    def _check_fingerprint(self):
        # I1: anything that is not the controller writing to the worktree invalidates the measurements.
        if self._fingerprint() != self.fingerprint:
            self.events.emit("guard", "tamper", run=self.run_id)
            raise SessionError("the candidate worktree was modified outside the controller (I1); "
                               "the session refuses to continue. Finish it and start a new one.")

    def _round_view(self):
        rd = self.round
        return {"round": rd["round"], "finding": finding_view(rd["finding"]), "followed_plan": rd["followed_plan"],
                "localized": bool(rd["localization"]),
                "implementer_calls_left": int(self.s["attempts_per_round"]) - rd["implement_calls"],
                "candidates": {c: self.candidates[c]["status"] for c in rd["candidates"]},
                "notes": rd["notes"][-6:]}
