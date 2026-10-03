#!/usr/bin/env bash
# One-command setup for sous-chef. Safe to run again: it also updates.
#
#   ./install.sh               set up (or update), then offer to keep it running
#   ./install.sh --service     also keep it running in the background, no question
#   ./install.sh --no-service  never ask about the background service
#
# What it does: finds Python 3.10+, makes a private environment in .venv (your
# terminal shows "(sous-chef)" when it is active), installs sous-chef into it,
# checks the setup, and on a Mac can start it at login so the app is always
# there. Nothing is installed outside this folder except, if you say yes, a
# login item (~/Library/LaunchAgents/com.sous-chef.web.plist).
set -euo pipefail
cd "$(dirname "$0")"

SERVICE=ask
for arg in "$@"; do
  case "$arg" in
    --service) SERVICE=yes ;;
    --no-service) SERVICE=no ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'
else B=; G=; Y=; R=; D=; N=; fi
step() { printf '\n%s==>%s %s%s%s\n' "$G" "$N" "$B" "$*" "$N"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

ok_python() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

# ── 1. Python ────────────────────────────────────────────────
step "Looking for Python 3.10 or newer"
PY=
for c in python3.13 python3.12 python3.11 python3.10 python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && ok_python "$c"; then PY=$(command -v "$c"); break; fi
done
if [ -z "$PY" ]; then
  found=$(python3 --version 2>/dev/null || echo "no python3")
  if [ "$(uname)" = Darwin ]; then
    die "Found $found, but sous-chef needs Python 3.10 or newer.
  Install it from https://www.python.org/downloads/ (the big yellow button),
  then open a new Terminal window and run ./install.sh again."
  else
    die "Found $found, but sous-chef needs Python 3.10 or newer.
  On Ubuntu/Debian: sudo apt install python3.11 python3.11-venv
  then run ./install.sh again."
  fi
fi
echo "   using $PY ($("$PY" --version))"

# ── 2. Private environment ───────────────────────────────────
step "Setting up the environment in .venv"
if [ -x .venv/bin/python ] && ! ok_python .venv/bin/python; then
  warn "The existing .venv uses an older Python — rebuilding it."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv --prompt sous-chef .venv 2>/dev/null || die "Could not create a virtual environment.
  On Ubuntu/Debian: sudo apt install python3-venv   (then run ./install.sh again)"
fi

# ── 3. Install ───────────────────────────────────────────────
step "Installing sous-chef (a minute or two the first time)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet --editable .
.venv/bin/sous-chef --help >/dev/null || die "Installed, but the sous-chef command does not run."

# ── 4. Background service (macOS) ────────────────────────────
PLIST="$HOME/Library/LaunchAgents/com.sous-chef.web.plist"
if [ "$(uname)" = Darwin ]; then
  if [ -f "$PLIST" ] && [ "$SERVICE" = ask ]; then
    step "Restarting the background service with the new version"
    .venv/bin/sous-chef restart || true
  elif [ -f "$PLIST" ] && [ "$SERVICE" = yes ]; then
    # Rewritten, not just restarted: it records this terminal's PATH, which is
    # how the service finds a `claude` installed since it was first set up.
    step "Reinstalling the background service"
    .venv/bin/sous-chef install-service
  else
    if [ "$SERVICE" = ask ] && [ -t 0 ]; then
      printf '\n%sKeep sous-chef running in the background, starting at login?%s [Y/n] ' "$B" "$N"
      read -r answer || answer=
      case "$answer" in [nN]*) SERVICE=no ;; *) SERVICE=yes ;; esac
    fi
    if [ "$SERVICE" = yes ]; then
      step "Installing the background service"
      .venv/bin/sous-chef install-service
    fi
  fi
fi

# ── 5. Check ─────────────────────────────────────────────────
step "Checking the setup"
.venv/bin/sous-chef doctor || die "Something needs fixing first — see above."

printf '\n%s✓ sous-chef is installed.%s\n\n' "$G$B" "$N"
if [ -f "$PLIST" ]; then
  echo "  Open ${B}http://localhost:8766${N} — it's already running."
  if command -v open >/dev/null 2>&1 && [ -t 1 ]; then open http://localhost:8766 || true; fi
else
  echo "  Start it:   ${B}.venv/bin/sous-chef web${N}"
  echo "  Then open:  ${B}http://localhost:8766${N}"
fi
echo
echo "  ${D}To use the sous-chef command directly in this terminal:  source .venv/bin/activate${N}"
echo "  ${D}To update later:  git pull && ./install.sh${N}"
