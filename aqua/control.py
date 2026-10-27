"""The AQUA control daemon: one Session behind a local Unix socket.

The orchestrator agent runs the `aqua` skill (loop/skills/aqua/SKILL.md). Its
script (skills/aqua/scripts/aqua) starts this daemon on first use and sends it one
JSON request per command. The Session lives here, in one process, so the
invariants in session.py hold whatever the agent does with its shell: this process
is the only writer of the candidate worktree (I1).

Slow operations (probes, subagents) run as background jobs: a request waits up to
~40 s and then returns {"job": ..., "status": "running"}; collect it with `wait`.
Jobs that write to the worktree never overlap with other jobs.

The daemon exits on `stop`, or after `session.idle_minutes` without requests; it
closes the session (removing the worktree unless kept) on the way out.

    .venv/bin/python loop/aqua/control.py serve --agent <harness>   (started by the script)
"""
import argparse
import json
import os
import socketserver
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from agents import AgentError  # noqa: E402
from knowledge import Knowledge  # noqa: E402
from runtime import LoopError  # noqa: E402
from session import Session, SessionError  # noqa: E402

LOOP_ROOT = Path(__file__).resolve().parents[1]
SOCKET = LOOP_ROOT / "artifacts" / "aqua.sock"
INLINE_WAIT = 40


def make_session(driver):
    cfg = yaml.safe_load(Path(os.environ.get("AQUA_CONFIG", LOOP_ROOT / "aqua.yml")).read_text())
    knowledge = Knowledge(os.environ.get("AQUA_KNOWLEDGE", LOOP_ROOT / "knowledge"),
                          LOOP_ROOT / "accepted-false-positives.yml",
                          cfg["characteristics"].keys(), cfg["bandit"]["prior"])
    return Session(cfg, os.environ.get("AQUA_FIREFOX", LOOP_ROOT.parent / "firefox"), knowledge,
                   os.environ.get("AQUA_ARTIFACTS", LOOP_ROOT / "artifacts"), driver=driver)


def _guarded(fn):
    try:
        return fn()
    except (SessionError, AgentError, LoopError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # report, do not kill the daemon
        traceback.print_exc(file=sys.stderr)
        return {"error": f"{type(exc).__name__}: {exc}"}


class Jobs:
    """Background jobs. 'write' jobs (probes on the worktree) run alone; 'read' jobs
    (subagents, which only read the worktree) may run in parallel with each other."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.jobs = {}
        self.lock = threading.Lock()
        self.count = 0

    def submit(self, kind, label, fn):
        with self.lock:
            running = [(jid, j) for jid, j in self.jobs.items() if not j["future"].done()]
            if running and (kind == "write" or any(j["kind"] == "write" for _, j in running)):
                busy = ", ".join(f"{jid} ({j['label']})" for jid, j in running)
                raise SessionError(f"cannot start {label} while {busy} is running; run `aqua wait` first")
            self.count += 1
            jid = f"j{self.count}"
            self.jobs[jid] = {"kind": kind, "label": label, "future": self.pool.submit(_guarded, fn)}
        return jid

    def collect(self, ids, seconds):
        unknown = [i for i in ids if i not in self.jobs]
        if unknown:
            return {"error": f"unknown job(s): {', '.join(unknown)}"}
        wait([self.jobs[i]["future"] for i in ids], timeout=max(1, min(int(seconds), 110)))
        out = {}
        for i in ids:
            fut = self.jobs[i]["future"]
            out[i] = fut.result() if fut.done() else {"status": "running", "label": self.jobs[i]["label"]}
        return out

    def running(self):
        return {jid: j["label"] for jid, j in self.jobs.items() if not j["future"].done()}


class Controller:
    """Maps each command of the skill's script to one Session operation."""

    def __init__(self, new_session):
        self.new_session = new_session
        self.s = new_session()
        self.jobs = Jobs()
        self.stopping = threading.Event()

    def _job(self, kind, label, fn):
        try:
            jid = self.jobs.submit(kind, label, fn)
        except SessionError as exc:
            return {"error": str(exc)}
        result = self.jobs.collect([jid], INLINE_WAIT)[jid]
        if result.get("status") == "running":
            return {"job": jid, "status": "running", "label": label, "next": f"aqua wait {jid}"}
        return result

    def handle(self, op, a):
        s = self.s
        if op == "start":
            if s.closed:                    # the previous session is over; this daemon may host another
                s = self.s = self.new_session()
            s.driver = a.get("agent") or s.driver
            return self._job("write", "start", lambda: s.start(a.get("rounds") or None, bool(a.get("fresh"))))
        if op == "status":
            return _guarded(lambda: {**s.status(), "running_jobs": self.jobs.running()})
        if op == "findings":
            return _guarded(lambda: s.list_findings(a.get("characteristic") or None, int(a.get("limit") or 10)))
        if op == "plan":
            return _guarded(s.plan)
        if op == "open-round":
            fid = a["finding_id"]
            return self._job("write", f"open-round {fid}", lambda: s.open_round(fid, a.get("rationale", "")))
        if op == "delegate":
            role = a["role"]
            return self._job("read", f"delegate {role}", lambda: s.delegate(
                role, a.get("brief", ""), a.get("candidate_id") or None, a.get("n") or None,
                a.get("models") or None, bool(a.get("escalate"))))
        if op == "evaluate":
            return self._job("write", "evaluate", lambda: s.evaluate(a.get("candidate_ids") or None))
        if op == "candidate":
            return _guarded(lambda: s.candidate(a["candidate_id"]))
        if op == "commit":
            cid = a["candidate_id"]
            return self._job("write", f"commit {cid}", lambda: s.commit(cid))
        if op == "give-up":
            return self._job("write", "give-up", lambda: s.give_up(a.get("reason", "")))
        if op == "finish":
            return self._job("write", "finish", s.finish)
        if op == "wait":
            return self.jobs.collect(a.get("job_ids") or [], a.get("seconds") or 90)
        if op == "stop":
            if self.jobs.running():
                return {"error": f"jobs still running: {self.jobs.running()}; `aqua wait` first"}
            self.stopping.set()
            return {"stopped": True, "run_id": s.run_id, "run_dir": str(s.run_dir) if s.run_id else None}
        return {"error": f"unknown command {op!r}"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        ctl = self.server.controller
        self.server.last_request = time.monotonic()
        try:
            req = json.loads(self.rfile.readline() or b"{}")
            out = ctl.handle(req.get("op", ""), req.get("args") or {})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            out = {"error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(out, default=str) + "\n").encode())
        self.server.last_request = time.monotonic()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(controller, sock_path=SOCKET, idle_minutes=120):
    sock_path = Path(sock_path)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    server = _Server(str(sock_path), _Handler)
    os.chmod(sock_path, 0o600)
    server.controller, server.last_request = controller, time.monotonic()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    try:
        while not controller.stopping.wait(5):
            idle = time.monotonic() - server.last_request
            if idle > idle_minutes * 60 and not controller.jobs.running():
                print(f"[aqua] idle for {idle_minutes} min, stopping", file=sys.stderr)
                break
    finally:
        time.sleep(0.5)                     # let the `stop` reply go out
        server.shutdown()
        sock_path.unlink(missing_ok=True)
        if controller.s.run_id:
            controller.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--agent", default="unknown", help="recorded as the session's driver")
    ap.add_argument("--socket", default=str(SOCKET))
    args = ap.parse_args()
    ctl = Controller(lambda: make_session(args.agent))
    serve(ctl, args.socket, ctl.s.cfg["session"].get("idle_minutes", 120))


if __name__ == "__main__":
    main()
