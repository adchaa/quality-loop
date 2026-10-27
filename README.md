# Evidence-gated ISO quality loop

> **Current implementation: AQUA.** In T3 Code, start a conversation with the agent of your choice
> and invoke the `aqua` skill ([skills/aqua/SKILL.md](skills/aqua/SKILL.md)). That agent orchestrates,
> delegating to OpenCode subagents through the skill's script and the AQUA control daemon. See [AQUA.md](AQUA.md) and [aqua-architecture.html](aqua-architecture.html). The rest of this
> file documents the earlier `orchestrator.py` prototype, kept for comparison.

Presentation report: [architecture-report.html](architecture-report.html).

Pilot scope: Firefox New Tab / Activity Stream under `firefox/browser/extensions/newtab/`.

## Model wiring

`orchestrator.py` now invokes these configured CLI roles:

- **OpenCode Muse Spark 1.3** selects one eligible finding from the supplied issue list.
- **OpenCode Step 5** proposes a single-file unified diff and a short hypothesis.
- **Codex GPT-6 Luna** reviews the candidate diff and evidence in read-only mode.

The controller validates every selected issue and diff. It creates a detached Git worktree, copies tracked local changes into that candidate snapshot, and applies agent diffs only there. It refuses execution when Firefox has untracked files, so the candidate cannot silently omit them. The original Firefox checkout is never patched by the loop.

## How to run

Preflight checks the issue list, scope, probe adapters and CLI availability without calling models:

```sh
python3 loop/orchestrator.py
```

Run the bounded loop and keep the candidate worktree for inspection:

```sh
python3 loop/orchestrator.py --execute --keep-worktree
```

Use `--max-rounds`, `--budget`, `--issues`, `--firefox-root`, or `--artifacts` to adjust the run. Preflight checks that the CLI programs are installed; provider access is exercised only by `--execute`. Without `--keep-worktree`, the temporary Git worktree is removed when the run ends. A reviewed candidate is still only a patch artifact; the tool does not adopt, merge, push, or submit it.

Each run writes a separate directory under `loop/artifacts/runs/` containing model prompts and outputs, ordered events, before/after probe logs, individual patch diffs, and `decision.json`. Prompts include relevant source context and applicable Firefox `AGENTS.md` guidance.

## Evidence and limits

The prototype supports targeted **ESLint**, **Semgrep**, and **Lizard** findings. Before asking Step 5 to patch, it must reproduce the selected finding. After applying the diff in the isolated worktree, the same probe must show improvement and touched-file lint must add no new findings. GPT-6 Luna then performs an independent read-only review. A reviewer PASS cannot compensate for missing or failed probe evidence.

This is a narrow static-analysis pilot. It does not yet run the full New Tab Jest, xpcshell, browser-test, or build rings. An unsupported or non-reproducible finding is unresolved; it is not treated as a pass. The controller never derives ISO quality scores from model judgments.

## Current evidence

- Live static probes were run on 2026-10-14; raw outputs and normalized findings are in `loop/artifacts/`.
- One Semgrep finding in `SectionsManager.sys.mjs` was changed and re-scanned from two findings to zero. Its touched-file lint artifact reports zero problems, and a read-only reviewer returned PASS.
- New Tab Jest, xpcshell, browser tests and a Firefox build were not run in that recorded R1 flow.
- The old dry-run artifacts predate the CLI wiring. Their mock score increments are synthetic and are not measured quality gains.

## Related tools

Static probe normalization and regression-baseline capture:

```sh
python3 loop/regression_baseline.py --firefox-root firefox --out loop/artifacts/regression-baseline.json
python3 loop/normalize.py --eslint /tmp/eslint.json --semgrep /tmp/semgrep.json --out loop/artifacts/issues.json
```

Firefox verification rings remain available for later broader validation. They are not automatically invoked by the current targeted-probe controller:

```sh
python3 loop/ring_runner.py --ring ring0 --files browser/extensions/newtab/lib/Feed.sys.mjs
python3 loop/ring_runner.py --ring ring1 --patch-id try1
python3 loop/ring_runner.py --ring ring2 --patch-id try1
```

Never submit to Phabricator without explicit user approval.
