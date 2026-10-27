#!/usr/bin/env bash
# Reviewer via Codex CLI (GPT-6 Luna), read-only. Usage:
#   loop/reviewer_codex.sh <patch-file> <out-file> [probe-evidence-file]
set -euo pipefail
PATCH="$1"
OUT="$2"
EVIDENCE="${3:-}"
PROBE_LINE="No reporting-probe result supplied."
if [ -n "$EVIDENCE" ]; then
  PROBE_LINE="Reporting-probe result:
$(cat "$EVIDENCE")"
fi
PROMPT="$(cat loop/prompts/reviewer.md)"
codex exec -s read-only --skip-git-repo-check \
  -m gpt-6-luna \
  --output-schema loop/reviewer.schema.json \
  -o "$OUT" \
  "$PROMPT

PATCH UNDER REVIEW:
$(cat "$PATCH")

$PROBE_LINE

Return verdict per schema."
