#!/usr/bin/env bash
# One-command launcher for illamaRAG: sets up whatever is missing, then runs
# the FastAPI server and the Vite client together. Ctrl+C stops both.
#
#   bash run.sh            # set up if needed, then run
#   bash run.sh --setup    # set up only, do not start anything
#
# Works in Git Bash on Windows as well as on Linux/macOS.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT/server"
CLIENT_DIR="$ROOT/client"
VENV="$SERVER_DIR/.venv"
SERVER_PORT="${SERVER_PORT:-8000}"
CLIENT_PORT="${CLIENT_PORT:-5173}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# In PowerShell/cmd, `bash` is usually C:\Windows\system32\bash.exe -- WSL, not
# Git Bash. A checkout on the Windows drive has a Windows venv and Windows
# node_modules, which Linux cannot use, so hand the run over to Git Bash.
if [[ "$ROOT" == /mnt/[a-z]/* ]] && grep -qi microsoft /proc/version 2>/dev/null; then
  for gitbash in "/mnt/c/Program Files/Git/bin/bash.exe" "/mnt/c/Program Files (x86)/Git/bin/bash.exe"; do
    if [[ -x "$gitbash" ]]; then
      say "Running under WSL -- handing over to Git Bash"
      exec "$gitbash" "$(wslpath -w "$ROOT/run.sh")" "$@"
    fi
  done
  die "This is a Windows checkout but you are in WSL, and Git Bash was not found. Install Git for Windows, or use run.cmd."
fi

# ---- Python -----------------------------------------------------------------

# The venv layout differs between Windows (Scripts/python.exe) and Unix (bin/python).
venv_python() {
  if [[ -x "$VENV/Scripts/python.exe" ]]; then echo "$VENV/Scripts/python.exe"
  elif [[ -x "$VENV/bin/python" ]]; then echo "$VENV/bin/python"
  fi
}

# llama-cpp-python's compiled build is tied to a Python minor version, so 3.11
# is preferred explicitly before falling back to whatever python is on PATH.
find_system_python() {
  if command -v py >/dev/null 2>&1 && py -3.11 -c "" >/dev/null 2>&1; then echo "py -3.11"; return; fi
  for candidate in python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 11))" >/dev/null 2>&1; then
      echo "$candidate"; return
    fi
  done
}

setup_server() {
  local py
  py="$(venv_python)"
  if [[ -z "$py" ]]; then
    local sys_py
    sys_py="$(find_system_python)"
    [[ -n "$sys_py" ]] || die "Python 3.11+ not found. Install Python 3.11 and re-run."
    say "Creating virtualenv with $sys_py"
    $sys_py -m venv "$VENV"
    py="$(venv_python)"
  fi

  # Reinstall only when requirements.txt has changed since the last install.
  local stamp="$VENV/.requirements.sha" want have=""
  want="$("$py" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$SERVER_DIR/requirements.txt")"
  [[ -f "$stamp" ]] && have="$(cat "$stamp")"
  if [[ "$want" != "$have" ]]; then
    say "Installing server requirements"
    "$py" -m pip install --disable-pip-version-check -q -r "$SERVER_DIR/requirements.txt"
    echo "$want" > "$stamp"
  fi

  # Not in requirements.txt (no Windows wheel on PyPI); try the prebuilt CPU
  # wheel index once. See "Installing llama-cpp-python" in the README.
  if ! "$py" -c "import llama_cpp" >/dev/null 2>&1; then
    say "Installing llama-cpp-python from the prebuilt CPU wheel index"
    "$py" -m pip install --disable-pip-version-check -q --only-binary=:all: "llama-cpp-python>=0.3.34" \
      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
      || die "Could not install llama-cpp-python. See 'Installing llama-cpp-python' in README.md."
  fi
  PY="$py"
}

# ---- Node -------------------------------------------------------------------

setup_client() {
  command -v npm >/dev/null 2>&1 || die "npm not found. Install Node 20+ and re-run."
  # Reinstall when node_modules is missing or older than the lockfile.
  if [[ ! -d "$CLIENT_DIR/node_modules" || "$CLIENT_DIR/package-lock.json" -nt "$CLIENT_DIR/node_modules" ]]; then
    say "Installing client dependencies"
    (cd "$CLIENT_DIR" && npm install --no-fund --no-audit)
    touch "$CLIENT_DIR/node_modules"
  fi
}

check_models() {
  [[ -f "$ROOT/models/Qwen3.5-4B-Q4_K_M.gguf" || -f "$SERVER_DIR/.env" ]] \
    || warn "models/Qwen3.5-4B-Q4_K_M.gguf is missing -- chat will fail until it is in place."
  [[ -f "$ROOT/models/nomic-embed-text-v1.5.Q8_0.gguf" || -f "$SERVER_DIR/.env" ]] \
    || warn "models/nomic-embed-text-v1.5.Q8_0.gguf is missing -- uploads and retrieval will fail."
}

# ---- Run --------------------------------------------------------------------

CHILD_PIDS=()

# Git Bash's `kill` does not reach native Windows child processes reliably, so
# on Windows each process tree is ended through taskkill instead.
stop_children() {
  local pid
  for pid in "${CHILD_PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    if [[ -r "/proc/$pid/winpid" ]] && command -v taskkill >/dev/null 2>&1; then
      taskkill //T //F //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1 || true
    else
      kill "$pid" 2>/dev/null || true
    fi
  done
  CHILD_PIDS=()
}

port_in_use() {
  "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$1"
}

run() {
  port_in_use "$SERVER_PORT" && die "Port $SERVER_PORT is already in use -- is the server already running?"
  port_in_use "$CLIENT_PORT" && die "Port $CLIENT_PORT is already in use -- is the client already running?"

  trap 'echo; say "Shutting down"; stop_children' EXIT
  trap 'exit 130' INT TERM

  say "Starting server on http://127.0.0.1:$SERVER_PORT"
  (cd "$SERVER_DIR" && exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$SERVER_PORT") &
  local server_pid=$!
  CHILD_PIDS+=("$server_pid")

  # Wait for the API before starting the client, so the UI never opens to
  # "server offline".
  for _ in $(seq 1 60); do
    kill -0 "$server_pid" 2>/dev/null || die "Server exited during startup -- see the log above."
    port_in_use "$SERVER_PORT" && break
    sleep 0.5
  done
  port_in_use "$SERVER_PORT" || die "Server did not start within 30 seconds."

  say "Starting client -- open http://localhost:$CLIENT_PORT  (Ctrl+C stops both)"
  (cd "$CLIENT_DIR" && API_TARGET="http://127.0.0.1:$SERVER_PORT" exec node node_modules/vite/bin/vite.js --port "$CLIENT_PORT" --strictPort) &
  local client_pid=$!
  CHILD_PIDS+=("$client_pid")

  # Both run in the background so the traps fire promptly; if either one
  # dies, the EXIT trap takes the other down with it.
  while kill -0 "$server_pid" 2>/dev/null && kill -0 "$client_pid" 2>/dev/null; do
    sleep 1
  done
  kill -0 "$server_pid" 2>/dev/null || warn "Server stopped."
  kill -0 "$client_pid" 2>/dev/null || warn "Client stopped."
}

setup_server
setup_client
check_models

if [[ "${1:-}" == "--setup" ]]; then
  say "Setup complete. Start with: bash run.sh"
  exit 0
fi

run
