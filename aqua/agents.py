"""Subagent runner: the workers the orchestrator delegates to.

Roles: localizer, implementer, reviewer, reporter. Each runs as a read-only
OpenCode agent (`opencode run`) defined in a per-run config (OPENCODE_CONFIG):
edit, bash, web, task and skill are denied. The model per role is set in aqua.yml.

The localizer also gets the `aqua-probes` MCP server (aqua/mcp_server.py) to run
the deterministic probes (ReAct: reason, call a tool, observe). Subagents never
change files: the controller applies proposed edits inside the cascade.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

LOOP_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = Path(__file__).resolve().parent / "prompts"
VENV_PYTHON = LOOP_ROOT.parent / ".venv" / "bin" / "python"
MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"

# format "json": the reply must contain a JSON object with the required keys.
# format "text": the reply is used as is (the reporter writes Markdown).
ROLES = {
    "localizer": {"prompt": "localizer.md", "mcp": True, "format": "json",
                  "required": ["region", "summary", "strategy"]},
    "implementer": {"prompt": "implementer.md", "mcp": False, "format": "json",
                    "required": ["edits", "hypothesis"]},
    "reviewer": {"prompt": "reviewer.md", "mcp": False, "format": "json",
                 "required": ["verdict", "summary"]},
    "reporter": {"prompt": "reporter.md", "mcp": False, "format": "text", "required": []},
}

READ_ONLY = {"edit": "deny", "bash": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny",
             "skill": "deny"}


class AgentError(RuntimeError):
    pass


class AgentTimeout(AgentError):
    """Not retried: a model that is too slow once is usually too slow twice."""


def mcp_env(worktree, cfg):
    p = cfg["probes"]
    return {"AQUA_REPO": str(worktree), "AQUA_SCOPE": cfg["scope"]["component"],
            "AQUA_CCN_LIMIT": str(p["ccn_limit"]), "AQUA_PROBE_TIMEOUT": str(p["timeout_seconds"]),
            "AQUA_SEMGREP_CONFIG": str(p.get("semgrep") or "auto")}


GLOBAL_OPENCODE = [Path.home() / ".config" / "opencode" / n for n in ("opencode.json", "opencode.jsonc")]


def _load_jsonc(path):
    text = re.sub(r"(?m)^\s*//.*$", "", path.read_text())          # whole-line comments only
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))           # and trailing commas


def foreign_mcp(paths=GLOBAL_OPENCODE):
    """The user's own MCP servers (e.g. a browser), switched off: OpenCode merges the global
    config into ours, and a subagent must not reach tools that can run code."""
    off = {}
    for path in paths:
        try:
            servers = _load_jsonc(path).get("mcp", {})
        except (OSError, ValueError):
            continue
        for name, spec in servers.items():
            if isinstance(spec, dict) and name != "aqua-probes":
                off[name] = {**spec, "enabled": False}
    return off


def write_opencode_config(path, worktree, cfg):
    """Per-session OpenCode config for the subagents. Subagents never get the `aqua`
    control tools (they cannot commit or plan), only the localizer gets probes, and the
    user's global MCP servers are disabled."""
    agents = {}
    for role, spec in ROLES.items():
        tools = {"aqua_*": False}
        if not spec["mcp"]:
            tools["aqua-probes_*"] = False
        agents[f"aqua-{role}"] = {"description": f"AQUA {role} (read-only)", "mode": "primary",
                                  "prompt": (PROMPTS / spec["prompt"]).read_text(),
                                  "permission": READ_ONLY, "tools": tools}
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {**foreign_mcp(), "aqua-probes": {"type": "local", "command": [str(VENV_PYTHON), str(MCP_SERVER)],
                                "environment": mcp_env(worktree, cfg), "enabled": True,
                                "timeout": int(cfg["probes"]["timeout_seconds"]) * 1000}},
        "agent": agents,
    }
    Path(path).write_text(json.dumps(config, indent=2))
    return path


def opencode_text(stdout):
    chunks, tools = [], []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part", {}) if isinstance(event, dict) else {}
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            chunks.append(str(part.get("text", "")))
        elif part.get("type") == "tool":
            tools.append(part.get("tool"))
    return ("".join(chunks).strip() if chunks else stdout.strip()), tools


def parse_json_object(text, required):
    """Return the last JSON object in `text` that has all `required` keys."""
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(k in value for k in required):
            found = value
    if found is None:
        raise AgentError(f"no JSON object with keys {sorted(required)} in model reply")
    return found


class Agents:
    def __init__(self, cfg, worktree, run_dir, events):
        self.cfg = cfg
        self.settings = cfg["agents"]
        self.worktree = Path(worktree)
        self.run_dir = Path(run_dir)
        self.model_dir = self.run_dir / "models"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.opencode_config = write_opencode_config(self.run_dir / "opencode.json", worktree, cfg)
        self.lock = threading.Lock()
        self.usage = {"calls": 0, "failed": 0, "seconds": 0.0, "by_model": {}}

    def call(self, role, spec, prompt, call_id, cwd=None):
        """Run one subagent turn. `spec` = {"model": "provider/model"}.
        Returns (parsed JSON or text, metadata)."""
        (self.model_dir / f"{call_id}.prompt.md").write_text(prompt)
        with self.lock:
            self.usage["calls"] += 1
        cwd = Path(cwd or self.worktree)
        last_error = None
        started = time.monotonic()
        for attempt in range(1 + int(self.settings.get("retries", 1))):
            try:
                text, tools = self._opencode(role, spec["model"], prompt, f"{call_id}.try{attempt}", cwd)
                if ROLES[role]["format"] == "text":
                    if not text.strip():
                        raise AgentError("empty reply")
                    value = text.strip()
                else:
                    value = parse_json_object(text, ROLES[role]["required"])
            except AgentTimeout as exc:
                last_error = str(exc)
                break
            except AgentError as exc:
                last_error = str(exc)
                continue
            meta = {"role": role, "model": spec["model"], "try": attempt,
                    "seconds": round(time.monotonic() - started, 1), "tool_calls": tools}
            self._account(spec["model"], meta["seconds"], ok=True)
            self.events.emit("agent", "reply", call=call_id, **meta)
            return value, meta
        self._account(spec["model"], time.monotonic() - started, ok=False)
        self.events.emit("agent", "failed", call=call_id, role=role, model=spec["model"], error=last_error)
        raise AgentError(f"{role} ({spec['model']}) failed: {last_error}")

    def _account(self, model, seconds, ok):
        with self.lock:
            row = self.usage["by_model"].setdefault(model, {"calls": 0, "failed": 0, "seconds": 0.0})
            row["calls"] += 1
            row["seconds"] = round(row["seconds"] + seconds, 1)
            self.usage["seconds"] = round(self.usage["seconds"] + seconds, 1)
            if not ok:
                row["failed"] += 1
                self.usage["failed"] += 1

    def _exec(self, argv, env, log_name, cwd):
        timeout = int(self.settings.get("timeout_seconds", 420))
        try:
            result = subprocess.run(argv, cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            (self.model_dir / f"{log_name}.stdout.jsonl").write_text(partial)
            raise AgentTimeout(f"{Path(argv[0]).name} timed out after {timeout}s") from None
        (self.model_dir / f"{log_name}.stdout.jsonl").write_text(result.stdout)
        (self.model_dir / f"{log_name}.stderr.txt").write_text(result.stderr)
        if result.returncode:
            raise AgentError(f"{Path(argv[0]).name} exited {result.returncode}; see models/{log_name}.stderr.txt")
        return result.stdout

    def _opencode(self, role, model, prompt, log_name, cwd):
        binary = shutil.which(self.settings.get("opencode_command", "opencode"))
        if not binary:
            raise AgentError("opencode not found on PATH")
        env = dict(os.environ, OPENCODE_CONFIG=str(self.opencode_config))
        stdout = self._exec([binary, "run", "--standalone", "--auto", "--model", model, "--agent", f"aqua-{role}",
                             "--format", "json", "--title", f"aqua {log_name}", prompt], env, log_name, cwd)
        text, tools = opencode_text(stdout)
        (self.model_dir / f"{log_name}.reply.md").write_text(text)
        return text, tools
