"""Normalize probe outputs into issues.json with ISO 25010 tags."""
import json
import sys
import argparse


PROBE_ISO = {
    "eslint": ("maintainability", "analysability"),
    "eslint_a11y": ("interaction_capability", "inclusiveness"),
    "eslint_security": ("security", "integrity"),
    "semgrep": ("security", "integrity"),
    "axe": ("interaction_capability", "inclusiveness"),
    "lizard": ("maintainability", "analysability"),
    "jscpd": ("maintainability", "reusability"),
    "knip": ("maintainability", "modifiability"),
    "bundle_report": ("performance_efficiency", "resource_utilisation"),
    "sonar_optional": ("maintainability", "analysability"),
}


def load_false_positives(path):
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return [], []
    entries = []
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line.startswith("path:"):
            entries.append(line.split(":", 1)[1].strip())
        elif line.startswith("rule:"):
            rules.append(line.split(":", 1)[1].strip())
    return entries, rules


def parse_eslint(data, probe):
    issues = []
    for file_result in data if isinstance(data, list) else []:
        path = file_result.get("filePath", "")
        for msg in file_result.get("messages", []):
            issues.append({
                "probe": probe,
                "path": path,
                "line": msg.get("line", 0),
                "rule": msg.get("ruleId", ""),
                "severity": msg.get("severity", 1),
                "message": msg.get("message", "")[:300],
            })
    return issues


def parse_semgrep(data):
    issues = []
    for r in (data.get("results", []) if isinstance(data, dict) else []):
        issues.append({
            "probe": "semgrep",
            "path": r.get("path", ""),
            "line": r.get("start", {}).get("line", 0),
            "rule": r.get("check_id", ""),
            "severity": r.get("extra", {}).get("severity", "WARNING"),
            "message": r.get("extra", {}).get("message", "")[:300],
        })
    return issues


def parse_generic(data, probe):
    if isinstance(data, list):
        return [{"probe": probe, **i} if isinstance(i, dict) else {"probe": probe, "raw": i} for i in data]
    if isinstance(data, dict):
        return [{"probe": probe, **data}]
    return []


def tag(issue):
    iso = PROBE_ISO.get(issue.get("probe", ""), ("maintainability", "analysability"))
    issue["iso_characteristic"] = iso[0]
    issue["iso_subcharacteristic"] = iso[1]
    issue["verify_cmd"] = f"re-run {issue.get('probe', '')} on {issue.get('path', '')}"
    return issue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eslint", default="")
    ap.add_argument("--semgrep", default="")
    ap.add_argument("--generic-probe", default="")
    ap.add_argument("--generic-file", default="")
    ap.add_argument("--false-positives", default="loop/accepted-false-positives.yml")
    ap.add_argument("--out", default="loop/artifacts/issues.json")
    args = ap.parse_args()

    issues = []
    if args.eslint:
        with open(args.eslint) as f:
            issues += parse_eslint(json.load(f), "eslint")
    if args.semgrep:
        with open(args.semgrep) as f:
            issues += parse_semgrep(json.load(f))
    if args.generic_file:
        with open(args.generic_file) as f:
            issues += parse_generic(json.load(f), args.generic_probe or "generic")

    fps, fp_rules = load_false_positives(args.false_positives)
    issues = [tag(i) for i in issues
              if i.get("path", "") not in fps
              and not any(r in i.get("rule", "") for r in fp_rules)]
    with open(args.out, "w") as f:
        json.dump(issues, f, indent=2)
    print(f"wrote {len(issues)} issues to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
