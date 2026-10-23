#!/bin/sh
# Claim: an invalid capacity refuses before layout creation; JSON syntax is never shell-evaluated.
set -eu
KIT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CASE=$(mktemp -d "${CONSTRUCT_TEST_TMP:?}/shell-config.XXXXXX")
trap 'rm -rf "$CASE"' EXIT HUP INT TERM
python3 - "$KIT" "$CASE" <<'PY'
import json,pathlib,sys
kit,case=map(pathlib.Path,sys.argv[1:]);d=json.loads((kit/'construct.example.json').read_text())
d['root']='$(touch injected)';d['slots']=0
(case/'config.json').write_text(json.dumps(d))
PY
if "$KIT/bin/construct" --config "$CASE/config.json" init >"$CASE/out" 2>"$CASE/err"; then
  echo 'invalid slot count accepted' >&2; exit 1
fi
grep -q 'slots must be' "$CASE/err"
test ! -e "$CASE/\$(touch injected)"
python3 - "$CASE/config.json" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);d=json.loads(p.read_text());d['slots']=1;p.write_text(json.dumps(d))
PY
"$KIT/bin/construct" --config "$CASE/config.json" init >"$CASE/out"
test -d "$CASE/\$(touch injected)/project/.construct"
test ! -e "$CASE/injected"
echo 'PASS shell config rejection and literal-data control'
