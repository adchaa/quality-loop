"""Evidence-gated orchestration for a bounded Firefox New Tab quality loop."""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath

import yaml


LOOP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = LOOP_ROOT.parent
DEFAULT_FIREFOX_ROOT = PROJECT_ROOT / "firefox"
EXCLUDED_PREFIXES = (
    "browser/extensions/newtab/prerendered/",
    "browser/extensions/newtab/test/",
    "browser/components/newtab/",
)


class LoopError(RuntimeError):
    pass


def run_command(argv, cwd, timeout, input_text=None):
    try:
        return subprocess.run(
            [str(part) for part in argv],
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LoopError(f"Command timed out after {timeout}s: {argv[0]}") from exc
    except OSError as exc:
        raise LoopError(f"Could not run {argv[0]}: {exc}") from exc


def configured_path(value):
    path = Path(value)
    if path.is_absolute():
        return path
    project_path = PROJECT_ROOT / path
    return project_path if project_path.exists() else LOOP_ROOT / path


def read_config(path):
    try:
        value = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise LoopError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"Configuration is not a mapping: {path}")
    return value


def append_event(path, stage, status, **details):
    event = {
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage": stage,
        "status": status,
        **details,
    }
    with Path(path).open("a") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def load_issues(path, scope):
    try:
        issues = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"Cannot read issue list {path}: {exc}") from exc
    if not isinstance(issues, list):
        raise LoopError("Issue input must be a JSON array")

    component = str(scope.get("component", "browser/extensions/newtab")).rstrip("/")
    accepted = []
    for raw in issues:
        if not isinstance(raw, dict):
            continue
        rel = PurePosixPath(str(raw.get("path", "")))
        path_text = rel.as_posix()
        if rel.is_absolute() or not path_text.startswith(component + "/"):
            continue
        excludes = list(scope.get("exclude", [])) + list(EXCLUDED_PREFIXES)
        if any(
            path_text == str(pattern).rstrip("*").rstrip("/")
            or path_text.startswith(str(pattern).rstrip("*").rstrip("/") + "/")
            for pattern in excludes
        ):
            continue
        if not raw.get("probe"):
            continue
        issue = dict(raw)
        if not issue.get("rule") and issue.get("probe") == "lizard":
            issue["rule"] = "lizard-ccn"
        if not issue.get("rule"):
            continue
        issue["path"] = path_text
        issue["id"] = hashlib.sha256(
            json.dumps(
                {k: issue.get(k) for k in ("probe", "path", "line", "rule", "message", "function")},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        accepted.append(issue)
    return accepted


def safe_target_path(repo, rel):
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not rel.startswith("browser/extensions/newtab/"):
        raise LoopError(f"Path is outside the New Tab scope: {rel}")
    root = Path(repo).resolve()
    target = root.joinpath(*pure.parts)
    resolved = target.resolve()
    if not resolved.is_relative_to(root) or not target.is_file() or target.is_symlink():
        raise LoopError(f"Target is missing or not a regular file in the Firefox checkout: {rel}")
    return target


def validate_issue(issue, supported_probes):
    if issue.get("probe") not in supported_probes:
        raise LoopError(f"Unsupported probe adapter: {issue.get('probe')}")
    if issue.get("iso_characteristic") not in {
        "functional_suitability",
        "performance_efficiency",
        "interaction_capability",
        "reliability",
        "security",
        "maintainability",
        "flexibility",
        "safety",
        "compatibility",
    }:
        raise LoopError(f"Unsupported ISO characteristic: {issue.get('iso_characteristic')}")


def extract_opencode_text(output):
    chunks = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part", {}) if isinstance(event, dict) else {}
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text", "")))
    if chunks:
        return "".join(chunks).strip()
    return output.strip()


def validate_selection(value):
    if value.get("decision") not in ("select", "stop"):
        raise LoopError("Muse Spark selection has an invalid decision")
    if value.get("issue_id") is not None and not isinstance(value.get("issue_id"), str):
        raise LoopError("Muse Spark selection issue_id must be a string or null")
    if not isinstance(value.get("rationale"), str) or len(value["rationale"]) > 500:
        raise LoopError("Muse Spark selection rationale is missing or too long")


def validate_patch_reply(value):
    if not isinstance(value.get("diff"), str) or not isinstance(value.get("hypothesis"), str):
        raise LoopError("Step 5 must return string fields diff and hypothesis")
    if len(value["hypothesis"]) > 1000:
        raise LoopError("Step 5 hypothesis is too long")


def parse_json_reply(value, role):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", value):
        try:
            parsed, _ = decoder.raw_decode(value[match.start():])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise LoopError(f"{role} did not return a JSON object")


def call_opencode(role, prompt, role_config, run_dir, scratch_root, timeout, call_id):
    binary = shutil.which(role_config.get("command", "opencode"))
    if not binary:
        raise LoopError("OpenCode CLI was not found on PATH")
    model_dir = Path(run_dir) / "models"
    scratch = Path(scratch_root) / f"{role}-{call_id}"
    model_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    prompt_path = model_dir / f"{role}-{call_id}.prompt.txt"
    output_path = model_dir / f"{role}-{call_id}.stdout.jsonl"
    error_path = model_dir / f"{role}-{call_id}.stderr.txt"
    prompt_path.write_text(prompt)
    argv = [
        binary,
        "run",
        "--standalone",
        "--model",
        role_config["model"],
        "--format",
        "json",
        "--agent",
        role_config.get("agent", "plan"),
        prompt,
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=scratch,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LoopError(f"{role} timed out after {timeout}s") from exc
    except OSError as exc:
        raise LoopError(f"Could not run OpenCode: {exc}") from exc
    output_path.write_text(result.stdout)
    error_path.write_text(result.stderr)
    if result.returncode:
        raise LoopError(f"{role} CLI failed ({result.returncode}); see {error_path}")
    return extract_opencode_text(result.stdout)


def call_codex_reviewer(prompt, role_config, schema_path, run_dir, scratch_root, timeout, call_id):
    binary = shutil.which(role_config.get("command", "codex"))
    if not binary:
        raise LoopError("Codex CLI was not found on PATH")
    model_dir = Path(run_dir) / "models"
    scratch = Path(scratch_root) / f"reviewer-{call_id}"
    model_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    prompt_path = model_dir / f"reviewer-{call_id}.prompt.txt"
    output_path = model_dir / f"reviewer-{call_id}.json"
    stdout_path = model_dir / f"reviewer-{call_id}.stdout.txt"
    stderr_path = model_dir / f"reviewer-{call_id}.stderr.txt"
    prompt_path.write_text(prompt)
    argv = [
        binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(scratch),
        "-m",
        role_config["model"],
        "-s",
        "read-only",
        "--output-schema",
        str(Path(schema_path).resolve()),
        "--output-last-message",
        str(output_path.resolve()),
        prompt,
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=scratch,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LoopError(f"Codex reviewer timed out after {timeout}s") from exc
    except OSError as exc:
        raise LoopError(f"Could not run Codex: {exc}") from exc
    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    if result.returncode:
        raise LoopError(f"Codex reviewer CLI failed ({result.returncode}); see {stderr_path}")
    try:
        return json.loads(output_path.read_text())
    except (OSError, json.JSONDecodeError):
        return parse_json_reply(result.stdout, "Codex reviewer")


def lint_snapshot(repo, rel, log_path, timeout):
    result = run_command(
        ["./mach", "lint", "--format", "json", rel],
        repo,
        timeout,
    )
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr)
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LoopError(f"mach lint did not return JSON; see {log_path}") from exc
    messages = []
    if isinstance(parsed, list):
        records = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        records = parsed["results"]
    elif isinstance(parsed, dict):
        records = []
        for filename, items in parsed.items():
            if isinstance(items, list):
                records.append({"filePath": filename, "messages": items})
    else:
        raise LoopError(f"Unexpected lint JSON format; see {log_path}")
    for record in records:
        if not isinstance(record, dict):
            continue
        filename = str(record.get("filePath", record.get("path", "")))
        if not (filename.replace("\\", "/").endswith(rel) or filename == rel):
            continue
        for message in record.get("messages", []):
            if not isinstance(message, dict):
                continue
            messages.append({
                "path": rel,
                "rule": str(message.get("ruleId", message.get("rule", ""))),
                "message": str(message.get("message", "")),
                "line": message.get("line"),
            })
    if result.returncode not in (0, 1):
        raise LoopError(f"mach lint exited {result.returncode}; see {log_path}")
    return messages


def semgrep_snapshot(repo, rel, log_path, timeout):
    result = run_command(
        [
            "semgrep", "--config", "auto", "--json",
            "--include=*.js", "--include=*.jsx", "--include=*.mjs", rel,
        ],
        repo,
        timeout,
    )
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr)
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LoopError(f"Semgrep did not return JSON; see {log_path}") from exc
    if result.returncode not in (0, 1) or parsed.get("errors"):
        raise LoopError(f"Semgrep failed; see {log_path}")
    return [
        {
            "path": rel,
            "rule": str(row.get("check_id", "")),
            "message": str(row.get("extra", {}).get("message", "")),
            "line": row.get("start", {}).get("line"),
        }
        for row in parsed.get("results", [])
        if str(row.get("path", "")).replace("\\", "/").endswith(rel)
    ]


def lizard_snapshot(repo, rel, log_path, timeout):
    result = run_command(["lizard", "-l", "javascript", "--csv", rel], repo, timeout)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr)
    if result.returncode:
        raise LoopError(f"Lizard failed; see {log_path}")
    functions = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 10:
            continue
        try:
            complexity = int(row[1])
            start_line = int(row[9])
        except ValueError:
            continue
        if row[6].replace("\\", "/").endswith(rel):
            functions.append({
                "path": rel,
                "complexity": complexity,
                "function": row[7],
                "long_name": row[8],
                "line": start_line,
            })
    return functions


def primary_snapshot(issue, repo, log_path, timeout):
    probe = issue["probe"]
    if probe == "eslint":
        return lint_snapshot(repo, issue["path"], log_path, timeout)
    if probe == "semgrep":
        return semgrep_snapshot(repo, issue["path"], log_path, timeout)
    if probe == "lizard":
        return lizard_snapshot(repo, issue["path"], log_path, timeout)
    raise LoopError(f"No deterministic verifier is registered for {probe}")


def stable_findings(findings):
    return Counter((row["path"], row["rule"], row["message"]) for row in findings)


def count_target(issue, findings, complexity_limit):
    if issue["probe"] == "lizard":
        name = str(issue.get("function", "")).strip()
        found = [
            row for row in findings
            if name and (row["function"] == name or row["long_name"].startswith(name + " "))
        ]
        if not found:
            return None
        return max(row["complexity"] for row in found)
    rule = str(issue["rule"]).casefold()
    matched = [
        row for row in findings
        if row["path"] == issue["path"]
        and (row["rule"].casefold() == rule or row["rule"].casefold().endswith(rule))
    ]
    return len(matched)


def validate_patch(diff, expected_path):
    diff = diff.strip() + "\n"
    headers = re.findall(r"(?m)^diff --git a/(.*?) b/(.*?)$", diff)
    if len(headers) != 1 or headers[0] != (expected_path, expected_path):
        raise LoopError("Step 5 must return a Git diff for exactly the selected file")
    if re.search(r"(?m)^(new file mode|deleted file mode|old mode|new mode|rename from|rename to|GIT binary patch)", diff):
        raise LoopError("Patch changes file type or mode; only text edits are allowed")
    if "/dev/null" in diff:
        raise LoopError("Patch may not create or delete files")
    return diff


@contextmanager
def candidate_context(repo, run_dir, keep_worktree):
    repo = Path(repo).resolve()
    root = run_command(["git", "rev-parse", "--show-toplevel"], repo, 20)
    if root.returncode:
        raise LoopError(f"Firefox root is not a Git checkout: {repo}")
    repo = Path(root.stdout.strip()).resolve()
    status = run_command(["git", "status", "--porcelain", "--untracked-files=all"], repo, 30)
    if status.returncode:
        raise LoopError("Could not inspect Firefox working tree")
    untracked = [line for line in status.stdout.splitlines() if line.startswith("?? ")]
    if untracked:
        raise LoopError("Firefox has untracked files; candidate snapshot refused to avoid omitting user files")
    overlay = run_command(["git", "diff", "--binary", "HEAD"], repo, 60)
    if overlay.returncode:
        raise LoopError("Could not snapshot tracked Firefox changes")
    changed_paths = run_command(["git", "diff", "--name-only", "HEAD"], repo, 30)
    if changed_paths.returncode:
        raise LoopError("Could not list tracked Firefox changes")
    overlay_bytes = overlay.stdout.encode()
    baseline = {
        "revision": run_command(["git", "rev-parse", "HEAD"], repo, 20).stdout.strip(),
        "tracked_overlay_sha256": hashlib.sha256(overlay_bytes).hexdigest(),
        "tracked_changed_paths": sorted(path for path in changed_paths.stdout.splitlines() if path),
        "untracked_paths": [],
    }
    (Path(run_dir) / "baseline-overlay.patch").write_bytes(overlay_bytes)
    (Path(run_dir) / "baseline.json").write_text(json.dumps(baseline, indent=2))

    worktree = Path(run_dir) / "candidate-worktree"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    added = run_command(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        repo,
        600,
    )
    if added.returncode:
        raise LoopError(f"Could not create isolated Git worktree: {added.stderr[-1000:]}")
    try:
        if overlay.stdout:
            applied = run_command(["git", "apply", "--check", "--binary"], worktree, 60, overlay.stdout)
            if applied.returncode:
                raise LoopError("Existing tracked changes could not be copied to the isolated worktree")
            applied = run_command(["git", "apply", "--binary"], worktree, 60, overlay.stdout)
            if applied.returncode:
                raise LoopError("Existing tracked changes could not be copied to the isolated worktree")
        yield_path = worktree
        yield yield_path
    finally:
        if not keep_worktree:
            run_command(["git", "worktree", "remove", "--force", str(worktree)], repo, 120)



def project_instructions(repo, rel, max_chars=24000):
    rel_parent = PurePosixPath(rel).parent
    directories = [Path(repo)]
    current = Path(repo)
    for part in rel_parent.parts:
        current = current / part
        directories.append(current)
    found = []
    for directory in directories:
        instruction = directory / "AGENTS.md"
        if instruction.is_file():
            content = instruction.read_text()
            found.append(f"--- {instruction.relative_to(repo)} ---\n{content}")
    return "\n".join(found)[:max_chars]



def source_context(source, line, max_chars=48000, radius=180):
    lines = source.splitlines()
    if len(source) <= max_chars:
        return source
    center = max(1, int(line or 1))
    start = max(1, center - radius)
    end = min(len(lines), center + radius)
    return "\n".join(
        [f"[context lines {start}-{end} of {len(lines)}]"]
        + [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
    )


def main():
    parser = argparse.ArgumentParser(description="Run a bounded, evidence-gated Firefox quality loop.")
    parser.add_argument("--issues", default=str(LOOP_ROOT / "artifacts" / "issues.json"))
    parser.add_argument("--firefox-root", default=str(DEFAULT_FIREFOX_ROOT))
    parser.add_argument("--artifacts", default=str(LOOP_ROOT / "artifacts"))
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--budget", type=int, default=5, help="Maximum candidate patches per run")
    parser.add_argument("--execute", action="store_true", help="Call the configured model CLIs and run probes")
    parser.add_argument("--keep-worktree", action="store_true", help="Keep the isolated candidate checkout for inspection")
    args = parser.parse_args()

    config = read_config(LOOP_ROOT / "agents.yml")
    roles = config.get("roles", {})
    budgets = config.get("budgets", {})
    timeouts = config.get("timeouts", {})
    supported_probes = set(config.get("verification", {}).get("supported_probes", []))
    issues = load_issues(args.issues, read_config(LOOP_ROOT / "quality-model.yml").get("scope", {}))
    filtered = []
    for issue in issues:
        try:
            validate_issue(issue, supported_probes)
            safe_target_path(args.firefox_root, issue["path"])
            filtered.append(issue)
        except LoopError:
            continue
    issues = filtered
    if args.max_rounds < 1 or args.budget < 1:
        raise LoopError("--max-rounds and --budget must be positive")
    if not issues:
        raise LoopError("No eligible New Tab findings with a supported deterministic verifier")

    required_roles = ("supervisor", "fixer", "reviewer")
    for role in required_roles:
        if role not in roles:
            raise LoopError(f"Missing configured role: {role}")
    if not args.execute:
        missing_cli = [roles[role].get("command", "opencode" if roles[role].get("provider") == "opencode" else "codex") for role in required_roles if not shutil.which(roles[role].get("command", "opencode" if roles[role].get("provider") == "opencode" else "codex"))]
        if missing_cli:
            raise LoopError("Required model CLI not found on PATH: " + ", ".join(sorted(set(missing_cli))))
        print(json.dumps({
            "mode": "preflight",
            "eligible_findings": len(issues),
            "eligible": [{"id": issue["id"], "probe": issue["probe"], "path": issue["path"], "rule": issue["rule"]} for issue in issues],
            "models": {role: roles[role]["model"] for role in required_roles},
            "next": "rerun with --execute to invoke models and probes",
        }, indent=2))
        return 0

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = Path(args.artifacts).resolve() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    events = run_dir / "events.jsonl"
    scratch_root = run_dir / "scratch"
    scratch_root.mkdir()
    try:
        with candidate_context(args.firefox_root, run_dir, args.keep_worktree) as candidate:
            accepted = []
            attempts = []
            issue_index = {issue["id"]: issue for issue in issues}
            max_rounds = min(args.max_rounds, int(budgets.get("max_l2_rounds", 3)), max(1, int(budgets.get("max_steps", 24)) // 3))
            patch_budget = min(args.budget, int(budgets.get("patches_per_run", 5)))
            timeout_model = int(timeouts.get("model_seconds", 300))
            timeout_probe = int(timeouts.get("probe_seconds", 300))
            for round_number in range(1, max_rounds + 1):
                remaining = [issue for issue in issues if issue["id"] not in attempts]
                if not remaining or len(accepted) >= patch_budget:
                    break
                selection_schema = json.loads(configured_path(roles["supervisor"]["schema"]).read_text())
                selection_prompt = (
                    configured_path(roles["supervisor"]["prompt"]).read_text()
                    + "\nReturn one JSON object matching this contract: "
                    + "Follow this JSON schema exactly: " + json.dumps(selection_schema) + ". "
                    + "Choose one eligible finding only. Do not invent IDs or scores.\n"
                    + "Eligible findings:\n"
                    + json.dumps([
                        {k: issue.get(k) for k in ("id", "probe", "path", "line", "rule", "severity", "message", "iso_characteristic", "iso_subcharacteristic")}
                        for issue in remaining
                    ], indent=2)
                    + f"\nRound {round_number} of {max_rounds}; accepted candidate patches: {len(accepted)}; patch budget: {patch_budget}."
                )
                append_event(events, "selection", "started", round=round_number)
                selection_text = call_opencode(
                    "supervisor", selection_prompt, roles["supervisor"], run_dir, scratch_root, timeout_model, f"round-{round_number:02d}"
                )
                selection = parse_json_reply(selection_text, "Muse Spark supervisor")
                validate_selection(selection)
                selected_id = selection.get("issue_id")
                if selection.get("decision") == "stop":
                    append_event(events, "selection", "stopped", round=round_number, rationale=selection.get("rationale", ""))
                    break
                if selection.get("decision") != "select" or selected_id not in issue_index or selected_id not in {i["id"] for i in remaining}:
                    raise LoopError("Muse Spark returned a decision outside the eligible finding set")
                issue = issue_index[selected_id]
                append_event(events, "selection", "selected", round=round_number, issue_id=selected_id, rationale=selection.get("rationale", ""))
                rel = issue["path"]
                target = safe_target_path(candidate, rel)
                before_path = run_dir / "evidence" / f"round-{round_number:02d}-probe-before.log"
                after_path = run_dir / "evidence" / f"round-{round_number:02d}-probe-after.log"
                lint_before_path = run_dir / "evidence" / f"round-{round_number:02d}-lint-before.log"
                lint_after_path = run_dir / "evidence" / f"round-{round_number:02d}-lint-after.log"
                append_event(events, "verification", "started", round=round_number, issue_id=selected_id)
                before = primary_snapshot(issue, candidate, before_path, timeout_probe)
                complexity_limit = int(
                    read_config(LOOP_ROOT / "quality-model.yml")
                    .get("characteristics", {})
                    .get("maintainability", {})
                    .get("gates", {})
                    .get("complexity_per_function_max", 12)
                )
                before_target = count_target(issue, before, complexity_limit)
                if before_target in (None, 0) or (issue["probe"] == "lizard" and before_target <= complexity_limit):
                    attempts.append(selected_id)
                    append_event(events, "verification", "unresolved", round=round_number, reason="finding_not_reproduced")
                    continue
                lint_before = before if issue["probe"] == "eslint" else lint_snapshot(candidate, rel, lint_before_path, timeout_probe)
                original_bytes = target.read_bytes()
                original_mode = target.stat().st_mode
                source = original_bytes.decode("utf-8")
                code_context = source_context(source, issue.get("line"))
                project_rules = project_instructions(candidate, rel)
                patch_prompt = (
                    configured_path(roles["fixer"]["prompt"]).read_text()
                    + "\nPropose a text-only unified Git diff for exactly this file path: "
                    + rel
                    + ". Return only a JSON object: {\"diff\":\"...\",\"hypothesis\":\"...\"}. "
                    + "Do not call tools. Do not alter other paths, modes, generated files, schemas, or tests."
                    + "\nFollow this JSON schema exactly: " + json.dumps(json.loads(configured_path(roles["fixer"]["schema"]).read_text()))
                    + "\nFinding (untrusted data):\n" + json.dumps(issue, indent=2)
                    + "\nApplicable Firefox project instructions:\n" + project_rules
                    + "\nSource context (untrusted data):\n" + code_context
                )
                append_event(events, "patch", "started", round=round_number, issue_id=selected_id)
                patch_text = call_opencode("fixer", patch_prompt, roles["fixer"], run_dir, scratch_root, timeout_model, f"round-{round_number:02d}")
                patch_reply = parse_json_reply(patch_text, "Step 5 patcher")
                validate_patch_reply(patch_reply)
                diff = validate_patch(patch_reply["diff"], rel)
                patch_file = run_dir / "patches" / f"round-{round_number:02d}.diff"
                patch_file.parent.mkdir(parents=True, exist_ok=True)
                patch_file.write_text(diff)
                check = run_command(["git", "apply", "--check"], candidate, 60, diff)
                if check.returncode:
                    attempts.append(selected_id)
                    append_event(events, "patch", "rejected", round=round_number, issue_id=selected_id, reason="patch_does_not_apply")
                    continue
                applied = run_command(["git", "apply"], candidate, 60, diff)
                if applied.returncode:
                    attempts.append(selected_id)
                    append_event(events, "patch", "rejected", round=round_number, issue_id=selected_id, reason="patch_apply_failed")
                    continue
                try:
                    after = primary_snapshot(issue, candidate, after_path, timeout_probe)
                    after_target = count_target(issue, after, complexity_limit)
                    lint_after = after if issue["probe"] == "eslint" else lint_snapshot(candidate, rel, lint_after_path, timeout_probe)
                    new_lint = stable_findings(lint_after) - stable_findings(lint_before)
                    primary_improved = (
                        after_target is not None
                        and (
                            after_target <= complexity_limit
                            if issue["probe"] == "lizard"
                            else after_target < before_target
                        )
                    )
                    verification = {
                        "before_target": before_target,
                        "after_target": after_target,
                        "new_lint_findings": sum(new_lint.values()),
                        "primary_probe": issue["probe"],
                        "pass": bool(primary_improved and not new_lint),
                    }
                except LoopError as exc:
                    verification = {"pass": False, "status": "unresolved", "reason": str(exc)}
                if not verification.get("pass"):
                    target.write_bytes(original_bytes)
                    os.chmod(target, original_mode)
                    attempts.append(selected_id)
                    append_event(events, "verification", "rejected", round=round_number, issue_id=selected_id, result=verification)
                    continue

                review_prompt = (
                    configured_path(roles["reviewer"]["prompt"]).read_text()
                    + "\nReview this exact candidate diff and evidence. Return verdict per the required JSON schema. "
                    + "The deterministic probe and lint gate must pass; report concrete defects, not speculative style preferences."
                    + "\nFinding:\n" + json.dumps(issue, indent=2)
                    + "\nVerification evidence:\n" + json.dumps(verification, indent=2)
                    + "\nCandidate diff:\n" + diff
                )
                append_event(events, "review", "started", round=round_number, issue_id=selected_id)
                try:
                    review = call_codex_reviewer(
                        review_prompt,
                        roles["reviewer"],
                        configured_path(roles["reviewer"].get("schema", "reviewer.schema.json")),
                        run_dir,
                        scratch_root,
                        timeout_model,
                        f"round-{round_number:02d}",
                    )
                except LoopError:
                    target.write_bytes(original_bytes)
                    os.chmod(target, original_mode)
                    raise
                if review.get("verdict") != "PASS":
                    target.write_bytes(original_bytes)
                    os.chmod(target, original_mode)
                    attempts.append(selected_id)
                    append_event(events, "review", "rejected", round=round_number, issue_id=selected_id, feedback=review.get("feedback", ""))
                    continue
                accepted.append({
                    "issue_id": selected_id,
                    "path": rel,
                    "characteristic": issue["iso_characteristic"],
                    "hypothesis": str(patch_reply.get("hypothesis", "")),
                    "patch_file": str(patch_file.relative_to(run_dir)),
                    "verification": verification,
                    "review": review,
                })
                attempts.append(selected_id)
                append_event(events, "candidate", "accepted_for_human_review", round=round_number, issue_id=selected_id)

            report = {
                "run_id": run_id,
                "status": "candidate_for_human_review" if accepted else "no_verified_candidate",
                "base_revision": run_command(["git", "rev-parse", "HEAD"], candidate, 20).stdout.strip(),
                "tracked_overlay_sha256": json.loads((run_dir / "baseline.json").read_text())["tracked_overlay_sha256"],
                "models": {role: roles[role]["model"] for role in required_roles},
                "accepted_candidates": accepted,
                "attempts": attempts,
                "automatic_adoption": False,
                "candidate_worktree": str(candidate) if args.keep_worktree else None,
                "quality_score_delta": None,
                "limitations": ["Only the selected reporting probe and touched-file lint were run.", "No ISO score is inferred from model output."],
            }
            (run_dir / "decision.json").write_text(json.dumps(report, indent=2))
            print(json.dumps({"run_dir": str(run_dir), **report}, indent=2))
    except Exception as exc:
        append_event(events, "run", "failed", error=str(exc))
        (run_dir / "failure.json").write_text(json.dumps({"run_id": run_id, "error": str(exc)}, indent=2))
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LoopError as error:
        raise SystemExit(f"quality-loop: {error}")
