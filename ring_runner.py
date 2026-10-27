"""Rings R0-R2 regression shield. Redirects all slow output to artifacts/."""
import argparse
import subprocess
import hashlib
from pathlib import Path


def run(cmd, out_file, cwd):
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    return r.returncode


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def load_quarantine(path):
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firefox-root", default="firefox")
    ap.add_argument("--artifacts", default="loop/artifacts")
    ap.add_argument("--patch-id", default="local")
    ap.add_argument("--files", default="")
    ap.add_argument("--ring", default="ring0", choices=["ring0", "ring1", "ring2"])
    ap.add_argument("--quarantine", default="loop/known-flaky.yml")
    args = ap.parse_args()

    art = Path(args.artifacts)
    art.mkdir(parents=True, exist_ok=True)
    quarantine = load_quarantine(args.quarantine)
    results = {"patch": args.patch_id, "ring": args.ring, "steps": {}}

    if args.ring in ("ring0", "ring1", "ring2"):
        code = run(f"./mach lint {args.files}", art / f"regression-{args.patch_id}-lint.txt", args.firefox_root)
        results["steps"]["lint"] = code

    if args.ring in ("ring1", "ring2"):
        code = run("./mach npm test --prefix=browser/extensions/newtab", art / f"regression-{args.patch_id}-jest.txt", args.firefox_root)
        results["steps"]["jest"] = code
        code = run("./mach test browser/components/newtab/test/xpcshell", art / f"regression-{args.patch_id}-xpcshell.txt", args.firefox_root)
        results["steps"]["xpcshell"] = code

    if args.ring == "ring2":
        code = run("./mach test browser/components/newtab/test/browser --headless", art / f"regression-{args.patch_id}-browser.txt", args.firefox_root)
        results["steps"]["browser"] = code
        code = run("./mach build faster", art / f"regression-{args.patch_id}-build.txt", args.firefox_root)
        results["steps"]["build"] = code

    verdict = "pass" if all(v == 0 for v in results["steps"].values()) else "fail"
    results["verdict"] = verdict
    results["quarantine_hint"] = bool(quarantine.strip())
    print(f"ring={args.ring} verdict={verdict} steps={results['steps']}")
    (art / f"regression-{args.patch_id}-{args.ring}.json").write_text(__import__("json").dumps(results, indent=2))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
