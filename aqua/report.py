"""Session evidence and the human report.

`evidence()` is the authoritative record of a session (written as decision.json).
The REPORTER agent turns it into prose for the human reviewer; `compose()` then
appends the controller's own evidence tables, so every number a human relies on
comes from measurements, not from the model (invariant I5).
"""
import probes


def limitations(cfg):
    out = ["Committed candidates are patch files for human review; nothing is applied to the user's checkout "
           "or submitted.",
           "q covers lint, Semgrep and Lizard measures only; performance efficiency (Perfherder/Talos) is not "
           "measured.",
           "F5 uses static guards; mutation-score checking is not implemented."]
    if not cfg["cascade"]["tests"].get("enabled"):
        out.append("F4 tests were disabled (newtab node_modules not installed), so functional correctness rests on "
                   "parse checks and the reviewer agent only.")
    a = cfg["agents"]
    models = {s["model"] for s in [a["localizer"], a["reporter"], *a["implementers"], *a["reviewers"]]}
    if len(models) == 1:
        out.append(f"Every subagent ran on one model ({models.pop()}): the implementer ensemble varies only by "
                   "sampling, and the reviewer is independent by context, not by model.")
    return out


def evidence(s):
    q_delta = {m: s.q[m] - s.q_start[m] for m in s.q}
    committed = [r for r in s.rounds if r["status"] == "committed"]
    return {
        "run_id": s.run_id, "driver": s.driver, "baseline": s.baseline,
        "q_start": s.q_start, "q_end": s.q, "q_delta": q_delta,
        "q_by_characteristic": {"start": probes.by_characteristic(s.q_start), "end": probes.by_characteristic(s.q)},
        "rounds": s.rounds,
        "committed": [{k: r[k] for k in ("candidate_id", "round", "finding", "model", "hypothesis", "patch", "diff",
                                         "filters", "review", "delta_q_global")} for r in committed],
        "candidates": {cid: {k: v for k, v in c.items() if k not in ("new_source", "edits")}
                       for cid, c in s.candidates.items()},
        "bandit": s.k.bandit,
        "budgets": {"rounds": s.rounds_budget, "attempts_per_round": s.s["attempts_per_round"],
                    "max_agent_calls": s.s["max_agent_calls"], "escalation_budget": s.cfg["agents"].get("escalation_budget", 0),
                    "escalations_used": s.escalations},
        "agent_usage": s.agents.usage,
        "limitations": limitations(s.cfg),
        "automatic_adoption": False,
    }


def for_reporter(ev, diff_chars=6000):
    """The evidence, trimmed to what a report needs (diffs capped, raw filter detail dropped)."""
    def cand(c):
        res = c.get("result") or {}
        return {"candidate_id": c["candidate_id"], "model": c["model"], "status": c["status"],
                "hypothesis": c.get("hypothesis"), "error": c.get("error"),
                "failed_at": res.get("failed_at"), "note": res.get("note"),
                "changed_lines": res.get("changed_lines"),
                "reviews": [{k: r[k] for k in ("verdict", "category", "summary", "comments", "model", "escalated")}
                            for r in c.get("reviews", [])]}
    rounds = []
    for r in ev["rounds"]:
        loc = r.get("localization") or {}
        rounds.append({
            "round": r["round"], "finding": r["finding"], "status": r["status"], "reason": r["reason"],
            "seconds": r["seconds"], "orchestrator_rationale": r["rationale"], "followed_planner": r["followed_plan"],
            "planner_suggestion": (r.get("plan") or {}).get("finding_id"),
            "localization": {k: loc.get(k) for k in ("summary", "risks", "strategy", "false_positive", "model")}
            if loc else None,
            "candidates": [cand(ev["candidates"][c]) for c in r["candidates"]],
            "committed_as": r.get("candidate_id")})
    return {
        "run_id": ev["run_id"], "orchestration": ev["driver"],
        "q_by_characteristic": ev["q_by_characteristic"],
        "q_delta_nonzero": {m: d for m, d in ev["q_delta"].items() if d},
        "measure_to_characteristic": probes.MEASURES,
        "rounds": rounds,
        "committed_patches": [{**{k: c[k] for k in ("candidate_id", "round", "model", "hypothesis", "review",
                                                    "delta_q_global")},
                               "finding": c["finding"],
                               "filters": [{k: f[k] for k in ("id", "status")} for f in c["filters"]],
                               "diff": c["diff"][:diff_chars]} for c in ev["committed"]],
        "budgets": ev["budgets"], "agent_usage": ev["agent_usage"], "limitations": ev["limitations"],
    }


def compose(narrative, ev, error=None):
    """Reporter prose + controller evidence. Returns (markdown, candidate ids the prose never mentions)."""
    missing = [c["candidate_id"] for c in ev["committed"] if narrative and c["candidate_id"] not in narrative]
    head = []
    if not narrative:
        head = [f"# AQUA session {ev['run_id']}", "",
                f"> The reporter agent was unavailable ({error}); only the controller's evidence follows.", ""]
    elif missing:
        head = [f"> Controller note: the reporter did not mention {', '.join(missing)}; see the evidence below.", ""]
    body = (narrative.strip() + "\n\n") if narrative else ""
    return "\n".join(head) + body + appendix(ev), missing


def appendix(ev):
    lines = ["---", "", "## Evidence (generated by the controller, not by a model)", "",
             f"Run `{ev['run_id']}`, orchestration: {ev['driver']}. Baseline `{ev['baseline']['revision'][:12]}` "
             f"(local changes sha256 {ev['baseline']['overlay_sha256'][:12]}).", "",
             "### Quality vector q (lower is better)", "",
             "| measure | ISO/IEC 25010 characteristic | start | end | Δ |", "|---|---|--:|--:|--:|"]
    for m in ev["q_start"]:
        lines.append(f"| {m} | {probes.MEASURES[m]} | {ev['q_start'][m]} | {ev['q_end'][m]} | {ev['q_delta'][m]:+d} |")
    lines += ["", "### Rounds", "", "| # | finding | planner followed | outcome | candidates | seconds |",
              "|--:|---|---|---|--:|--:|"]
    for r in ev["rounds"]:
        f = r["finding"]
        lines.append(f"| {r['round']} | `{f['rule']}` {f['path'].split('/')[-1]}:{f.get('line')} "
                     f"{f.get('function') or ''} | {'yes' if r['followed_plan'] else 'no'} | {r['status']}"
                     f"{' (' + r['candidate_id'] + ')' if r.get('candidate_id') else ''} | {len(r['candidates'])} "
                     f"| {r['seconds']} |")
    lines += ["", "### Candidates", "", "| candidate | model | outcome | review |", "|---|---|---|---|"]
    for cid, c in ev["candidates"].items():
        res = c.get("result") or {}
        outcome = c["status"] if res.get("passed") or not res else f"{c['status']} at {res.get('failed_at')}"
        reviews = ", ".join(f"{r['verdict']} by {r['model']}" for r in c.get("reviews", [])) or "-"
        lines.append(f"| {cid} | {c['model']} | {outcome} | {reviews} |")
    lines += ["", "### Committed patches", ""]
    for c in ev["committed"]:
        filters = ", ".join(f"{f['id']}:{f['status']}" for f in c["filters"])
        lines.append(f"- **{c['candidate_id']}** `{c['patch']}`: filters {filters}; review "
                     f"{c['review']['verdict']} by {c['review']['model']}; Δq {c['delta_q_global']}")
    if not ev["committed"]:
        lines.append("- none")
    u = ev["agent_usage"]
    lines += ["", "### Agents", "", f"{u['calls']} subagent calls ({u['failed']} failed, {ev['budgets']['escalations_used']} reviewer escalations), "
              f"{u['seconds']} s total.", "", "| model | calls | failed | seconds |", "|---|--:|--:|--:|"]
    for model, row in u["by_model"].items():
        lines.append(f"| {model} | {row['calls']} | {row['failed']} | {row['seconds']} |")
    lines += ["", "### Bandit posteriors", "", "| arm | α | β | mean | pulls |", "|---|--:|--:|--:|--:|"]
    for arm, st in ev["bandit"].items():
        lines.append(f"| {arm} | {st['alpha']} | {st['beta']} | {st['alpha'] / (st['alpha'] + st['beta']):.2f} "
                     f"| {st['pulls']} |")
    lines += ["", "### Human decision", "",
              "Record a verdict per patch (feeds the planner's posteriors):", "", "```sh"]
    lines += [f"python3 loop/aqua_loop.py feedback {c['candidate_id']} accept   # or reject" for c in ev["committed"]]
    lines += ["```", "", "### Limitations", ""] + [f"- {x}" for x in ev["limitations"]]
    return "\n".join(lines) + "\n"
