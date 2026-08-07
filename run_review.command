#!/usr/bin/env bash
#
# Double-click me (macOS), or run ./run_review.command (Linux).
#
# Starts the referral review screen from a clean slate every time:
#
#   1. updates to the latest code            (skipped if offline)
#   2. sets up Python and the dependencies   (fixes them if missing)
#   3. asks whether to re-read the inbox from scratch
#   4. starts the server — DRY RUN unless you type LIVE
#
# In a dry run everything works — reading referrals, the review screen,
# editing fields — but clicking "Create in Nookal" writes nothing: every
# write is a logged no-op. Type LIVE at the prompt for real creates.

set -uo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RESET=$'\033[0m'

step()  { printf "\n%s==> %s%s\n" "$BOLD" "$1" "$RESET"; }
ok()    { printf "%s  OK  %s%s\n" "$GREEN" "$1" "$RESET"; }
warn()  { printf "%s  !!  %s%s\n" "$YELLOW" "$1" "$RESET"; }
fail()  { printf "\n%s  STOPPED: %s%s\n\n" "$RED" "$1" "$RESET"; hold; exit 1; }
hold()  { printf "Press Return to close this window. "; read -r _; }

# ------------------------------------------------------------ latest code

step "Checking for updates"
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    if git pull --ff-only 2>/dev/null; then
        ok "on: $(git log --oneline -1)"
    else
        warn "could not update (offline, or local changes) — continuing"
        warn "with: $(git log --oneline -1)"
    fi
else
    warn "not a git folder — running whatever code is here"
fi

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

# ---------------------------------------------------------- dependencies

# A private environment inside this folder, so a macOS or Homebrew Python
# upgrade can never take the packages away again — and pip never refuses
# with 'externally managed environment'.
step "Setting up the environment"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv || fail "Could not create the Python environment."
fi
VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || fail "The Python environment looks broken.
  Delete the .venv folder and run this again."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$VENV_PY" -m pip install --quiet -r requirements.txt -r requirements-extract.txt \
    || fail "Could not install the Python packages. Are you online?"
ok "packages ready"

if command -v tesseract >/dev/null 2>&1; then
    ok "Tesseract found — scanned referrals will be read"
else
    warn "Tesseract is not installed: scanned referrals will need manual"
    warn "entry. Digital ones are unaffected. To fix: brew install tesseract"
fi

# ----------------------------------------------------------------- inbox

step "The queue"
if [ -d queue ] && [ -n "$(ls queue/*.json 2>/dev/null)" ]; then
    printf "There are referrals already in the queue. Type %sFRESH%s and press\n" "$BOLD" "$RESET"
    printf "Return to throw them away and re-read everything in inbox/ with\n"
    printf "the latest extraction, or just press Return to keep them: "
    read -r answer
    if [ "$answer" = "FRESH" ]; then
        rm -rf queue
        ok "queue cleared — the inbox will be re-read"
    else
        ok "keeping the existing queue"
    fi
else
    ok "queue is empty — the inbox will be read on start"
fi

# ---------------------------------------------------------------- server

step "Start the server"
printf "Press Return for a %sDRY RUN%s (everything works, but 'Create in\n" "$BOLD" "$RESET"
printf "Nookal' writes nothing), or type %sLIVE%s for real creates: " "$BOLD" "$RESET"
read -r answer

DRY="--dry-run"
if [ "$answer" = "LIVE" ]; then
    DRY=""
    warn "LIVE — 'Create in Nookal' will create real records."
else
    ok "dry run — nothing will be written to Nookal"
fi

# Open the review screen once the server has had a moment to start.
if command -v open >/dev/null 2>&1; then
    (sleep 2 && open "http://127.0.0.1:8765") &
fi

# shellcheck disable=SC2086
"$VENV_PY" review_server.py --queue queue --inbox inbox $DRY
hold
