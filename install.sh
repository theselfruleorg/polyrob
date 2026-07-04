#!/usr/bin/env bash
# install.sh — POLYROB local bootstrap
#
# Usage:
#   bash install.sh [--dir DIR] [--venv-dir VENV_DIR]
#
# Flags:
#   --dir DIR        Source directory containing pyproject.toml (default: directory of this script)
#   --venv-dir VENV  Where to create the virtual env (default: <DIR>/.venv)
#
# Self-host bootstrap: clone the repo, then run `bash install.sh` to get a working
# `polyrob` CLI in a local virtualenv. Safe to re-run (idempotent).
#
# What it does:
#   1. Detects Python ≥ 3.11 (tries python3.13, 3.12, 3.11, then python3)
#   2. Creates a venv at VENV_DIR (re-uses an existing one; never destroys without --reset)
#   3. pip install -e . (uses pyproject.toml which declares the `polyrob` console script)
#   4. Runs `polyrob init --no-prompt` (non-interactive, idempotent)
#   5. Prints activation instructions

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { printf "${BLUE}[polyrob]${NC} %s\n" "$*"; }
success() { printf "${GREEN}[polyrob]${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}[polyrob] WARN:${NC} %s\n" "$*" >&2; }
die()     { printf "${RED}[polyrob] ERROR:${NC} %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}"
VENV_DIR=""
RESET_VENV=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      SRC_DIR="$2";  shift 2 ;;
    --venv-dir) VENV_DIR="$2"; shift 2 ;;
    --reset)    RESET_VENV=true; shift ;;
    -h|--help)
      grep '^#' "$0" | head -20 | sed 's/^# \?//'
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -z "${VENV_DIR}" ]] && VENV_DIR="${SRC_DIR}/.venv"

# ---------------------------------------------------------------------------
# 1. Python ≥ 3.11 detection
# ---------------------------------------------------------------------------
PYTHON_BIN=""

_check_python() {
  local bin="$1"
  if command -v "${bin}" >/dev/null 2>&1; then
    local ver
    ver="$("${bin}" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || true)"
    # ver looks like "(3, 11)" — extract major and minor
    local major minor
    major="$(printf '%s' "${ver}" | tr -d '() ' | cut -d',' -f1)"
    minor="$(printf '%s' "${ver}" | tr -d '() ' | cut -d',' -f2)"
    if [[ -n "${major}" && -n "${minor}" ]]; then
      if [[ "${major}" -gt 3 ]] || { [[ "${major}" -eq 3 ]] && [[ "${minor}" -ge 11 ]]; }; then
        PYTHON_BIN="${bin}"
        return 0
      fi
    fi
  fi
  return 1
}

for _candidate in python3.13 python3.12 python3.11 python3 python; do
  if _check_python "${_candidate}"; then
    break
  fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
  die "Python 3.11 or newer is required but was not found.
Install it from https://www.python.org/downloads/ or via your package manager:
  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3.12"
fi

PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}.{v.micro}")')"
info "Found Python ${PYTHON_VERSION} at $(command -v "${PYTHON_BIN}")"

# ---------------------------------------------------------------------------
# 2. Verify source directory has pyproject.toml
# ---------------------------------------------------------------------------
if [[ ! -f "${SRC_DIR}/pyproject.toml" ]]; then
  die "pyproject.toml not found in ${SRC_DIR}. Pass --dir <path> pointing at the polyrob source root."
fi

# ---------------------------------------------------------------------------
# 3. Create / reuse virtual environment
# ---------------------------------------------------------------------------
if [[ -d "${VENV_DIR}" ]]; then
  if [[ "${RESET_VENV}" == "true" ]]; then
    warn "Removing existing venv at ${VENV_DIR} (--reset requested)"
    rm -rf "${VENV_DIR}"
  else
    info "Reusing existing venv at ${VENV_DIR}  (pass --reset to rebuild)"
  fi
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  info "Creating venv at ${VENV_DIR} ..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# ---------------------------------------------------------------------------
# 4. Activate venv and upgrade pip
# ---------------------------------------------------------------------------
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"

info "Upgrading pip ..."
pip install --quiet --upgrade pip

# ---------------------------------------------------------------------------
# 5. Install the project (editable, with pyproject.toml console script)
# ---------------------------------------------------------------------------
info "Installing polyrob from ${SRC_DIR} ..."
pip install --quiet -e "${SRC_DIR}"

# ---------------------------------------------------------------------------
# 6. Run polyrob init (non-interactive)
# ---------------------------------------------------------------------------
info "Running 'polyrob init --no-prompt' ..."
polyrob init --no-prompt || {
  warn "'polyrob init --no-prompt' exited non-zero (see above). You can re-run it manually."
}

# ---------------------------------------------------------------------------
# 7. Success banner
# ---------------------------------------------------------------------------
success "
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  POLYROB installed successfully!

  Virtual env : ${VENV_DIR}
  Config file : ~/.polyrob/.env

  To activate this venv in a new shell:
    source ${VENV_DIR}/bin/activate

  Then add your API key:
    polyrob config set ANTHROPIC_API_KEY <your-key> --global

  And start the agent:
    polyrob run
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
