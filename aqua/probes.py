"""MONITOR sensors: deterministic probes and the ISO/IEC 25023-style measure vector.

Stdlib only, because the MCP server (aqua/mcp_server.py) imports this module from
the project venv, which has no PyYAML.
"""
import csv
import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"

# Measures that make up the quality vector q. All are "lower is better" counts.
# Each one belongs to exactly one ISO/IEC 25010:2023 characteristic.
MEASURES = {
    "lint_maintainability": "maintainability",
    "ccn_excess": "maintainability",
    "lint_security": "security",
    "semgrep_findings": "security",
    "lint_a11y": "interaction_capability",
    "parse_errors": "functional_suitability",
}

SECURITY_LINT_PREFIXES = ("no-unsanitized/", "mozilla/no-unsafe", "no-eval", "no-implied-eval", "no-new-func")
A11Y_LINT_PREFIXES = ("jsx-a11y/", "mozilla/a11y")


class ProbeError(RuntimeError):
    pass


def tool(name):
    """Prefer the project venv for Python-based probes (semgrep, lizard)."""
    candidate = VENV_BIN / name
    return str(candidate) if candidate.exists() else name


def run(argv, cwd, timeout, log_path=None):
    env = dict(os.environ)
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [str(a) for a in argv], cwd=cwd, text=True, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise ProbeError(f"cannot run {argv[0]}: {exc}") from exc
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else ""))
    return result


def _json_tail(text):
    """mach may print virtualenv setup lines before the JSON document."""
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith(("{", "[")):
            try:
                return json.loads("\n".join(text.splitlines()[i:]))
            except json.JSONDecodeError:
                continue
    return None


def lint_characteristic(rule):
    rule = rule or ""
    if rule.startswith(A11Y_LINT_PREFIXES):
        return "interaction_capability", "inclusiveness"
    if rule.startswith(SECURITY_LINT_PREFIXES):
        return "security", "integrity"
    return "maintainability", "analysability"


def _rel(repo, filename):
    filename = str(filename).replace("\\", "/")
    root = str(Path(repo).resolve()).replace("\\", "/").rstrip("/") + "/"
    return filename[len(root):] if filename.startswith(root) else filename


def eslint(repo, target, timeout=600, log_path=None):
    """`./mach lint` over a file or directory. Returns normalized findings."""
    # --warnings: mach hides warnings by default, and most quality findings are warnings.
    result = run(["./mach", "lint", "--warnings", "--format", "json", target], repo, timeout, log_path)
    parsed = _json_tail(result.stdout)
    if parsed is None:
        raise ProbeError(f"mach lint did not return JSON (exit {result.returncode})")
    if result.returncode not in (0, 1):
        raise ProbeError(f"mach lint exited {result.returncode}")
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        records = parsed["results"]
    elif isinstance(parsed, list):
        records = parsed
    elif isinstance(parsed, dict):
        records = [{"filePath": k, "messages": v} for k, v in parsed.items() if isinstance(v, list)]
    else:
        raise ProbeError("unexpected mach lint JSON shape")
    findings = []
    for record in records:
        if not isinstance(record, dict):
            continue
        path = _rel(repo, record.get("filePath", record.get("path", "")))
        for msg in record.get("messages", []):
            if not isinstance(msg, dict):
                continue
            rule = str(msg.get("ruleId") or msg.get("rule") or "")
            fatal = bool(msg.get("fatal")) or (not rule and "Parsing error" in str(msg.get("message", "")))
            iso, sub = ("functional_suitability", "correctness") if fatal else lint_characteristic(rule)
            findings.append({
                "probe": "eslint", "path": path, "line": msg.get("line") or msg.get("lineno"),
                "rule": rule or ("parse-error" if fatal else "unknown"),
                "severity": msg.get("severity", msg.get("level", 1)),
                "message": str(msg.get("message", ""))[:300],
                "iso_characteristic": iso, "iso_subcharacteristic": sub, "fatal": fatal,
            })
    return findings


def semgrep(repo, target, timeout=600, log_path=None, config=None):
    config = config or os.environ.get("AQUA_SEMGREP_CONFIG", "auto")
    # `--config auto` requires semgrep metrics; any explicit ruleset can run with them off.
    metrics = [] if config == "auto" else ["--metrics", "off"]
    result = run([
        tool("semgrep"), "--config", config, "--json", "--quiet", *metrics,
        "--include=*.js", "--include=*.jsx", "--include=*.mjs", target,
    ], repo, timeout, log_path)
    parsed = _json_tail(result.stdout)
    if not isinstance(parsed, dict):
        raise ProbeError(f"semgrep did not return JSON (exit {result.returncode})")
    if result.returncode not in (0, 1):
        raise ProbeError(f"semgrep exited {result.returncode}")
    return [{
        "probe": "semgrep", "path": _rel(repo, row.get("path", "")),
        "line": row.get("start", {}).get("line"), "rule": str(row.get("check_id", "")),
        "severity": row.get("extra", {}).get("severity", "WARNING"),
        "message": str(row.get("extra", {}).get("message", ""))[:300],
        "iso_characteristic": "security", "iso_subcharacteristic": "integrity",
    } for row in parsed.get("results", [])]


def lizard(repo, target, ccn_limit, timeout=600, log_path=None):
    """Returns (all functions, findings for functions above the CCN limit)."""
    result = run([tool("lizard"), "-l", "javascript", "--csv", target], repo, timeout, log_path)
    if result.returncode:
        raise ProbeError(f"lizard exited {result.returncode}")
    functions = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 10:
            continue
        try:
            ccn, start = int(row[1]), int(row[9])
        except ValueError:
            continue
        functions.append({"path": _rel(repo, row[6]), "function": row[7], "long_name": row[8],
                          "ccn": ccn, "line": start})
    findings = [{
        "probe": "lizard", "path": fn["path"], "line": fn["line"], "rule": "lizard-ccn",
        "function": fn["function"], "complexity": fn["ccn"], "severity": fn["ccn"] - ccn_limit,
        "message": f"cyclomatic complexity {fn['ccn']} > {ccn_limit} in {fn['function']}",
        "iso_characteristic": "maintainability", "iso_subcharacteristic": "analysability",
    } for fn in functions if fn["ccn"] > ccn_limit]
    return functions, findings


def snapshot(repo, target, ccn_limit, timeout=600, log_dir=None, semgrep_config="auto"):
    """Run every probe once on `target` (file or directory).

    Returns {"findings": [...], "functions": [...], "q": {measure: value}}.
    The vector q is what the Pareto gate (F3) compares before/after a patch.
    """
    log = (lambda name: Path(log_dir) / name) if log_dir else (lambda name: None)
    lint = eslint(repo, target, timeout, log("eslint.json"))
    sem = (semgrep(repo, target, timeout, log("semgrep.json"), semgrep_config)
           if semgrep_config and _is_js_target(target) else [])
    functions, liz = lizard(repo, target, ccn_limit, timeout, log("lizard.csv")) if _is_js_target(target) else ([], [])
    q = vector(lint, sem, functions, ccn_limit)
    return {"findings": lint + sem + liz, "functions": functions, "q": q}


def vector(lint, sem, functions, ccn_limit):
    q = {name: 0 for name in MEASURES}
    for f in lint:
        if f.get("fatal"):
            q["parse_errors"] += 1
        elif f["iso_characteristic"] == "security":
            q["lint_security"] += 1
        elif f["iso_characteristic"] == "interaction_capability":
            q["lint_a11y"] += 1
        else:
            q["lint_maintainability"] += 1
    q["semgrep_findings"] = len(sem)
    q["ccn_excess"] = sum(max(0, fn["ccn"] - ccn_limit) for fn in functions)
    return q


def _is_js_target(target):
    suffix = Path(target).suffix
    return suffix in ("", ".js", ".jsx", ".mjs")


def by_characteristic(q):
    out = {}
    for measure, value in q.items():
        out.setdefault(MEASURES[measure], {})[measure] = value
    return out
