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
#   1. Finds Python >= 3.11 (and git, in managed mode)
#   2. Clones or reuses the source tree
#   3. Creates a venv and installs polyrob hash-checked against requirements.lock
#   4. Optionally installs the browser engine (Playwright + Chromium) and
#      first-party packs (packs/*: X, discovery, ...), hash-checked the same way
#   5. Installs a `polyrob` command on your PATH
#   6. Runs the setup wizard (works inside `curl | bash` — it reads /dev/tty)
#   7. Offers to run the agent as a background service when a chat token was set
#   8. Records what it did in <data home>/.polyrob-bootstrap.json
#
# Flags:
#   --dir DIR          Source tree to install (implies source mode)
#   --home DIR         POLYROB home (default: $POLYROB_HOME or ~/.polyrob)
#   --venv-dir DIR     Virtualenv location
#   --branch NAME      Branch to clone/update (default: main)
#   --commit SHA       Check out an exact commit after cloning
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
#   --reset            Recreate the virtualenv from scratch
#   --uninstall        Remove the command, the PATH entry, the venv and the
#                        source tree. Your data home is KEPT.
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
    --uninstall)  DO_UNINSTALL=true; shift ;;
    -h|--help)
      if [[ -n "${SCRIPT_SRC}" && -f "${SCRIPT_SRC}" ]]; then
        awk '/^#/{print substr($0,3); next} {exit}' "${SCRIPT_SRC}"
      else
        echo "polyrob installer. Flags: --dir --home --venv-dir --branch --commit"
        echo "  --extras LIST --browser --no-browser --packs LIST --no-setup --no-service"
        echo "  --no-prompt --reset --uninstall.  Details: https://polyrob.dev/install"
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
  printf '%s\n' "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.bash_profile" \
                "${HOME}/.profile" "${HOME}/.config/fish/config.fish"
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
  if [[ -e "${SHIM}" || -L "${SHIM}" ]]; then rm -f "${SHIM}"; info "Removed ${SHIM}"; fi
  remove_path_block
  if [[ -d "${VENV_DIR}" ]]; then rm -rf "${VENV_DIR}"; info "Removed ${VENV_DIR}"; fi
  if [[ "${MODE}" == "managed" && -d "${SRC_DIR}" ]]; then
    rm -rf "${SRC_DIR}"; info "Removed ${SRC_DIR}"
  fi
  success "Uninstalled. Data, keys and memory remain in ${POLYROB_HOME_DIR}."
  echo   "  Delete them yourself if you mean to: rm -rf ${POLYROB_HOME_DIR}"
  echo   "  (that directory holds the agent wallet seed — read it before you delete it)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Python >= 3.11
# ---------------------------------------------------------------------------
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
for _c in python3.13 python3.12 python3.11 python3 python; do
  _check_python "${_c}" && break
done
[[ -n "${PYTHON_BIN}" ]] || die "Python 3.11 or newer is required but was not found.
Install it from https://www.python.org/downloads/ or with your package manager:
  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3.12 python3.12-venv"

PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}.{v.micro}")')"
info "Python ${PYTHON_VERSION} at $(command -v "${PYTHON_BIN}")"

STAGES=""   # JSON fragments, joined at the end
stage() {   # stage NAME ok|skipped REASON
  local entry
  entry="$(printf '{"name":"%s","status":"%s","detail":"%s"}' "$1" "$2" "${3//\"/\'}")"
  if [[ -z "${STAGES}" ]]; then STAGES="${entry}"; else STAGES="${STAGES},${entry}"; fi
}
stage python ok "${PYTHON_VERSION}"

# ---------------------------------------------------------------------------
# 2. Source tree
# ---------------------------------------------------------------------------
if [[ "${MODE}" == "managed" ]]; then
  command -v git >/dev/null 2>&1 || die "git is required to install POLYROB.
  macOS:  xcode-select --install
  Ubuntu: sudo apt install git"
  if [[ -d "${SRC_DIR}/.git" ]]; then
    info "Updating ${SRC_DIR} (branch ${BRANCH}) ..."
    if [[ -n "$(git -C "${SRC_DIR}" status --porcelain 2>/dev/null)" ]]; then
      die "${SRC_DIR} has local changes. Commit, stash or remove them, or pass --dir to install a different tree."
    fi
    git -C "${SRC_DIR}" fetch --quiet origin "${BRANCH}"
    git -C "${SRC_DIR}" checkout --quiet "${BRANCH}"
    git -C "${SRC_DIR}" merge --ff-only --quiet "origin/${BRANCH}"
    stage repository ok "updated ${BRANCH}"
  else
    info "Cloning ${REPO_URL} into ${SRC_DIR} ..."
    mkdir -p "$(dirname "${SRC_DIR}")"
    git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${SRC_DIR}"
    stage repository ok "cloned ${BRANCH}"
  fi
  if [[ -n "${COMMIT}" ]]; then
    git -C "${SRC_DIR}" fetch --quiet --depth 1 origin "${COMMIT}" 2>/dev/null \
      || git -C "${SRC_DIR}" fetch --quiet --unshallow 2>/dev/null \
      || git -C "${SRC_DIR}" fetch --quiet origin
    git -C "${SRC_DIR}" checkout --quiet "${COMMIT}" \
      || die "commit ${COMMIT} not found in ${REPO_URL}"
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
if [[ -d "${VENV_DIR}" && "${RESET_VENV}" == "true" ]]; then
  warn "Removing ${VENV_DIR} (--reset)"
  rm -rf "${VENV_DIR}"
fi
if [[ ! -d "${VENV_DIR}" ]]; then
  info "Creating venv at ${VENV_DIR} ..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}" \
    || die "could not create a virtualenv. On Debian/Ubuntu install the venv module:
  sudo apt install python3-venv"
else
  info "Reusing venv at ${VENV_DIR}  (pass --reset to rebuild)"
fi
VPY="${VENV_DIR}/bin/python"
[[ -x "${VPY}" ]] || die "virtualenv interpreter missing at ${VPY}"

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
    && "${VPY}" -m pip install --quiet --require-hashes --prefer-binary -r "${deps}" \
    && "${VPY}" -m pip install --quiet --no-deps --no-build-isolation -e "${SRC_DIR}" \
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
  "${VPY}" "${SRC_DIR}/core/packs/retire.py"
}

info "Installing polyrob from ${SRC_DIR}${EXTRAS:+ with extras: ${EXTRAS}} (hash-checked) ..."
hashed_install "${EXTRAS}"
stage python-deps ok "${EXTRAS:-base}"

# ---------------------------------------------------------------------------
# 4. Browser engine (opt-in — it downloads ~150 MB of Chromium)
# ---------------------------------------------------------------------------
if [[ -z "${WANT_BROWSER}" ]]; then
  if ask_yes_no "Install the browser engine for web automation (~150 MB)?" no; then
    WANT_BROWSER="yes"
  else
    WANT_BROWSER="no"
  fi
fi
if [[ "${WANT_BROWSER}" == "yes" ]]; then
  info "Installing the browser extra and Chromium ..."
  if hashed_install browser && "${VPY}" -m playwright install chromium; then
    stage browser ok "playwright + chromium"
  else
    warn "browser install failed — run it later: ${VPY} -m pip install 'polyrob[browser]' && ${VPY} -m playwright install chromium"
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
    && "${VPY}" -m pip install --quiet --require-hashes --prefer-binary -r "${deps}" \
    || rc=$?
  rm -f "${deps}"
  return "${rc}"
}

AVAILABLE_PACKS=""
if [[ -f "${SRC_DIR}/core/lock_closure.py" ]]; then
  AVAILABLE_PACKS="$("${VPY}" "${SRC_DIR}/core/lock_closure.py" packs --root "${SRC_DIR}" --ids all --list 2>/dev/null \
    | awk '{print $1}' | paste -sd, - || true)"
fi
if [[ "${PACKS_SET}" != "true" && -n "${AVAILABLE_PACKS}" ]]; then
  PACKS="$(ask_line "Packs ship with polyrob; their SDKs are optional: ${AVAILABLE_PACKS}. Install which? (comma list, all, or Enter for none)")"
fi
[[ "${PACKS}" == "none" ]] && PACKS=""
if [[ -n "${PACKS}" ]]; then
  if ! "${VPY}" "${SRC_DIR}/core/lock_closure.py" packs --root "${SRC_DIR}" --ids "${PACKS}" >/dev/null; then
    die "unknown pack in '${PACKS}'. This tree has: ${AVAILABLE_PACKS:-no packs}"
  fi
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
mkdir -p "${BIN_DIR}"
# rm first: an older install may have left a SYMLINK here, and `cat >` would
# follow it and overwrite the venv's own console script.
rm -f "${SHIM}"
cat > "${SHIM}" <<SHIM_EOF
#!/usr/bin/env bash
# POLYROB launcher (written by install.sh). PYTHONPATH/PYTHONHOME are cleared so
# an inherited value cannot make this import a different checkout.
unset PYTHONPATH
unset PYTHONHOME
exec "${VPY}" -I -m cli.polyrob "\$@"
SHIM_EOF
chmod +x "${SHIM}"
success "Installed the polyrob command → ${SHIM}"
stage command ok "${SHIM}"

add_path_block() {
  case ":${PATH}:" in *":${BIN_DIR}:"*) stage path ok "already on PATH"; return 0 ;; esac
  local shell_name rc line
  shell_name="$(basename "${SHELL:-/bin/bash}")"
  case "${shell_name}" in
    zsh)  rc="${HOME}/.zshrc";                      line="export PATH=\"${BIN_DIR}:\$PATH\"" ;;
    fish) rc="${HOME}/.config/fish/config.fish";    line="set -gx PATH ${BIN_DIR} \$PATH" ;;
    *)    rc="${HOME}/.bashrc";                     line="export PATH=\"${BIN_DIR}:\$PATH\"" ;;
  esac
  mkdir -p "$(dirname "${rc}")"
  if [[ -f "${rc}" ]] && grep -q '^# >>> polyrob >>>' "${rc}"; then
    stage path ok "already written to ${rc}"; return 0
  fi
  {
    echo ""
    echo "# >>> polyrob >>>"
    echo "${line}"
    echo "# <<< polyrob <<<"
  } >> "${rc}"
  info "Added ${BIN_DIR} to your PATH in ${rc}"
  stage path ok "${rc}"
  RELOAD_HINT="${rc}"
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
  "${VPY}" -m cli.polyrob "$1" --help >/dev/null 2>&1
}

wizard_verb() {
  if has_verb setup; then echo "setup"; else echo "init"; fi
}

# ---------------------------------------------------------------------------
# 6. Setup wizard
# ---------------------------------------------------------------------------
BOOTSTRAP_JSON="{\"install_method\":\"${MODE}\",\"source_dir\":\"${SRC_DIR}\",\"venv_dir\":\"${VENV_DIR}\",\"command\":\"${SHIM}\",\"stages\":[${STAGES}]}"
export POLYROB_BOOTSTRAP_STAGES="${BOOTSTRAP_JSON}"
export POLYROB_HOME="${POLYROB_HOME_DIR}"

WIZARD="$(wizard_verb)"
if [[ "${RUN_SETUP}" == "true" && -n "${TTY_DEV}" && "${NO_PROMPT}" != "true" ]]; then
  info "Starting setup ..."
  "${VPY}" -m cli.polyrob "${WIZARD}" < "${TTY_DEV}" \
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
  "${VPY}" -m cli.polyrob init --no-prompt >/dev/null 2>&1 \
    || warn "'polyrob init --no-prompt' exited non-zero. Re-run it manually."
fi

# ---------------------------------------------------------------------------
# 7. Background service (only when a chat surface was configured)
# ---------------------------------------------------------------------------
if [[ "${OFFER_SERVICE}" == "true" && -n "${TTY_DEV}" ]]; then
  if "${VPY}" -m cli.polyrob service needed >/dev/null 2>&1; then
    if ask_yes_no "A chat surface is configured. Run the agent in the background?" yes; then
      "${VPY}" -m cli.polyrob service install < "${TTY_DEV}" \
        || warn "service install failed — try: polyrob service install"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
success "
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  POLYROB is installed.

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
echo "  polyrob update          update in place"
has_verb uninstall && echo "  polyrob uninstall       remove it again"
cat <<'NEXT'

  Memory, goals and identity live in a .polyrob/ directory beside the project
  you run in, so the agent keeps a separate memory per project. Run it from
  one directory to keep one memory.
NEXT
if [[ -n "${MISSING_HOST_TOOLS}" ]]; then
  echo ""
  echo "  Optional host tools not found:${MISSING_HOST_TOOLS}"
  echo "    rg     faster file search (brew install ripgrep / apt install ripgrep)"
  echo "    ffmpeg voice-note transcription (brew install ffmpeg / apt install ffmpeg)"
fi
