"""Capture R0 regression baseline without running slow tests by default."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def git_rev(root):
    r = subprocess.run(["git", "log", "-1", "--format=%H %ad", "--date=short"],
                        capture_output=True, text=True, cwd=root)
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firefox-root", default="firefox")
    ap.add_argument("--out", default="loop/artifacts/regression-baseline.json")
    args = ap.parse_args()

    root = Path(args.firefox_root)
    baseline = {"rev": git_rev(args.firefox_root), "files": {}}
    watch = [
        "browser/extensions/newtab/manifest.json",
        "browser/components/newtab/metrics.yaml",
        "browser/components/newtab/pings.yaml",
        "browser/extensions/newtab/prerendered/activity-stream.html",
    ]
    for rel in watch:
        p = root / rel
        if p.exists():
            baseline["files"][rel] = {"sha16": sha(p), "bytes": p.stat().st_size}
        else:
            baseline["files"][rel] = {"missing": True}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, indent=2))
    print(f"wrote baseline rev={baseline['rev']} to {out}")


if __name__ == "__main__":
    main()
