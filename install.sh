#!/usr/bin/env bash
# install.sh — POLYROB installer
#
# Usage:
#   curl -fsSL https://polyrob.dev/install.sh | bash
#   curl -fsSL https://polyrob.dev/install.sh | bash -s -- --extras server,browser
#   bash install.sh                      # from a clone: install THIS tree
#
# Two modes, decided automatically:
#   managed  — no local tree: clone into $POLYROB_HOME/src, venv in
#              $POLYROB_HOME/venv, command at ~/.local/bin/polyrob
#   source   — a pyproject.toml is beside this script (or --dir): install that
#              tree in place (the developer path this script used to be)
#
# What it does:
#   1. Finds Python >= 3.11, or fetches its own copy under the POLYROB home when
#      this machine has none (checksum-pinned uv; no sudo). Needs git in managed mode.
#   2. Clones or reuses the source tree
#   3. Creates a venv and installs polyrob hash-checked against requirements.lock
#   4. Optionally installs the browser engine (Playwright + Chromium) and
#      first-party packs (packs/*: X, discovery, ...), hash-checked the same way
#   5. Installs a `polyrob` command on your PATH
#   6. Runs a short setup: a key and a model (works inside `curl | bash` — it reads /dev/tty)
#   7. Offers to run the agent as a background service when a chat token was set
#   8. Records what it did in <POLYROB home>/.polyrob-bootstrap.json
#
# Every command's output goes to <POLYROB home>/logs/install.log; a failure prints
# its tail and names the step. Re-running is safe.
#
# Flags:
#   --dir DIR          Source tree to install (implies source mode)
#   --home DIR         POLYROB home (default: $POLYROB_HOME or ~/.polyrob)
#   --venv-dir DIR     Virtualenv location
#   --branch NAME      Branch to clone/update (default: main)
#   --commit REF       Check out an exact commit or release tag (v1.7.4)
#   --extras LIST      Comma-separated pip extras, e.g. server,browser,telegram
#   --browser          Install the browser engine without asking
#   --no-browser       Never install the browser engine
#   --packs LIST       First-party packs whose SDKs to install: ids (x,discovery),
#                        all or none. The packs ship inside polyrob; this adds
#                        each one's extra (x -> twitter, ...). Default: ask once
#                        (interactive), none (piped or --no-prompt).
#                        `polyrob pack list` names what a pack still needs.
#   --no-setup         Skip the setup wizard
#   --no-service       Never offer the background service
#   --no-prompt        Never ask anything (implies --no-browser, --no-service)
#   --no-telemetry     Do not send the install count (DO_NOT_TRACK=1 does the same).
#                        A fresh install that works sends one anonymous count:
#                        version, OS and install method, with no ID. CI runs send none.
#   --verbose          Show every command's output instead of the log file
#   --reset            Recreate the virtualenv from scratch
#   --uninstall        Stop the service, remove the command, the PATH entry, the
#                        venv, the source tree and the private Python. Your data
#                        home is KEPT.
#   -h, --help         Show this help

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()    { printf "${BLUE}[polyrob]${NC} %s\n" "$*"; }
success() { printf "${GREEN}[polyrob]${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}[polyrob] WARN:${NC} %s\n" "$*" >&2; }
die()     { printf "${RED}[polyrob] ERROR:${NC} %s\n" "$*" >&2; exit 1; }

REPO_URL="${POLYROB_REPO_URL:-https://github.com/theselfruleorg/polyrob.git}"

# ---------------------------------------------------------------------------
# Where is this script? `curl | bash` leaves BASH_SOURCE unset, and `set -u`
# turned that into "unbound variable" on line 1 of every piped run.
# ---------------------------------------------------------------------------
SCRIPT_SRC="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_SRC}" && -f "${SCRIPT_SRC}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SRC}")" && pwd)"
else
  SCRIPT_DIR=""   # piped: there is no script directory
fi

# ⚠️ The whole installer is one function, called on the LAST line. `curl | bash`
# runs what it has read so far, so a connection cut mid-script would otherwise
# execute a prefix of it — and a child that reads stdin would swallow the rest of
# the script, because under a pipe stdin IS the script. A function parses whole
# before any of it runs: a truncated download is a syntax error and does nothing.
main() {

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
SRC_DIR=""
SRC_DIR_EXPLICIT=false
POLYROB_HOME_DIR="${POLYROB_HOME:-$HOME/.polyrob}"
VENV_DIR=""
BRANCH="main"
COMMIT=""
EXTRAS=""
WANT_BROWSER=""          # "", "yes", "no"
PACKS=""                 # pack ids, "all" or "none"
PACKS_SET=false          # true = --packs was given (never ask; a failure is fatal)
RUN_SETUP=true
OFFER_SERVICE=true
NO_PROMPT=false
RESET_VENV=false
DO_UNINSTALL=false
VERBOSE=false
SEND_INSTALL_COUNT=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        SRC_DIR="$2"; SRC_DIR_EXPLICIT=true; shift 2 ;;
    --home)       POLYROB_HOME_DIR="$2"; shift 2 ;;
    --venv-dir)   VENV_DIR="$2"; shift 2 ;;
    --branch)     BRANCH="$2"; shift 2 ;;
    --commit)     COMMIT="$2"; shift 2 ;;
    --extras)     EXTRAS="$2"; shift 2 ;;
    --browser)    WANT_BROWSER="yes"; shift ;;
    --no-browser) WANT_BROWSER="no"; shift ;;
    --packs)      PACKS="$2"; PACKS_SET=true; shift 2 ;;
    --no-setup)   RUN_SETUP=false; shift ;;
    --no-service) OFFER_SERVICE=false; shift ;;
    --no-prompt|--non-interactive)
                  NO_PROMPT=true; shift ;;
    --reset)      RESET_VENV=true; shift ;;
    --verbose)    VERBOSE=true; shift ;;
    --no-telemetry) SEND_INSTALL_COUNT=false; shift ;;
    --uninstall)  DO_UNINSTALL=true; shift ;;
    -h|--help)
      if [[ -n "${SCRIPT_SRC}" && -f "${SCRIPT_SRC}" ]]; then
        awk '/^#/{print substr($0,3); next} {exit}' "${SCRIPT_SRC}"
      else
        echo "polyrob installer. Flags: --dir --home --venv-dir --branch --commit"
        echo "  --extras LIST --browser --no-browser --packs LIST --no-setup --no-service"
        echo "  --no-prompt --reset --verbose --no-telemetry --uninstall.  Details: https://polyrob.dev/install"
      fi
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

if [[ "${NO_PROMPT}" == "true" ]]; then
  [[ -z "${WANT_BROWSER}" ]] && WANT_BROWSER="no"
  OFFER_SERVICE=false
fi

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die "unsupported platform: $(uname -s). POLYROB runs on Linux, macOS and WSL2. On Windows, install WSL2 and run this inside it." ;;
esac

# A terminal for prompts: our own stdin when it is a TTY, else /dev/tty — which
# is what makes the wizard work inside `curl | bash`. Probe by OPENING it: in a
# Docker build the device node exists and the open fails.
TTY_DEV=""
if [[ -t 0 ]]; then
  TTY_DEV="/dev/stdin"
elif (: </dev/tty) 2>/dev/null; then
  TTY_DEV="/dev/tty"
fi

ask_yes_no() {  # ask_yes_no "Question" default(yes|no)
  local q="$1" def="${2:-no}" ans="" suffix="[y/N]"
  [[ "${def}" == "yes" ]] && suffix="[Y/n]"
  if [[ "${NO_PROMPT}" == "true" || -z "${TTY_DEV}" ]]; then
    [[ "${def}" == "yes" ]] && return 0 || return 1
  fi
  printf "%s %s " "${q}" "${suffix}" > /dev/tty 2>/dev/null || true
  IFS= read -r ans < "${TTY_DEV}" || ans=""
  ans="$(printf '%s' "${ans}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ -z "${ans}" ]] && { [[ "${def}" == "yes" ]] && return 0 || return 1; }
  case "${ans}" in y|yes) return 0 ;; *) return 1 ;; esac
}

ask_line() {  # ask_line "Question" -> the answer on stdout ("" when nobody can answer)
  local ans=""
  if [[ "${NO_PROMPT}" == "true" || -z "${TTY_DEV}" ]]; then
    return 0
  fi
  printf "%s " "$1" > /dev/tty 2>/dev/null || true
  IFS= read -r ans < "${TTY_DEV}" || ans=""
  printf '%s' "${ans}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'
}

# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
if [[ "${SRC_DIR_EXPLICIT}" != "true" ]]; then
  if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/pyproject.toml" ]]; then
    SRC_DIR="${SCRIPT_DIR}"          # run from a clone
  else
    SRC_DIR="${POLYROB_HOME_DIR}/src"  # managed install
  fi
fi
MODE="source"
[[ "${SRC_DIR}" == "${POLYROB_HOME_DIR}/src" ]] && MODE="managed"

if [[ -z "${VENV_DIR}" ]]; then
  if [[ "${MODE}" == "managed" ]]; then VENV_DIR="${POLYROB_HOME_DIR}/venv"
  else VENV_DIR="${SRC_DIR}/.venv"; fi
fi

# Where the `polyrob` command goes.
if [[ "$(id -u)" == "0" && "$(uname -s)" == "Linux" ]]; then
  BIN_DIR="/usr/local/bin"
else
  BIN_DIR="${HOME}/.local/bin"
fi
SHIM="${BIN_DIR}/polyrob"

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
rc_files() {
  printf '%s\n' "${HOME}/.zshrc" "${HOME}/.zprofile" "${HOME}/.bashrc" "${HOME}/.bash_profile" \
                "${HOME}/.bash_login" "${HOME}/.profile" "${HOME}/.config/fish/config.fish"
}

remove_path_block() {
  local f
  while IFS= read -r f; do
    [[ -f "${f}" ]] || continue
    grep -q '^# >>> polyrob >>>' "${f}" 2>/dev/null || continue
    local tmp="${f}.polyrob.tmp"
    awk '/^# >>> polyrob >>>/{skip=1} !skip{print} /^# <<< polyrob <<</{skip=0}' \
      "${f}" > "${tmp}" && mv -f "${tmp}" "${f}"
    info "Removed the PATH entry from ${f}"
  done < <(rc_files)
}

if [[ "${DO_UNINSTALL}" == "true" ]]; then
  info "Uninstalling polyrob (your data home ${POLYROB_HOME_DIR} is KEPT)"
  # A service left behind restarts forever against a venv that no longer exists.
  if [[ -x "${VENV_DIR}/bin/python" ]] \
     && POLYROB_HOME="${POLYROB_HOME_DIR}" "${VENV_DIR}/bin/python" -m cli.polyrob service uninstall --help >/dev/null 2>&1; then
    POLYROB_HOME="${POLYROB_HOME_DIR}" "${VENV_DIR}/bin/python" -m cli.polyrob service uninstall </dev/null \
      || warn "could not remove the background service; check: systemctl --user status polyrob"
  fi
  if [[ -e "${SHIM}" || -L "${SHIM}" ]]; then rm -f "${SHIM}"; info "Removed ${SHIM}"; fi
  remove_path_block
  if [[ -d "${VENV_DIR}" ]]; then rm -rf "${VENV_DIR}"; info "Removed ${VENV_DIR}"; fi
  if [[ "${MODE}" == "managed" && -d "${SRC_DIR}" ]]; then
    rm -rf "${SRC_DIR}"; info "Removed ${SRC_DIR}"
  fi
  for _own in "${POLYROB_HOME_DIR}/python" "${POLYROB_HOME_DIR}/tools"; do
    if [[ -d "${_own}" ]]; then rm -rf "${_own}"; info "Removed ${_own}"; fi
  done
  success "Uninstalled. Data, keys and memory remain in ${POLYROB_HOME_DIR}."
  echo   "  Delete them yourself if you mean to: rm -rf ${POLYROB_HOME_DIR}"
  echo   "  (that directory holds the agent wallet seed — read it before you delete it)"
  exit 0
fi

# ---------------------------------------------------------------------------
# The install count. One anonymous signal when a fresh install works, and nothing
# on a re-run: that is what lets it carry no ID. Said here, at the start, because
# this is when the reader can still stop and add the flag.
# ---------------------------------------------------------------------------
case "${DO_NOT_TRACK:-}" in ""|0) ;; *) SEND_INSTALL_COUNT=false ;; esac
[[ -z "${CI:-}" ]] || SEND_INSTALL_COUNT=false
[[ ! -e "${SHIM}" ]] || SEND_INSTALL_COUNT=false
if [[ "${SEND_INSTALL_COUNT}" == "true" ]]; then
  info "When it finishes, this install sends one anonymous count: version, OS and install method, with no ID."
  info "To turn it off, use --no-telemetry or DO_NOT_TRACK=1. Details: https://polyrob.dev/privacy"
fi

send_install_count() {
  [[ "${SEND_INSTALL_COUNT}" == "true" ]] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  local version os
  version="$(printf '%s' "$1" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  # Short limits and no retry: this must never slow or stop an install.
  curl -fsS --connect-timeout 2 --max-time 4 -o /dev/null \
    -d "event=completed&version=${version}&os=${os}&method=${MODE}" \
    "https://polyrob.dev/api/install-event" </dev/null >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Failure reporting. Every step names itself, its output goes to the log, and a
# failure says which step stopped and that a re-run is safe — `set -e` alone
# leaves the reader at a silent prompt after a failed clone.
# ---------------------------------------------------------------------------
INSTALL_LOG="${POLYROB_HOME_DIR}/logs/install.log"
mkdir -p "$(dirname "${INSTALL_LOG}")" 2>/dev/null || INSTALL_LOG="$(mktemp "${TMPDIR:-/tmp}/polyrob-install.XXXXXX")"
: >> "${INSTALL_LOG}" 2>/dev/null || INSTALL_LOG="$(mktemp "${TMPDIR:-/tmp}/polyrob-install.XXXXXX")"
export PIP_DISABLE_PIP_VERSION_CHECK=1
CURRENT_STEP="start"
on_exit() {
  local rc=$?
  [[ "${rc}" -eq 0 ]] && return 0
  printf "${RED}[polyrob] ERROR:${NC} the install stopped at step '%s' (exit %s).\n" "${CURRENT_STEP}" "${rc}" >&2
  printf '  Log: %s\n  Re-running the installer is safe. Add --verbose to watch every command.\n' "${INSTALL_LOG}" >&2
}
trap on_exit EXIT

quietly() {  # quietly LABEL CMD... — the output goes to the log; a failure prints its tail
  local label="$1"; shift
  if [[ "${VERBOSE}" == "true" ]]; then
    "$@" </dev/null
    return $?
  fi
  local start rc=0
  start="$(( $(wc -l < "${INSTALL_LOG}") + 1 ))"
  printf '==> %s (%s)\n' "${label}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${INSTALL_LOG}"
  "$@" </dev/null >> "${INSTALL_LOG}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    warn "${label} failed (exit ${rc}). Last output:"
    tail -n +"$(( start + 1 ))" "${INSTALL_LOG}" | tail -n 20 | sed 's/^/    /' >&2
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 1. Python >= 3.11
# ---------------------------------------------------------------------------
CURRENT_STEP="python"
PYTHON_BIN=""
_check_python() {
  local bin="$1" ver major minor
  command -v "${bin}" >/dev/null 2>&1 || return 1
  ver="$("${bin}" -c 'import sys; print("%d %d" % sys.version_info[:2])' 2>/dev/null || true)"
  [[ -z "${ver}" ]] && return 1
  major="${ver%% *}"; minor="${ver##* }"
  if [[ "${major}" -gt 3 ]] || { [[ "${major}" -eq 3 ]] && [[ "${minor}" -ge 11 ]]; }; then
    PYTHON_BIN="${bin}"; return 0
  fi
  return 1
}

# A private Python for a machine that has no usable one: Ubuntu 22.04 ships 3.10,
# macOS ships 3.9, Debian and Ubuntu split `venv` into a package only sudo can add.
# uv is the exact artifact pinned below, checked against its checksum, and everything
# it fetches stays under the POLYROB home. Python's own bytes come from uv's pinned
# release metadata, and a machine that already has a usable Python never gets here.
UV_VERSION="0.13.7"
uv_pin() {  # uv_pin <target> -> the sha256 of that target's archive
  case "$1" in
    x86_64-unknown-linux-gnu)    echo "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83" ;;
    aarch64-unknown-linux-gnu)   echo "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc" ;;
    x86_64-unknown-linux-musl)   echo "287491da03f2a785c70fa75b13e1f849918c2f2b5415fe84876b8214eeb792f9" ;;
    aarch64-unknown-linux-musl)  echo "2d3ec076a0c4c0e8250bb6418c44080828b544f1a3be0b6e46adf9fb6923de45" ;;
    x86_64-apple-darwin)         echo "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7" ;;
    aarch64-apple-darwin)        echo "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731" ;;
    *) return 1 ;;
  esac
}

uv_target() {
  local arch libc="gnu"
  case "$(uname -m)" in
    arm64|aarch64) arch="aarch64" ;;
    x86_64|amd64)  arch="x86_64" ;;
    *) return 1 ;;
  esac
  case "$(uname -s)" in
    Darwin) echo "${arch}-apple-darwin"; return 0 ;;
    Linux)
      if compgen -G '/lib/ld-musl-*.so.1' >/dev/null; then libc="musl"; fi
      echo "${arch}-unknown-linux-${libc}"
      ;;
    *) return 1 ;;
  esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else return 1; fi
}

# 3.12 first: it is what CI and the classifiers cover beside 3.11. Fails with a
# reason on stderr; never exits, so the caller can name the system-package remedy.
provision_python() {
  local target want digest tmp uv_bin
  target="$(uv_target)" && want="$(uv_pin "${target}")" || {
    warn "no pinned uv build for $(uname -s) $(uname -m)."; return 1; }
  uv_bin="${POLYROB_HOME_DIR}/tools/uv-${UV_VERSION}/uv"
  if [[ ! -x "${uv_bin}" ]]; then
    command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || {
      warn "curl and tar are needed to fetch a private Python."; return 1; }
    info "Fetching uv ${UV_VERSION} to get a private Python ..."
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/polyrob-uv.XXXXXX")"
    if ! quietly "Downloading uv" curl -fsSL --retry 3 \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz" -o "${tmp}/uv.tar.gz"; then
      rm -rf "${tmp}"; return 1
    fi
    digest="$(sha256_of "${tmp}/uv.tar.gz")" || { rm -rf "${tmp}"; warn "no sha256 tool to check the uv download."; return 1; }
    if [[ "${digest}" != "${want}" ]]; then
      rm -rf "${tmp}"; warn "uv checksum mismatch (expected ${want}, got ${digest}). Refusing to run it."; return 1
    fi
    tar -xzf "${tmp}/uv.tar.gz" -C "${tmp}" || { rm -rf "${tmp}"; warn "could not unpack uv."; return 1; }
    mkdir -p "$(dirname "${uv_bin}")"
    mv "${tmp}/uv-${target}/uv" "${uv_bin}" && chmod +x "${uv_bin}"
    rm -rf "${tmp}"
  fi
  info "Installing a private Python 3.12 into ${POLYROB_HOME_DIR}/python ..."
  quietly "Installing Python 3.12" env UV_NO_CONFIG=1 "${uv_bin}" python install --no-bin --no-registry \
      --install-dir "${POLYROB_HOME_DIR}/python" 3.12 || return 1
  local found
  found="$(env UV_NO_CONFIG=1 UV_PYTHON_INSTALL_DIR="${POLYROB_HOME_DIR}/python" "${uv_bin}" python find --managed-python 3.12 2>/dev/null)" || {
    warn "uv installed Python but could not find it."; return 1; }
  _check_python "${found}" || { warn "the private Python at ${found} does not run on this machine."; return 1; }
}

no_python_remedy() {
  cat <<NOPY
Python 3.11 or newer is required and a private copy could not be installed.
Install it from https://www.python.org/downloads/ or with your package manager:
  macOS:         brew install python@3.12
  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv
  Fedora:        sudo dnf install python3.12
NOPY
}

for _c in python3.12 python3.11 python3.13 python3 python; do
  _check_python "${_c}" && break
done
if [[ -z "${PYTHON_BIN}" ]]; then
  info "No Python 3.11+ found on this machine."
  provision_python || die "$(no_python_remedy)"
fi

PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}.{v.micro}")')"
info "Python ${PYTHON_VERSION} at $(command -v "${PYTHON_BIN}")"

json_esc() { local v="${1//\\/\\\\}"; printf '%s' "${v//\"/\\\"}"; }
STAGES=""   # JSON fragments, joined at the end
stage() {   # stage NAME ok|skipped REASON
  local entry
  entry="$(printf '{"name":"%s","status":"%s","detail":"%s"}' "$1" "$2" "$(json_esc "$3")")"
  if [[ -z "${STAGES}" ]]; then STAGES="${entry}"; else STAGES="${STAGES},${entry}"; fi
}
stage python ok "${PYTHON_VERSION}"

# ---------------------------------------------------------------------------
# 2. Source tree
# ---------------------------------------------------------------------------
CURRENT_STEP="repository"
git_remedy() {
  case "$(uname -s)" in
    Darwin) echo "  macOS: run xcode-select --install, finish Apple's dialog, then run this installer again" ;;
    *)      echo "  Debian/Ubuntu: sudo apt install git   Fedora: sudo dnf install git   Alpine: apk add git" ;;
  esac
}
if [[ "${MODE}" == "managed" ]]; then
  # macOS ships /usr/bin/git as a stub until the Command Line Tools exist: it is on
  # PATH and every call fails, so `command -v git` alone says yes to a broken git.
  command -v git >/dev/null 2>&1 || die "git is required to install POLYROB.
$(git_remedy)"
  git --version >/dev/null 2>&1 || die "git is on your PATH but does not run.
$(git_remedy)"
  # A killed clone can leave a .git with no commit: every later step fails on it.
  # Move it aside, never delete it: it may hold something the owner wants.
  if [[ -e "${SRC_DIR}/.git" ]] && ! git -C "${SRC_DIR}" rev-parse --verify HEAD >/dev/null 2>&1; then
    _broken="${SRC_DIR}.broken-$(date -u +%Y%m%d-%H%M%S)"
    warn "${SRC_DIR} is not a usable checkout; moving it aside to ${_broken}"
    mv "${SRC_DIR}" "${_broken}"
  fi
  if [[ -d "${SRC_DIR}/.git" ]]; then
    info "Updating ${SRC_DIR} (branch ${BRANCH}) ..."
    if [[ -n "$(git -C "${SRC_DIR}" status --porcelain 2>/dev/null)" ]]; then
      die "${SRC_DIR} has local changes. Commit, stash or remove them, or pass --dir to install a different tree."
    fi
    quietly "Fetching ${BRANCH}" git -C "${SRC_DIR}" fetch --quiet origin "${BRANCH}"
    quietly "Checking out ${BRANCH}" git -C "${SRC_DIR}" checkout --quiet "${BRANCH}"
    quietly "Fast-forwarding ${BRANCH}" git -C "${SRC_DIR}" merge --ff-only --quiet "origin/${BRANCH}"
    stage repository ok "updated ${BRANCH}"
  else
    if [[ -e "${SRC_DIR}" ]] && [[ -n "$(ls -A "${SRC_DIR}" 2>/dev/null)" ]]; then
      die "${SRC_DIR} exists and is not a git checkout. Move it aside, or pass --home to install elsewhere."
    fi
    info "Downloading POLYROB into ${SRC_DIR} ..."
    mkdir -p "$(dirname "${SRC_DIR}")"
    # Staged, then moved: an interrupted clone must never look like a checkout.
    _staged="$(mktemp -d "$(dirname "${SRC_DIR}")/.polyrob-clone-XXXXXX")"
    _cloned=false
    for _attempt in 1 2 3; do
      if quietly "Cloning ${REPO_URL} (attempt ${_attempt} of 3)" \
          git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${_staged}/tree"; then
        _cloned=true; break
      fi
      rm -rf "${_staged}/tree"
      [[ "${_attempt}" == "3" ]] || sleep "$(( _attempt * 3 ))"
    done
    if [[ "${_cloned}" != "true" ]]; then
      rm -rf "${_staged}"
      die "could not download ${REPO_URL} after 3 attempts. Check the network, then run this installer again."
    fi
    [[ ! -d "${SRC_DIR}" ]] || rmdir "${SRC_DIR}"
    mv "${_staged}/tree" "${SRC_DIR}"
    rmdir "${_staged}"
    stage repository ok "cloned ${BRANCH}"
  fi
  if [[ -n "${COMMIT}" ]]; then
    # A shallow clone has neither the tag nor the commit: fetch the one named. The
    # tag refspec comes first, because a by-name fetch writes only FETCH_HEAD.
    git -C "${SRC_DIR}" fetch --quiet --depth 1 origin "+refs/tags/${COMMIT}:refs/tags/${COMMIT}" 2>/dev/null \
      || git -C "${SRC_DIR}" fetch --quiet --depth 1 origin "${COMMIT}" 2>/dev/null \
      || git -C "${SRC_DIR}" fetch --quiet --unshallow 2>/dev/null \
      || git -C "${SRC_DIR}" fetch --quiet origin
    git -C "${SRC_DIR}" checkout --quiet --detach "${COMMIT}" \
      || die "commit or tag ${COMMIT} not found in ${REPO_URL}"
    info "Pinned to ${COMMIT}"
  fi
else
  [[ -f "${SRC_DIR}/pyproject.toml" ]] || die "pyproject.toml not found in ${SRC_DIR}.
Pass --dir <path> pointing at the polyrob source root, or drop --dir to install
a managed copy into ${POLYROB_HOME_DIR}/src."
  stage repository ok "local tree ${SRC_DIR}"
fi

# >>> root-trust >>>
# As root the shim goes into /usr/local/bin and runs for every user, so the
# code it executes must be root's: refuse a source or venv tree (or any of its
# ancestors) that another user owns or can write (OPS-15).
untrusted_root_path() {   # prints the first untrusted ancestor of $1, if any
  "${PYTHON_BIN}" -I -c '
import os, stat, sys
p = os.path.abspath(sys.argv[1])
while not os.path.lexists(p):
    p = os.path.dirname(p)
while True:
    st = os.lstat(p)
    if st.st_uid != 0 or (st.st_mode & 0o022 and not stat.S_ISLNK(st.st_mode)):
        print(p); break
    if p == os.path.dirname(p):
        break
    p = os.path.dirname(p)
' "$1"
}
# <<< root-trust <<<
if [[ "$(id -u)" == "0" && "$(uname -s)" == "Linux" ]]; then
  for _tree in "${SRC_DIR}" "${VENV_DIR}"; do
    _bad="$(untrusted_root_path "${_tree}")"
    [[ -z "${_bad}" ]] || die "refusing a root install from ${_tree}: ${_bad} is not root-owned or is group/world-writable.
Install as the user who owns the tree, or copy it to a root-owned directory first."
  done
fi

# ---------------------------------------------------------------------------
# 3. Virtualenv + install
# ---------------------------------------------------------------------------
CURRENT_STEP="virtualenv"
VPY="${VENV_DIR}/bin/python"
# Before anything runs in the venv: with no lock there is nothing it may install.
[[ -f "${SRC_DIR}/requirements.lock" ]] \
  || die "requirements.lock is missing from ${SRC_DIR}; refusing unhashed dependency installation. Use a complete source checkout."
if [[ -d "${VENV_DIR}" && "${RESET_VENV}" == "true" ]]; then
  warn "Removing ${VENV_DIR} (--reset)"
  rm -rf "${VENV_DIR}"
fi
# A Python upgrade leaves a dangling interpreter, and a killed run leaves a venv
# with no pip. Neither holds anything: rebuild it instead of failing on it forever.
if [[ -d "${VENV_DIR}" ]] && ! "${VPY}" -c 'import pip' >/dev/null 2>&1; then
  # A venv in a source checkout is the person's own (`uv venv` makes one with no pip).
  [[ "${MODE}" == "managed" ]] \
    || die "${VENV_DIR} has no working pip, and it is not ours to delete. Add pip to it, or re-run with --reset to rebuild it."
  warn "${VENV_DIR} does not work (a moved or upgraded Python, or an interrupted run). Rebuilding it."
  warn "Pass --extras again for any extra that it held."
  rm -rf "${VENV_DIR}"
fi
if [[ ! -d "${VENV_DIR}" ]]; then
  info "Creating venv at ${VENV_DIR} ..."
  if ! quietly "Creating the virtualenv" "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
    rm -rf "${VENV_DIR}"
    # Debian and Ubuntu ship Python without `venv`; adding it needs sudo. A private
    # Python has it, so the install does not stop on a package it cannot add.
    info "This Python cannot create a virtualenv. Trying a private Python ..."
    provision_python \
      && quietly "Creating the virtualenv" "${PYTHON_BIN}" -m venv "${VENV_DIR}" \
      || { rm -rf "${VENV_DIR}"; die "could not create a virtualenv. On Debian/Ubuntu install the venv module:
  sudo apt install python3-venv"; }
  fi
else
  info "Reusing venv at ${VENV_DIR}  (pass --reset to rebuild)"
fi
[[ -x "${VPY}" ]] || die "virtualenv interpreter missing at ${VPY}. Run this installer again with --reset."

CURRENT_STEP="dependencies"
info "Using the virtualenv bundled pip (no network bootstrap)"

# 066 P1 / D2: every dependency is hash-checked against requirements.lock. pip's
# hash mode cannot hash the project itself, so the install is two steps: (1) the
# lock's closure of the chosen extras, --require-hashes; (2) the project,
# --no-deps --no-build-isolation (its build backend came hashed in step 1). A
# source build stays allowed where a platform has no wheel (a production box is
# wheel-only: scripts/deploy_prod.sh). A missing lock refuses the install.
hashed_install() {
  local extras="$1" deps rc=0
  if [[ ! -f "${SRC_DIR}/requirements.lock" ]]; then
    warn "requirements.lock is missing from ${SRC_DIR}; refusing unhashed dependency installation. Use a complete source checkout."
    return 1
  fi
  deps="$(mktemp "${TMPDIR:-/tmp}/polyrob-deps.XXXXXX")"
  "${VPY}" "${SRC_DIR}/core/lock_closure.py" deps --lock "${SRC_DIR}/requirements.lock" \
      --pyproject "${SRC_DIR}/pyproject.toml" --extras "${extras}" -o "${deps}" \
    && quietly "Installing hash-checked dependencies" "${VPY}" -m pip install --require-hashes --prefer-binary -r "${deps}" \
    && quietly "Installing polyrob" "${VPY}" -m pip install --no-deps --no-build-isolation -e "${SRC_DIR}" \
    && retire_pack_dists \
    || rc=$?
  rm -f "${deps}"
  return "${rc}"
}

# 067 (one install): a reused venv may hold the retired separate first-party pack
# distributions (a second provider of the pack entry points, pinned to an old
# core). AFTER the project install, core/packs/retire.py removes their METADATA
# only (never a pack file — polyrob owns those now) and verifies one provider per
# pack. It is stdlib-only and ships in the public tree; an older tree has none.
retire_pack_dists() {
  [[ -f "${SRC_DIR}/core/packs/retire.py" ]] || return 0
  "${VPY}" "${SRC_DIR}/core/packs/retire.py" || return $?
  # Dropped third-party dependencies (core/retired_deps.py) whose pins would
  # fail `pip check` on a re-run over an existing venv. Named, never fatal.
  [[ -f "${SRC_DIR}/core/retired_deps.py" ]] || return 0
  "${VPY}" "${SRC_DIR}/core/retired_deps.py" || warn "a retired dependency is still installed (see above)"
}

info "Installing polyrob from ${SRC_DIR}${EXTRAS:+ with extras: ${EXTRAS}} (hash-checked). This takes about a minute ..."
hashed_install "${EXTRAS}"
stage python-deps ok "${EXTRAS:-base}"

# ---------------------------------------------------------------------------
# 4. Browser engine (opt-in — it downloads ~150 MB of Chromium)
# ---------------------------------------------------------------------------
CURRENT_STEP="browser"
if [[ -z "${WANT_BROWSER}" ]]; then
  if ask_yes_no "Install the browser engine for web automation (~150 MB)?" no; then
    WANT_BROWSER="yes"
  else
    WANT_BROWSER="no"
  fi
fi
if [[ "${WANT_BROWSER}" == "yes" ]]; then
  info "Installing the browser extra and Chromium ..."
  if hashed_install browser && quietly "Downloading Chromium" "${VPY}" -m playwright install chromium; then
    stage browser ok "playwright + chromium"
    if [[ "$(uname -s)" == "Linux" ]]; then
      info "If the browser cannot start, add its system libraries: sudo ${VPY} -m playwright install-deps chromium"
    fi
  else
    warn "browser install failed — run the installer again with --browser (log: ${INSTALL_LOG})"
    stage browser skipped "install failed"
  fi
else
  stage browser skipped "not requested — later: polyrob doctor names the command"
fi

# ---------------------------------------------------------------------------
# 4b. First-party pack SDKs (067, one install). The packs ship INSIDE polyrob
# (already installed above); what a pack may lack is the extra that carries its
# SDKs (x -> twitter, discovery -> anysite, markets -> crypto; each pack.toml
# names it). core/lock_closure.py maps ids -> extras and writes their hashed
# closure, so a new pack needs no edit here.
# ---------------------------------------------------------------------------
pack_install() {
  local ids="$1" deps rc=0
  deps="$(mktemp "${TMPDIR:-/tmp}/polyrob-pack-deps.XXXXXX")"
  "${VPY}" "${SRC_DIR}/core/lock_closure.py" deps --lock "${SRC_DIR}/requirements.lock" \
       --pyproject "${SRC_DIR}/pyproject.toml" --extras "${EXTRAS}" --packs "${ids}" -o "${deps}" \
    && quietly "Installing pack dependencies" "${VPY}" -m pip install --require-hashes --prefer-binary -r "${deps}" \
    || rc=$?
  rm -f "${deps}"
  return "${rc}"
}

CURRENT_STEP="packs"
AVAILABLE_PACKS=""
if [[ -f "${SRC_DIR}/core/lock_closure.py" ]]; then
  AVAILABLE_PACKS="$("${VPY}" "${SRC_DIR}/core/lock_closure.py" packs --root "${SRC_DIR}" --ids all --list 2>/dev/null \
    | awk '{print $1}' | paste -sd, - || true)"
fi
if [[ "${PACKS_SET}" != "true" && -n "${AVAILABLE_PACKS}" ]]; then
  PACKS="$(ask_line "Packs ship with polyrob; their SDKs are optional: ${AVAILABLE_PACKS}. Install which? (comma list, all, or Enter for none)")"
fi
case "${PACKS}" in none|n|no) PACKS="" ;; esac
if [[ -n "${PACKS}" ]] && ! "${VPY}" "${SRC_DIR}/core/lock_closure.py" packs --root "${SRC_DIR}" --ids "${PACKS}" >/dev/null 2>&1; then
  # A mistyped answer at a prompt must not end an install that already succeeded.
  [[ "${PACKS_SET}" == "true" ]] || { warn "no pack named '${PACKS}'; skipping. Add one later: polyrob pack list"; PACKS=""; }
  [[ -z "${PACKS}" ]] || die "unknown pack in '${PACKS}'. This tree has: ${AVAILABLE_PACKS:-no packs}"
fi
if [[ -n "${PACKS}" ]]; then
  info "Installing the SDKs of packs: ${PACKS} (hash-checked) ..."
  if pack_install "${PACKS}"; then
    stage packs ok "${PACKS}"
  elif [[ "${PACKS_SET}" == "true" ]]; then
    die "pack install failed (--packs ${PACKS}). Re-run with --packs to retry."
  else
    warn "pack install failed — re-run the installer with --packs ${PACKS}"
    stage packs skipped "install failed"
  fi
else
  stage packs skipped "none requested${AVAILABLE_PACKS:+ — available: ${AVAILABLE_PACKS}}"
fi

# Optional host tools we only ever DETECT. An absent one is slower or narrower,
# never broken, so the installer names it and never runs a package manager.
MISSING_HOST_TOOLS=""
for _bin in rg ffmpeg; do
  command -v "${_bin}" >/dev/null 2>&1 || MISSING_HOST_TOOLS="${MISSING_HOST_TOOLS} ${_bin}"
done
stage host-tools ok "missing:${MISSING_HOST_TOOLS:- none}"

# ---------------------------------------------------------------------------
# 5. The `polyrob` command
# ---------------------------------------------------------------------------
CURRENT_STEP="command"
mkdir -p "${BIN_DIR}"
# rm first: an older install may have left a SYMLINK here, and `cat >` would
# follow it and overwrite the venv's own console script.
rm -f "${SHIM}"
# --home must outlive the install: without this line the wizard writes the keys to
# the chosen home and every later `polyrob` reads ~/.polyrob and finds none.
SHIM_HOME_LINE=""
if [[ "${POLYROB_HOME_DIR}" != "${HOME}/.polyrob" ]]; then
  SHIM_HOME_LINE="export POLYROB_HOME=\"\${POLYROB_HOME:-${POLYROB_HOME_DIR}}\"
"
fi
cat > "${SHIM}" <<SHIM_EOF
#!/usr/bin/env bash
# POLYROB launcher (written by install.sh). PYTHONPATH/PYTHONHOME are cleared so
# an inherited value cannot make this import a different checkout.
unset PYTHONPATH
unset PYTHONHOME
${SHIM_HOME_LINE}exec "${VPY}" -I -m cli.polyrob "\$@"
SHIM_EOF
chmod +x "${SHIM}"
success "Installed the polyrob command → ${SHIM}"
stage command ok "${SHIM}"

write_path_block() {  # write_path_block <rc> <line>
  local rc="$1" line="$2"
  mkdir -p "$(dirname "${rc}")"
  if [[ -f "${rc}" ]] && grep -q '^# >>> polyrob >>>' "${rc}"; then
    return 0
  fi
  {
    echo ""
    echo "# >>> polyrob >>>"
    echo "${line}"
    echo "# <<< polyrob <<<"
  } >> "${rc}"
  info "Added ${BIN_DIR} to your PATH in ${rc}"
}

# A login shell and an interactive shell read different files: an SSH session, a new
# macOS Terminal window and a desktop terminal must all find the command.
add_path_block() {
  case ":${PATH}:" in *":${BIN_DIR}:"*) stage path ok "already on PATH"; return 0 ;; esac
  local shell_name guarded rc
  shell_name="$(basename "${SHELL:-/bin/bash}")"
  guarded="case \":\$PATH:\" in *\":${BIN_DIR}:\"*) ;; *) export PATH=\"${BIN_DIR}:\$PATH\" ;; esac"
  case "${shell_name}" in
    zsh)
      write_path_block "${HOME}/.zshrc" "${guarded}"
      write_path_block "${HOME}/.zprofile" "${guarded}"
      RELOAD_HINT="${HOME}/.zshrc"
      ;;
    fish)
      write_path_block "${HOME}/.config/fish/config.fish" "set -gx PATH ${BIN_DIR} \$PATH"
      RELOAD_HINT="${HOME}/.config/fish/config.fish"
      ;;
    *)
      write_path_block "${HOME}/.bashrc" "${guarded}"
      # A login bash reads only the FIRST of these that exists, so a line in
      # ~/.profile is hidden when ~/.bash_profile is present.
      write_path_block "${HOME}/.profile" "${guarded}"
      for rc in "${HOME}/.bash_profile" "${HOME}/.bash_login"; do
        [[ -f "${rc}" ]] && write_path_block "${rc}" "${guarded}"
      done
      RELOAD_HINT="${HOME}/.bashrc"
      ;;
  esac
  stage path ok "${RELOAD_HINT}"
}
RELOAD_HINT=""
if [[ "${BIN_DIR}" == "/usr/local/bin" ]]; then
  stage path ok "/usr/local/bin is already on every shell's PATH"
else
  add_path_block
fi

# The installer is published from the site and clones whatever is on the public
# repo's branch, so it can be NEWER than the code it installs. Advertising a
# verb the installed CLI does not have (or worse, CALLING one) is a promise the
# artifact cannot keep — `polyrob setup` did not exist in 1.1.0 and an
# interactive install would have died on it. Ask the installed CLI instead.
has_verb() {
  "${VPY}" -m cli.polyrob "$1" --help </dev/null >/dev/null 2>&1
}

wizard_verb() {
  if has_verb setup; then echo "setup"; else echo "init"; fi
}

# ---------------------------------------------------------------------------
# 6. Setup wizard
# ---------------------------------------------------------------------------
CURRENT_STEP="verify"
# The last moment a broken install can still say so instead of "installed".
if ! _ver="$("${VPY}" -I -m cli.polyrob --version 2>>"${INSTALL_LOG}")"; then
  die "the polyrob command does not start. Log: ${INSTALL_LOG}"
fi
stage verify ok "${_ver}"
send_install_count "${_ver}"

CURRENT_STEP="setup"
BOOTSTRAP_JSON="{\"install_method\":\"${MODE}\",\"source_dir\":\"$(json_esc "${SRC_DIR}")\",\"venv_dir\":\"$(json_esc "${VENV_DIR}")\",\"command\":\"$(json_esc "${SHIM}")\",\"stages\":[${STAGES}]}"
export POLYROB_BOOTSTRAP_STAGES="${BOOTSTRAP_JSON}"
export POLYROB_HOME="${POLYROB_HOME_DIR}"

WIZARD="$(wizard_verb)"
if [[ "${RUN_SETUP}" == "true" && -n "${TTY_DEV}" && "${NO_PROMPT}" != "true" ]]; then
  # The first run asks for a key and a model. The other questions all have
  # good defaults and stay one command away; a person who came to try the agent
  # should reach it first. An older CLI has no --quick, so ask it.
  WIZARD_ARGS=()
  if "${VPY}" -m cli.polyrob "${WIZARD}" --help </dev/null 2>/dev/null | grep -q -- '--quick'; then
    WIZARD_ARGS=(--quick)
  fi
  info "Starting setup. Press Enter to take a default; 'polyrob ${WIZARD}' later shows every option."
  "${VPY}" -m cli.polyrob "${WIZARD}" ${WIZARD_ARGS[@]+"${WIZARD_ARGS[@]}"} < "${TTY_DEV}" \
    || warn "setup exited non-zero — re-run it any time with: polyrob ${WIZARD}"
else
  # Say ONE true thing about why the wizard did not run — printing both
  # reasons told the reader we had not checked which applied.
  if [[ "${RUN_SETUP}" != "true" ]]; then
    info "Skipping setup (--no-setup). Run it later: polyrob ${WIZARD}"
  elif [[ "${NO_PROMPT}" == "true" ]]; then
    info "Non-interactive install. Run it later: polyrob ${WIZARD}"
  else
    info "No terminal available — skipping setup. Run it later: polyrob ${WIZARD}"
  fi
  # Still record the install and seed the identity docs, non-interactively.
  "${VPY}" -m cli.polyrob init --no-prompt </dev/null >/dev/null 2>&1 \
    || warn "'polyrob init --no-prompt' exited non-zero. Re-run it manually."
fi

# ---------------------------------------------------------------------------
# 7. Background service (only when a chat surface was configured)
# ---------------------------------------------------------------------------
CURRENT_STEP="service"
if [[ "${OFFER_SERVICE}" == "true" && -n "${TTY_DEV}" ]]; then
  if "${VPY}" -m cli.polyrob service needed </dev/null >/dev/null 2>&1; then
    if ask_yes_no "A chat surface is configured. Run the agent in the background?" yes; then
      "${VPY}" -m cli.polyrob service install < "${TTY_DEV}" \
        || warn "service install failed — try: polyrob service install"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
CURRENT_STEP="finish"
success "
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  POLYROB is installed (${_ver}).

  Command : ${SHIM}
  Source  : ${SRC_DIR}
  Config  : ${POLYROB_HOME_DIR}   (keys, settings, the install record)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ -n "${RELOAD_HINT}" ]]; then
  echo "  Reload your shell first:   source ${RELOAD_HINT}"
fi
echo "  polyrob                 talk to the agent"
printf '  polyrob %-15s re-run the wizard (keys, model, surfaces)\n' "${WIZARD}"
echo "  polyrob doctor          check the install and name what is missing"
has_verb service   && echo "  polyrob service status  the background service"
has_verb service   && echo "  /restart, /update       restart or update it from your chat"
echo "  polyrob update          update in place"
has_verb uninstall && echo "  polyrob uninstall       remove it again"
cat <<NEXT

  Memory, goals and identity live in ${POLYROB_HOME_DIR}/data,
  one store for every directory you run it from.
  A project keeps only small files in its own .polyrob/.
NEXT
if [[ -n "${MISSING_HOST_TOOLS}" ]]; then
  echo ""
  echo "  Optional host tools not found:${MISSING_HOST_TOOLS}"
  case "${MISSING_HOST_TOOLS}" in *rg*) echo "    rg     faster file search (brew install ripgrep / apt install ripgrep)" ;; esac
  case "${MISSING_HOST_TOOLS}" in *ffmpeg*) echo "    ffmpeg voice notes (brew install ffmpeg / apt install ffmpeg)" ;; esac
fi

}

main "$@"
