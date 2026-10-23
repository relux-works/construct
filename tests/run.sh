#!/bin/sh
# Shell entry point; all fixtures live under caller-selected scratch, never a shared HOME.
set -eu
KIT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${CONSTRUCT_TEST_TMP:=$KIT/.temp/tests}"
export CONSTRUCT_TEST_TMP PYTHONDONTWRITEBYTECODE=1
mkdir -p "$CONSTRUCT_TEST_TMP"
sh "$KIT/tests/config.sh"
sh "$KIT/tests/request.sh"
cd "$KIT/tests"
python3 -B -m unittest -v test_config test_request test_slots test_workers test_landing
