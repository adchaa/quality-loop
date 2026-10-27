#!/usr/bin/env python3
"""AQUA — Assured QUality Adaptation loop for Firefox (ISO/IEC 25010:2023).

The main mode is agentic: in a conversation with the agent of your choice (started
from T3 Code), invoke the `aqua` skill (loop/skills/aqua/SKILL.md). That agent is the
orchestrator; it drives the session through the skill's script.

  python3 loop/aqua_loop.py check                 preflight: tools, OpenCode models, venv, skill
  python3 loop/aqua_loop.py monitor               MONITOR + ANALYZE only (no models)
  python3 loop/aqua_loop.py plan                  what the planner suggests now (no models)
  python3 loop/aqua_loop.py run [--rounds 3]      scripted driver: same tools, fixed order
                                                  (the baseline for orchestrator ablations)
  python3 loop/aqua_loop.py status                bandit posteriors, ledger, candidates
  python3 loop/aqua_loop.py feedback ID accept|reject   human verdict on a candidate
  python3 loop/aqua_loop.py false-positive FINDING_ID --reason "..."

Architecture: loop/aqua-architecture.html. Nothing is ever applied to the user's
Firefox checkout or submitted; committed candidates are patch files for review.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

LOOP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(LOOP_ROOT / "aqua"))

import yaml  # noqa: E402

import probes  # noqa: E402
import session as sessions  # noqa: E402
from analyze import analyze, characteristic_counts  # noqa: E402
from knowledge import Knowledge  # noqa: E402
from plan import Planner  # noqa: E402
from runtime import Events, LoopError  # noqa: E402
from session import Session, SessionError  # noqa: E402

VENV_PYTHON = LOOP_ROOT.parent / ".venv" / "bin" / "python"
SKILL_SCRIPT = LOOP_ROOT / "skills" / "aqua" / "scripts" / "aqua"
# Where Claude Code, Codex and OpenCode look for project skills (T3 Code starts any of them).
SKILL_LINKS = [root / d / "skills" / "aqua" for root in (LOOP_ROOT.parent, LOOP_ROOT)
               for d in (".claude", ".agents", ".opencode")]


def load(args):
    cfg = yaml.safe_load(Path(args.config).read_text())
    knowledge = Knowledge(args.knowledge, LOOP_ROOT / "accepted-false-positives.yml",
                          cfg["characteristics"].keys(), cfg["bandit"]["prior"])
    return cfg, knowledge


def cmd_check(args):
    cfg, _ = load(args)
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'ok' if good else 'FAIL'}] {name} {detail}")

    root = Path(args.firefox_root)
    report("firefox checkout", (root / "mach").exists(), str(root))
    for name in ("semgrep", "lizard"):
        report(name, Path(probes.tool(name)).exists() or shutil.which(name), probes.tool(name))
    report("node (F1 parse check)", shutil.which("node"))
    a = cfg["agents"]
    specs = [a["localizer"], a["reporter"], *a["implementers"], *a["reviewers"]]
    if a.get("reviewer_escalation"):
        specs.append(a["reviewer_escalation"])
    report("opencode", shutil.which("opencode"), shutil.which("opencode") or "")
    if shutil.which("opencode"):
        models = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=120,
                                stdin=subprocess.DEVNULL).stdout.split()
        for model in sorted({s["model"] for s in specs}):
            report(f"opencode model {model}", model in models)
    deps = subprocess.run([str(VENV_PYTHON), "-c", "import mcp.server.fastmcp, yaml"], capture_output=True)
    report("venv deps (mcp for the probe tools, pyyaml)", deps.returncode == 0)
    imp = subprocess.run([str(VENV_PYTHON), "-c", "import sys; sys.path.insert(0, 'aqua'); "
                          "import control, session, report, agents"], cwd=LOOP_ROOT, capture_output=True, text=True)
    report("control daemon modules import", imp.returncode == 0, imp.stderr.strip()[-200:])
    for link in SKILL_LINKS:
        report(f"skill link {link.relative_to(LOOP_ROOT.parent)}", (link / "SKILL.md").exists())
    report("skill script is executable", os.access(SKILL_SCRIPT, os.X_OK))
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                            capture_output=True, text=True).stdout
    report("no untracked files in firefox", not any(l.startswith("??") for l in status.splitlines()))
    return 0 if ok else 1


def cmd_monitor(args):
    cfg, knowledge = load(args)
    events = Events(Path(args.knowledge) / "monitor-events.jsonl")
    snap = sessions.load_or_monitor(cfg, args.firefox_root, knowledge, Path(args.knowledge) / "monitor-logs",
                                 not args.fresh, events)
    findings, dropped = analyze(snap["findings"], cfg["scope"], knowledge)
    q = sessions.global_q(snap, cfg["probes"]["ccn_limit"])
    print(json.dumps({"q": probes.by_characteristic(q), "eligible_findings": characteristic_counts(findings),
                      "dropped": dropped}, indent=2))
    return 0


def cmd_plan(args):
    cfg, knowledge = load(args)
    cache = Path(args.knowledge) / "monitor-latest.json"
    if not cache.exists():
        raise LoopError("run `monitor` first")
    findings, _ = analyze(json.loads(cache.read_text())["snapshot"]["findings"], cfg["scope"], knowledge)
    planner = Planner(knowledge, cfg["session"]["max_failures_per_finding"], cfg["bandit"].get("seed"),
                      cfg["bandit"].get("arm_override"))
    finding, decision = planner.choose(findings, set())
    print(json.dumps({"decision": decision, "finding": finding}, indent=2))
    return 0


def cmd_run(args):
    """Scripted driver: the orchestrator's protocol in a fixed order, no LLM decisions.
    It calls the same Session operations as the agent, so the two are directly comparable."""
    cfg, knowledge = load(args)
    if args.policy:
        cfg["bandit"]["arm_override"] = None if args.policy == "thompson" else args.policy
    if args.keep_worktree:
        cfg["session"]["keep_worktree"] = True
    s = Session(cfg, args.firefox_root, knowledge, args.artifacts, driver="scripted")
    s.start(args.rounds, args.fresh_monitor)
    try:
        while s.status()["budgets"]["rounds_left"] > 0:
            suggestion = s.plan()["suggestion"]
            if not suggestion:
                break
            if not s.open_round(suggestion["id"], "scripted driver: followed the planner")["reproduced"]:
                continue
            s.delegate("localizer")
            if not scripted_round(s, cfg, args.candidates):
                s.give_up("scripted driver: implementer attempts exhausted")
    except SessionError as exc:            # a budget ran out
        print(f"aqua: stopping early: {exc}", file=sys.stderr)
    s.finish()
    result = s.delegate("reporter") if not args.no_report else None
    s.close()
    print((s.run_dir / "report.md").read_text() if result else json.dumps(s.evidence["q_delta"], indent=2))
    return 0


def scripted_round(s, cfg, n):
    for _ in range(cfg["session"]["attempts_per_round"]):
        proposed = s.delegate("implementer", n=n)
        if not any(c["status"] == "proposed" for c in proposed["candidates"]):
            continue
        for cid in s.evaluate()["ranking"]:
            verdict = s.delegate("reviewer", candidate_id=cid).get("verdict")
            if verdict == "approve":
                s.commit(cid)
                return True
            if verdict == "request_changes":
                break                       # revise: the comments go to the next implementer call
    return False


def cmd_status(args):
    cfg, knowledge = load(args)
    print("Bandit posteriors (Beta):")
    for arm, s in knowledge.bandit.items():
        print(f"  {arm:<24} α={s['alpha']:<3} β={s['beta']:<3} mean={s['alpha'] / (s['alpha'] + s['beta']):.2f} "
              f"pulls={s['pulls']} accepted={s['accepted']}")
    print("\nLedger (last 8):")
    for row in knowledge.ledger()[-8:]:
        print(f"  {row['time']} run={row['run'][-6:]} round={row['round']} q={row['q']} {row['note'][:70]}")
    print("\nCandidates:")
    for cid, c in knowledge.candidates.items():
        print(f"  {cid}  {c['characteristic']:<22} {c['finding']['rule']:<28} human={c.get('human', 'pending')}  {c['patch']}")
    return 0


def cmd_feedback(args):
    _, knowledge = load(args)
    knowledge.human_feedback(args.candidate_id, args.verdict)
    print(f"recorded {args.verdict} for {args.candidate_id}")
    return 0


def cmd_false_positive(args):
    cfg, knowledge = load(args)
    cache = Path(args.knowledge) / "monitor-latest.json"
    findings, _ = analyze(json.loads(cache.read_text())["snapshot"]["findings"], cfg["scope"], knowledge)
    match = [f for f in findings if f["id"] == args.finding_id]
    if not match:
        raise LoopError(f"no eligible finding {args.finding_id} in the latest MONITOR snapshot")
    knowledge.confirm_false_positive(match[0], args.reason)
    print(f"added to {knowledge.fp_file}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(LOOP_ROOT / "aqua.yml"))
    ap.add_argument("--firefox-root", default=str(LOOP_ROOT.parent / "firefox"))
    ap.add_argument("--knowledge", default=str(LOOP_ROOT / "knowledge"))
    ap.add_argument("--artifacts", default=str(LOOP_ROOT / "artifacts"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    m = sub.add_parser("monitor")
    m.add_argument("--fresh", action="store_true", help="ignore the cached snapshot")
    m.set_defaults(fn=cmd_monitor)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)
    r = sub.add_parser("run", help="scripted driver (baseline)")
    r.add_argument("--rounds", type=int)
    r.add_argument("--candidates", type=int, help="N candidates per implementer call")
    r.add_argument("--policy", choices=["thompson", "random", "round_robin"], help="planner ablation")
    r.add_argument("--keep-worktree", action="store_true")
    r.add_argument("--fresh-monitor", action="store_true")
    r.add_argument("--no-report", action="store_true", help="skip the reporter agent")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    f = sub.add_parser("feedback")
    f.add_argument("candidate_id")
    f.add_argument("verdict", choices=["accept", "reject"])
    f.set_defaults(fn=cmd_feedback)
    fp = sub.add_parser("false-positive")
    fp.add_argument("finding_id")
    fp.add_argument("--reason", required=True)
    fp.set_defaults(fn=cmd_false_positive)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LoopError as exc:
        sys.exit(f"aqua: {exc}")
