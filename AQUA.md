# AQUA — Assured QUality Adaptation loop

Implementation of the architecture in [aqua-architecture.html](aqua-architecture.html).

An **orchestrator agent** drives the loop: the agent you start a conversation with in
**T3 Code** (Claude Code, Codex, OpenCode, ...). You invoke the **`aqua` skill** ([skills/aqua/SKILL.md](skills/aqua/SKILL.md)), which holds the whole
playbook. The orchestrator plans and **delegates to subagents**:
- a localizer;
- an ensemble of implementers;
- a reviewer, after implementation;
- a reporter, which writes the human report.

It works through the skill's script (`skills/aqua/scripts/aqua`), which talks to the
**control daemon** (`aqua/control.py`) over a local Unix socket. The daemon measures every
candidate with the assurance cascade and enforces the rules in code. The subagents run on
OpenCode, by default with one model (`opencode/deepseek-v4.1-flash-free`) for every role. The outer loop is still
MAPE-K, with a Thompson-sampling planner over ISO/IEC 25010:2023 characteristics.

> **LLMs propose, measurements decide, humans adopt.**

Pilot scope: `firefox/browser/extensions/newtab` (`.js/.jsx/.mjs`; tests and generated code are excluded).

## Who does what

| actor | is | does | cannot |
|---|---|---|---|
| Human | you | picks the agent in T3 Code, invokes the skill with the session parameters, accepts or rejects patches | — |
| Orchestrator | the agent of your T3 Code conversation, running the `aqua` skill | chooses findings, writes briefs, delegates, decides to retry, escalate or give up | edit files, run anything but the `aqua` script, commit without the two keys |
| Localizer | OpenCode subagent with probe tools (MCP `aqua-probes`) | explains where and why, names the risks | edit |
| Implementers ×N | OpenCode agents (an ensemble when several models are configured) | propose search/replace edits | edit (the controller applies the edits) |
| Reviewer | a separate OpenCode agent; a rationed escalation for a second opinion | approve, request changes or reject, with line comments | approve something that failed a measurement |
| Reporter | OpenCode subagent | writes the human report from the frozen evidence | change any number (the controller appends the evidence) |
| Controller | `aqua/session.py` inside the daemon `aqua/control.py` | measures (F1–F5), keeps knowledge, enforces I1–I5 | — |

## Invariants (enforced in `session.py`, tested in `tests/test_session.py`)

| | name | rule |
|---|---|---|
| I1 | sole writer | Only the daemon writes to the candidate worktree. A fingerprint check stops the session if anything else did. |
| I2 | two keys | `aqua_commit` requires that F1–F5 passed on exactly that content **and** that a reviewer approved exactly that candidate. If the reviewer is unavailable, the commit is refused (fail closed). |
| I3 | independence | The reviewer is a separate agent with a fresh context, and a different model whenever one is configured (`require_different_reviewer_model: true` makes that mandatory). |
| I4 | budgets | Rounds, implementer calls per round, total agent calls, and reviewer escalations (`escalation_budget`). |
| I5 | grounding | The controller appends its evidence tables to the reporter's text and flags any committed patch the text omits. |

## Module map

| Architecture box | Code |
|---|---|
| Orchestrator playbook | `skills/aqua/SKILL.md` (the skill), linked into `.claude/`, `.agents/` and `.opencode/skills/aqua` (here and in the parent directory) |
| Orchestrator commands | `skills/aqua/scripts/aqua` (stdlib client; starts the daemon on `start`) |
| Control plane | `aqua/control.py` (Unix-socket daemon, background jobs) → `aqua/session.py` (rules and state) |
| MONITOR (sensors, quality vector q) | `aqua/probes.py`: `mach lint --warnings`, Semgrep, Lizard → findings and q |
| ANALYZE | `aqua/analyze.py`: scope, false-positive base, dedupe, stable ids |
| PLAN (bandit, advisory) | `aqua/plan.py`: Thompson sampling over characteristics, then finding priority |
| VALIDATE (F1–F5) | `aqua/cascade.py` |
| Subagents | `aqua/agents.py` + `aqua/prompts/{localizer,implementer,reviewer,reporter}.md` |
| Probe tools for subagents | `aqua/mcp_server.py`: `probe_file`, `function_complexity` |
| Report | `aqua/report.py`: evidence (`decision.json`) and the deterministic appendix |
| KNOWLEDGE | `aqua/knowledge.py` → `loop/knowledge/` (ledger, bandit, playbook, attempts, candidates) |
| CLI | `aqua_loop.py` |

## One round

```
orchestrator: aqua plan -> aqua open-round <finding> --why ...            probe re-run: reproduces?
           -> aqua delegate localizer                                   region, risks, strategy
           -> aqua delegate implementer --brief .. -n N ──► N candidates (parallel)
           -> aqua evaluate                          ──► F1..F5 per candidate, ranked by Δq
           -> aqua delegate reviewer --candidate best ──► approve | request_changes | reject
                 request_changes -> implementer again with the comments (REFLECT)
                 approve         -> aqua commit  (I2 checked) -> series/NN-<id>.diff, q re-measured
           -> or aqua give-up <reason>
after the last round: aqua finish -> aqua delegate reporter -> report.md -> aqua stop
```

## Start a session

1. Run the preflight once (no models are called):

   ```sh
   python3 loop/aqua_loop.py check
   ```

2. In **T3 Code**, open the project (`quality/` or `quality/loop/`) and start a conversation
   with the agent you want as orchestrator. Claude Code, Codex and OpenCode all find the skill:
   it is linked into `.claude/skills/`, `.agents/skills/` and `.opencode/skills/`.
3. Ask for it, with any parameters from the skill's §1 (rounds, focus, candidates, escalation):

   > Use the aqua skill to run one AQUA session with rounds=2 and focus=maintainability.

   In Claude Code you can also type `/aqua rounds=2 focus=maintainability`.
4. When the agent is done, it tells you where `report.md` is. You then accept or reject
   each patch (below).

What the orchestrator may do is set by the skill's rules (only the `aqua` script, read-only
otherwise) and enforced by the controller (I1–I5). It is not set by your harness's
permissions. A shell command that edits the worktree stops the session (I1), and nothing
can be committed without the two keys (I2).

**Codex:** its default sandbox blocks the controller's local socket. Allow it for the
conversation (`sandbox_workspace_write.network_access = true`, or full access).

The control daemon starts on `aqua start` and stops on `aqua stop`, or after
`session.idle_minutes`. Its log is `loop/artifacts/aqua-daemon.log`. If a conversation
ends mid-session, `loop/skills/aqua/scripts/aqua stop` cleans up.

## Other commands

```sh
python3 loop/aqua_loop.py monitor --fresh   # MONITOR + ANALYZE (~1-2 min), no models
python3 loop/aqua_loop.py plan              # the planner's suggestion, no models
python3 loop/aqua_loop.py run --rounds 3    # scripted driver: same Session operations in a fixed order (baseline)
python3 loop/aqua_loop.py status            # posteriors, ledger, candidates
python3 loop/aqua_loop.py feedback <candidate-id> accept|reject   # the human verdict, fed to the bandit
python3 loop/aqua_loop.py false-positive <finding-id> --reason "..."
python3 -m unittest discover -s loop/tests -v
```

### Evaluation design

The skill-driven orchestrator and the scripted driver call the **same** Session operations. So
"does an agentic orchestrator beat a fixed protocol?" is a controlled comparison:
same budgets, same cascade, same knowledge.

Other ablations:
- `run --policy random|round_robin` (planner);
- `run --candidates 1` (no ensemble);
- several models in `agents.implementers` / `agents.reviewers` (heterogeneous vs single-model);
- `session.attempts_per_round: 1` (no reflection or revision).

## Artifacts

Each session writes `loop/artifacts/aqua-runs/<run-id>/`:

| path | contents |
|---|---|
| `report.md` | the reporter's text and the controller's evidence appendix. **Start here** |
| `decision.json` | the full evidence: rounds, candidates, reviews, q, budgets, agent usage |
| `events.jsonl` | every action, in order |
| `models/` | every subagent prompt, raw output and reply |
| `rounds/rNN/` | finding, probe logs, per-candidate `result.json`, `candidate.diff`, reviews, `outcome.json` |
| `series/` | committed patches, in order. They apply on top of the baseline revision plus your local changes |

## Safety

- The orchestrator and all subagents are read-only.
  - The orchestrator is told to run only the `aqua` script; whatever it runs, the I1 guard
    and the two-key commit hold.
  - Subagents are denied edit, bash, web, task and skill, and your global OpenCode MCP
    servers (e.g. a browser) are disabled for them.
  - The daemon socket is user-only (`0600`).
- The daemon applies edits only inside a detached git worktree under the run
  directory, which is removed at the end unless `session.keep_worktree` is set.
- Your Firefox checkout is never modified. Nothing is submitted.

## Configuration (`aqua.yml`)

- `agents`: the OpenCode model per subagent role, `escalation_budget`, `reviewer_escalation`,
  and `require_different_reviewer_model`. The orchestrator is whatever agent you start.
- `session`: rounds, `attempts_per_round`, candidates, `max_agent_calls`, `max_failures_per_finding`, `idle_minutes`.
- `cascade`: the size limit, Pareto epsilon, and the F4 test command. F4 is off by default
  because the newtab `node_modules` are not installed.
