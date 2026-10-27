# Quality ledger
Append one row per L2 round. Scores 0-100 per characteristic, from probes not from LLM judgement.

| round | performance | interaction | security | maintainability | regression | promoted | notes |
|------:|------------:|------------:|---------:|----------------:|:----------:|---------:|-------|
| 0 |  |  |  |  | baseline | - | R0 baseline |

## R1 live run — 2026-10-14
- Probes (venv .venv): mach lint -W → 0 errors 5 warnings; semgrep 1.180.0 → 12 findings (10 INFO formatstring, 2 WARNING object-assign); lizard 1.24.1 → 3262 fns, 49 CCN>15; jscpd → JS 1.12%, JSX 0.71%.
- L3 calibration: rule-based FP suppression added to normalize.py; unsafe-formatstring accepted as FP (console-only sink). 61 → 51 issues.
- Bugs found by live run: FP rule lines missed (`- ` prefix); checkpoint JSON truncation on 51-issue state. Both fixed.
- L0 fix: SectionsManager.sys.mjs:176 Object.assign → direct assignment. Verifier: semgrep 2→0. Reviewer (gpt-6-luna): PASS. Shield: mach lint 0 problems.
- Open: 54 issues (49 lizard maintainability, 5 eslint). Raw outputs in loop/artifacts/.
