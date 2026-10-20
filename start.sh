#!/usr/bin/env bash
# AudioLab launcher for macOS and Linux.
#   ./start.sh              install on first run (or after an update), then open the app
#                           (macOS: adds AudioLab to your Applications folder and opens it from there)
#   ./start.sh --with-ai    also install the optional AI software; models download on first use
#   ./start.sh --reinstall  start over with a fresh environment
#   ./start.sh --terminal   macOS: run the app in this terminal instead (for developers)
# App packages live in ./.venv; uv/Python, app data and AI caches may live elsewhere.
# See docs/INSTALLATION.md before removing the folder: it may contain your output.
# The AudioLab app in Applications runs `start.sh --app`: the same update check, without a
# terminal (progress as notifications, output in .venv/install.log).
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
LOG="$VENV/install.log"
WITH_AI=0
MODE=terminal
[ "$(uname)" = Darwin ] && MODE=handoff
APP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --with-ai) WITH_AI=1 ;;
    --reinstall) rm -rf "$VENV" ;;
    --terminal) MODE=terminal ;;
    --app) MODE=app ;;
    *) APP_ARGS+=("$arg") ;;
  esac
done
# Options for the app itself (--browser, --port …) only work when it runs right here.
[ "$MODE" = handoff ] && [ ${#APP_ARGS[@]} -gt 0 ] && MODE=terminal

# The text goes to AppleScript as an argument, never into its source (folder names may contain quotes).
say() {
  if [ "$MODE" = app ]; then
    osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title "AudioLab"' -e 'end run' "$*" >/dev/null 2>&1 || true
  else
    printf '\033[1m%s\033[0m\n' "$*"
  fi
}

alert() {
  osascript -e 'on run argv' -e 'display alert "AudioLab cannot open" message (item 1 of argv) as critical' -e 'end run' "$*" >/dev/null 2>&1 || true
}

fail() {
  if [ "$MODE" = app ]; then
    alert "$*"
  else
    printf '%s\n' "$*" >&2
  fi
  exit 1
}

# Runs an install step; without a terminal its output goes to the install log.
run() {
  if [ "$MODE" != app ]; then
    "$@"
  elif ! "$@" >>"$LOG" 2>&1; then
    alert "Installing an update failed. Details: $PWD/$LOG"
    exit 1
  fi
}

# A Python between 3.10 and 3.13 (torch wheels lag behind the newest Python).
find_python() {
  for cand in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1; then
        command -v "$cand"; return 0
      fi
    fi
  done
  return 1
}

ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  [ -x "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH" && return 0
  say "AudioLab needs a working Python 3.10–3.13."
  printf 'Install "uv" (a small tool that sets up Python for you, from astral.sh)? [Y/n] '
  read -r answer || answer=""
  case "$answer" in [nN]*) echo "Please install Python 3.12 from https://www.python.org/downloads/ and run ./start.sh again."; exit 1 ;; esac
  command -v curl >/dev/null 2>&1 || fail "Setup needs curl to download uv. Install uv using https://docs.astral.sh/uv/getting-started/installation/ and run the launcher again."
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    fail "Could not install uv. Check the messages above and your internet connection, then run the launcher again. Manual setup: https://docs.astral.sh/uv/getting-started/installation/"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv was not found after setup. Reopen Terminal and run the launcher again. See docs/INSTALLATION.md."
}

pip_install() {
  if command -v uv >/dev/null 2>&1; then
    # --compile-bytecode: done here, not on the first (silent) start of the app
    uv pip install --compile-bytecode --python "$PY" "$@"
  else
    "$PY" -m pip install --disable-pip-version-check "$@"
  fi
}

uv_venv() {
  if [ "$(uname)" = Darwin ]; then
    # uv's own Python, not Homebrew's or python.org's: those are app bundles themselves, so
    # macOS would know the running AudioLab app as "Python" (only its name and icon are patched).
    uv venv --seed --python-preference only-managed --python 3.12 "$VENV"
  else
    uv venv --seed --python 3.12 "$VENV"
  fi
}

make_venv() {
  if command -v uv >/dev/null 2>&1; then
    uv_venv; return
  fi
  if SYS_PY="$(find_python)"; then
    "$SYS_PY" -m venv "$VENV" && return 0
    rm -rf "$VENV"
    say "Python is installed but cannot create environments (Debian/Ubuntu: sudo apt install python3-venv)."
  fi
  ensure_uv
  uv_venv
}

if [ ! -x "$PY" ]; then
  if [ "$MODE" = app ]; then
    alert "AudioLab needs to be set up again: open the AudioLab folder ($PWD) and double-click AudioLab.command."
    exit 1
  fi
  say "Setting up AudioLab (first run; downloads may take several minutes)…"
  make_venv
fi

# An executable can still fail to start (for example, a loader or runtime is broken).
# Probe it before trusting an old install stamp, including a silent Applications launch.
if ! "$PY" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1; then
  fail "AudioLab's Python environment cannot run or is unsupported. Open Terminal in the AudioLab folder ($PWD) and run: bash start.sh --reinstall. See docs/INSTALLATION.md for help. Your saved audio and Library are kept."
fi

# (Re)install when pyproject.toml changed since the last install.
STAMP="$VENV/.audiolab-installed"
HASH="$(cksum pyproject.toml | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  say "Installing AudioLab and its parts (ffmpeg, downloader)…"
  run pip_install -e .
  echo "$HASH" > "$STAMP"
fi

if [ "$WITH_AI" = 1 ]; then
  say "Installing AI software (model files may also download on first use)…"
  run pip_install -e ".[ai]"
fi

if [ "$MODE" = handoff ]; then
  if "$PY" -m audiolab shortcut; then
    open "$HOME/Applications/AudioLab.app"
    say "AudioLab is in your Applications folder now — you can close this window."
    exit 0
  fi
  say "Could not add AudioLab to Applications — opening it from here instead."
fi

exec "$PY" -m audiolab app ${APP_ARGS[@]+"${APP_ARGS[@]}"}
