#!/usr/bin/env bash
#
# Double-click me (macOS), or run ./run_diagnostic.command (Linux).
#
# Does the whole safe sequence in order and stops before anything is written
# to your live Nookal account:
#
#   1. sets up Python and installs the one dependency
#   2. runs the offline test suite
#   3. checks the API key authenticates          (no writes)
#   4. full rehearsal of every phase             (no writes)
#
# The real run, which does create records, has to be asked for explicitly at
# the end. Nothing here writes to Nookal on its own.

set -uo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RESET=$'\033[0m'

step()  { printf "\n%s==> %s%s\n" "$BOLD" "$1" "$RESET"; }
ok()    { printf "%s  OK  %s%s\n" "$GREEN" "$1" "$RESET"; }
warn()  { printf "%s  !!  %s%s\n" "$YELLOW" "$1" "$RESET"; }
fail()  { printf "\n%s  STOPPED: %s%s\n\n" "$RED" "$1" "$RESET"; hold; exit 1; }
hold()  { printf "Press Return to close this window. "; read -r _; }

# ---------------------------------------------------------------- python

step "Checking Python"
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done
[ -n "$PY" ] || fail "Python 3.9 or newer is not installed.
  Install it from https://www.python.org/downloads/ and run this again."
ok "$($PY --version)"

# ------------------------------------------------------------ dependency

step "Setting up the environment"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv || fail "Could not create the Python environment."
fi
VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || fail "The Python environment looks broken.
  Delete the .venv folder and run this again."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$VENV_PY" -m pip install --quiet -r requirements.txt \
    || fail "Could not install the 'requests' package. Are you online?"
ok "ready"

# ---------------------------------------------------------------- config

step "Checking for your API key"
if [ ! -f nookal_config.json ]; then
    fail "nookal_config.json not found in this folder.

  Copy config.example.json to nookal_config.json, then open it and replace
  PASTE-YOUR-NOOKAL-API-KEY-HERE with your key from Nookal Practice Setup.
  Keep the quotes around it."
fi
"$VENV_PY" -c "import json,sys; k=json.load(open('nookal_config.json')).get('api_key','');
sys.exit(0 if k and 'PASTE-YOUR' not in k else 1)" 2>/dev/null \
    || fail "nookal_config.json has no API key in it yet, or is not valid JSON.
  Open it and replace PASTE-YOUR-NOOKAL-API-KEY-HERE with your real key."
ok "key found (not shown)"

# ----------------------------------------------------------------- tests

step "Running the offline tests (about a minute, nothing leaves this machine)"
if ! "$VENV_PY" -m unittest discover -t . -s tests 2>&1 | tail -4; then
    fail "The offline tests did not pass. Send me the output above before
  going any further — do not run the live diagnostic yet."
fi
ok "all tests passed"

# --------------------------------------------------------------- phase 0

step "Checking your API key against Nookal (read-only, nothing is written)"
if ! "$VENV_PY" diagnostic.py --dry-run --yes --phases 0; then
    fail "Could not authenticate with Nookal. Check the key in
  nookal_config.json, then run this again."
fi
ok "authenticated"
warn "If the line above says GET rather than POST, open nookal_config.json"
warn "and change \"http_method\": \"POST\" to \"GET\" before continuing."

# ------------------------------------------------------------- rehearsal

step "Full rehearsal — every phase, no writes"
"$VENV_PY" diagnostic.py --dry-run --yes

printf "\n%s%s%s\n" "$BOLD" "=======================================================" "$RESET"
ok "Rehearsal finished. Nothing was created in Nookal."
printf "\nThe report so far is in this folder: diagnostic_report.txt\n"

# ------------------------------------------------------------- live run

printf "\n%sThe real run creates records in your LIVE Nookal account:%s\n" "$BOLD" "$RESET"
printf "  - a throwaway patient, ZZTEST APIDIAG\n"
printf "  - a case on that patient, plus fake Medicare details and a test PDF\n"
printf "  - optionally a test entry in the clinic-wide case Title dropdown\n"
printf "\nIt asks before each one, and you can skip any of them.\n"
printf "You will need to delete the test patient in Nookal afterwards.\n"
printf "\nType %sLIVE%s and press Return to do the real run, or just press\n" "$BOLD" "$RESET"
printf "Return to stop here: "
read -r answer

if [ "$answer" = "LIVE" ]; then
    step "Real run — read each prompt before answering"
    "$VENV_PY" diagnostic.py
    printf "\n"
    ok "Done. Send diagnostic_report.txt back, then in Nookal:"
    printf "   1. delete the patient ZZTEST APIDIAG\n"
    printf "   2. remove 'ZZ DIAGNOSTIC TITLE TEST' from the case Title\n"
    printf "      dropdown if it was added\n"
else
    printf "\nStopped before the live run. Nothing was written to Nookal.\n"
    printf "Run this again and type LIVE when you are ready.\n"
fi

printf "\n"
hold
