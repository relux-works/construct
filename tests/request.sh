#!/bin/sh
# Claim: the latest event cannot replace an explicitly selected earlier request; the exact earlier event passes.
set -eu
KIT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CASE=$(mktemp -d "${CONSTRUCT_TEST_TMP:?}/shell-request.XXXXXX")
trap 'rm -rf "$CASE"' EXIT HUP INT TERM
cp "$KIT/construct.example.json" "$CASE/config.json"
cat >"$CASE/event.json" <<'EOF'
{"event_id":"$latest","sender":"build_project_orch","room":"#project","kind":"request","to":["build_project_dev1"],"body":"Task TASK-260101-abcdef"}
EOF
if "$KIT/bin/construct" --config "$CASE/config.json" request-check "$CASE/event.json" '$earlier' TASK-260101-abcdef dev1 >"$CASE/out" 2>"$CASE/err"; then
  echo 'latest request substituted' >&2; exit 1
fi
grep -q 'event id mismatch' "$CASE/err"
sed 's/\$latest/\$earlier/' "$CASE/event.json" >"$CASE/exact.json"
"$KIT/bin/construct" --config "$CASE/config.json" request-check "$CASE/exact.json" '$earlier' TASK-260101-abcdef dev1 >"$CASE/out"
echo 'PASS shell exact-request rejection and positive control'
