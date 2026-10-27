"""Run bookkeeping: event log and the isolated candidate worktree."""
import datetime as dt
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


class LoopError(RuntimeError):
    pass


def git(args, cwd, timeout=120, input_text=None):
    try:
        return subprocess.run(["git", *args], cwd=cwd, input=input_text, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise LoopError(f"git {args[0]} timed out") from exc


class Events:
    """Append-only JSONL event log; also prints one progress line per event."""

    def __init__(self, path, quiet=False):
        self.path = Path(path)
        self.quiet = quiet

    def emit(self, stage, status, **details):
        row = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "stage": stage, "status": status, **details}
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        if not self.quiet:
            short = {k: v for k, v in details.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80}
            print(f"[aqua] {stage:<9} {status:<12} {json.dumps(short)}", file=sys.stderr, flush=True)


def checkout_state(repo):
    """Revision + hash of tracked local changes; identifies what the probes measured."""
    root = git(["rev-parse", "--show-toplevel"], repo)
    if root.returncode:
        raise LoopError(f"not a Git checkout: {repo}")
    repo = Path(root.stdout.strip())
    overlay = git(["diff", "--binary", "HEAD"], repo, 120).stdout
    return {
        "repo": str(repo),
        "revision": git(["rev-parse", "HEAD"], repo).stdout.strip(),
        "overlay_sha256": hashlib.sha256(overlay.encode()).hexdigest(),
        "overlay": overlay,
    }


@contextmanager
def candidate_worktree(repo, run_dir, keep):
    """Detached worktree at HEAD plus the user's tracked changes. The user's
    checkout is never modified; untracked files make the snapshot ambiguous, so refuse."""
    state = checkout_state(repo)
    repo = Path(state["repo"])
    status = git(["status", "--porcelain", "--untracked-files=all"], repo, 120)
    if any(line.startswith("?? ") for line in status.stdout.splitlines()):
        raise LoopError("Firefox checkout has untracked files; refusing to snapshot it")
    worktree = Path(run_dir) / "worktree"
    added = git(["worktree", "add", "--detach", str(worktree), "HEAD"], repo, 1800)
    if added.returncode:
        raise LoopError(f"git worktree add failed: {added.stderr[-800:]}")
    try:
        if state["overlay"]:
            applied = git(["apply", "--binary"], worktree, 120, state["overlay"])
            if applied.returncode:
                raise LoopError("could not copy tracked local changes into the worktree")
        # ESLint's node_modules are gitignored, so a fresh worktree lacks them and
        # `mach lint` would reinstall them; reuse the checkout's (read-only use).
        if (repo / "node_modules").is_dir() and not (worktree / "node_modules").exists():
            (worktree / "node_modules").symlink_to(repo / "node_modules", target_is_directory=True)
        (Path(run_dir) / "baseline.json").write_text(json.dumps(
            {k: v for k, v in state.items() if k != "overlay"}, indent=2))
        yield worktree, state
    finally:
        if not keep:
            git(["worktree", "remove", "--force", str(worktree)], repo, 600)
