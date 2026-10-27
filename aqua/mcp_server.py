"""MCP server that exposes AQUA's deterministic probes to OpenCode agents.

The LOCALIZE agent calls these tools to observe the finding on the candidate
worktree (ReAct: reason -> act with a tool -> observe). Tools are read-only:
they run linters, never edit files.

Launched by OpenCode with the project venv's python (needs the `mcp` package):
    AQUA_REPO=<worktree> AQUA_CCN_LIMIT=15 .venv/bin/python loop/aqua/mcp_server.py
"""
import json
import os
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import probes  # noqa: E402

REPO = Path(os.environ["AQUA_REPO"]).resolve()
SCOPE = os.environ.get("AQUA_SCOPE", "browser/extensions/newtab").rstrip("/")
CCN_LIMIT = int(os.environ.get("AQUA_CCN_LIMIT", "15"))
TIMEOUT = int(os.environ.get("AQUA_PROBE_TIMEOUT", "600"))

server = FastMCP("aqua-probes")


def _checked(path):
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not str(pure).startswith(SCOPE + "/"):
        raise ValueError(f"path must be a relative file under {SCOPE}/")
    if not (REPO / pure).is_file():
        raise ValueError(f"no such file: {path}")
    return str(pure)


@server.tool()
def probe_file(path: str) -> str:
    """Run ESLint (mach lint), Semgrep and Lizard on one file. Returns the findings
    and the quality vector q for that file. Slow (up to a minute): call it at most once."""
    rel = _checked(path)
    snap = probes.snapshot(REPO, rel, CCN_LIMIT, TIMEOUT)
    return json.dumps({"q": snap["q"], "findings": [f for f in snap["findings"] if f["path"] == rel]}, indent=1)


@server.tool()
def function_complexity(path: str) -> str:
    """List every function in a JS file with its cyclomatic complexity (fast)."""
    rel = _checked(path)
    functions, _ = probes.lizard(REPO, rel, CCN_LIMIT, TIMEOUT)
    return json.dumps(sorted(functions, key=lambda f: -f["ccn"]), indent=1)


if __name__ == "__main__":
    server.run()
