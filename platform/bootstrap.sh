#!/usr/bin/env bash
# ==========================================================================
# Nick's Stack — bootstrap installer for an EXISTING Ubuntu/Orgo computer
# ==========================================================================
# Installs the exact same stack that `build_template.py` bakes into the
# orgo.ai/v1 template, but onto a machine that is already running (no Orgo
# Scale plan, no template publishing, no golden image required).
#
#   sudo bash platform/bootstrap.sh
#
# build_template.py is the SOURCE OF TRUTH. Nothing here is redesigned:
# every file keeps Nick's name, every destination keeps Nick's path, the
# install order mirrors the template's apps[].install script, the two
# supervised services mirror apps[].services, and the two autostart entries
# mirror apps[].autostart (delays included).
#
#   template concept            -> what this script does
#   ---------------------------------------------------------------------
#   build.apt                   -> apt-get install (xz-utils first)
#   files[] staged at /opt/…    -> staged at /opt/nicks-stack/stage
#   apps[].install              -> steps 3-12 below, in the same order
#   apps[].services             -> /etc/supervisor/conf.d/nicks-stack.conf
#   apps[].autostart            -> /root/.config/autostart/*.desktop
#   hooks.on_first_boot         -> /var/lib/orgo stamp + runtime dirs
#
# Properties:
#   * root only, Ubuntu/Debian only, Ubuntu 24.04 tested
#   * `set -Eeuo pipefail` — stops immediately on any error
#   * idempotent — safe to re-run; installs only what is missing
#   * heavy logging — every action is logged to the console and to
#     /var/log/nicks-stack-bootstrap.log
#
# File-ownership policy (this is what "preserve user data" means here):
#   PRESERVED (installed only when absent, never overwritten):
#     /root/.hermes/.env            (merged: only MISSING default keys added)
#     /root/.hermes/.op.env         (never touched)
#     /root/.hermes/auth.json       (never touched)
#     /root/.hermes_agentphone_bridge/env  (merged, same rule as .env)
#     /root/.config/obsidian/obsidian.json
#     /root/Documents/HermesVault/** (existing notes always win)
#   MANAGED (replaced when content differs; the previous copy is backed up
#   under /opt/nicks-stack/backups/<timestamp>/ first):
#     config.yaml, SOUL.md, plugins/, skills/, scripts/, local-packages/,
#     /usr/local/bin/* launchers, /root/Desktop/*.desktop, wallpaper,
#     the supervisor conf and the autostart entries.
# ==========================================================================

set -Eeuo pipefail
IFS=$'\n\t'
umask 022

# --------------------------------------------------------------------------
# Constants — pinned exactly as build_template.py pins them
# --------------------------------------------------------------------------
readonly SCRIPT_NAME="Taylor AI Platform bootstrap"
# Platform + component versions live in files/platform.yaml (the declared
# spec). This mirror is only for the banner before that file is deployed.
readonly SCRIPT_VERSION="1.1.17"

readonly HERMES_INSTALL_URL="https://hermes-agent.nousresearch.com/install.sh"

readonly OP_VERSION="2.34.1"
readonly OP_ZIP_URL="https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_amd64_v${OP_VERSION}.zip"

readonly CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

readonly OLLAMA_INSTALL_URL="https://ollama.com/install.sh"

readonly OBSIDIAN_DEB_URL="https://github.com/obsidianmd/obsidian-releases/releases/download/v1.12.7/obsidian_1.12.7_amd64.deb"
readonly OBSIDIAN_DEB_SHA="3644e3ef19bcd23db4d17f7c73311b5245429391a2a48b361da93375f59712b0"

# build.apt from the template + the two extras the install script apt-gets
# itself (libsecret-1-0 for Obsidian) and needs to supervise anything.
# python3-venv/python3-pip are what `python3 -m venv` needs on Ubuntu; without
# them the Composio SDK venv cannot be created at all.
readonly APT_PACKAGES=(git xz-utils python3-yaml python3-venv python3-pip \
                       ripgrep ffmpeg libsecret-1-0)

# Global npm helpers — same pins, same fallback-to-unpinned behaviour.
readonly NPM_HELPERS=(
  "@modelcontextprotocol/server-filesystem@2026.1.14"
  "agent-cards@0.5.59"
  "@xdevplatform/xurl"
)

# npx pre-warm targets (cold stdio MCP installs otherwise burn their retries)
readonly NPX_PREWARM=("github:nickvasilescu/orgo-mcp" "agentphone-mcp")

# Hermes paths — Nick's locations, unchanged.
readonly HERMES_HOME="/root/.hermes"
readonly VENV_PY="/usr/local/lib/hermes-agent/venv/bin/python"
readonly BRIDGE_DIR="/root/.hermes_agentphone_bridge"
readonly VAULT_DIR="/root/Documents/HermesVault"
readonly OBSIDIAN_CFG_DIR="/root/.config/obsidian"
readonly DESKTOP_DIR="/root/Desktop"
readonly AUTOSTART_DIR="/root/.config/autostart"
readonly WALLPAPER_DIR="/usr/share/backgrounds"
readonly ORGO_LIB="/var/lib/orgo"
readonly ORGO_LOG="/var/log/orgo"
readonly PREFIX_BIN="/usr/local/bin"

readonly STACK_ROOT="/opt/nicks-stack"
readonly STAGE="${STACK_ROOT}/stage"

readonly LOG_FILE="${NICKS_STACK_LOG:-/var/log/nicks-stack-bootstrap.log}"
readonly LOCK_NAME="nicks-stack-bootstrap.lock"
# Preferred lock path, then the fallbacks tried in order. Orgo images ship
# /var/lock as a symlink to a /run/lock that does not exist yet, so nothing
# here may assume any of these directories is present.
readonly LOCK_CANDIDATES=(
  "${NICKS_STACK_LOCK:-/var/lock/${LOCK_NAME}}"
  "/run/lock/${LOCK_NAME}"
  "/tmp/${LOCK_NAME}"
)

readonly TOTAL_STEPS=16

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_BLUE=$'\033[1;36m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
else
  C_RESET=''; C_DIM=''; C_BOLD=''; C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

_ts() { date -Iseconds; }
log()   { printf '%s[%s] %s%s\n'    "$C_DIM"    "$(_ts)" "$*" "$C_RESET"; }
info()  { printf '%s[%s] ›  %s%s\n' "$C_DIM"    "$(_ts)" "$*" "$C_RESET"; }
ok()    { printf '%s[%s] ✓  %s%s\n' "$C_GREEN"  "$(_ts)" "$*" "$C_RESET"; }
skip()  { printf '%s[%s] ·  %s%s\n' "$C_DIM"    "$(_ts)" "$*" "$C_RESET"; }
warn()  { printf '%s[%s] !  %s%s\n' "$C_YELLOW" "$(_ts)" "$*" "$C_RESET"; WARNINGS+=("$*"); }
err()   { printf '%s[%s] ✗  %s%s\n' "$C_RED"    "$(_ts)" "$*" "$C_RESET"; }

WARNINGS=()
HEALTH_FAILURES=()
CHANGES=0

step() {
  local n="$1"; shift
  printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
  printf '%s[%s] STEP %s/%s — %s%s\n' "$C_BLUE$C_BOLD" "$(_ts)" "$n" "$TOTAL_STEPS" "$*" "$C_RESET"
  printf '%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
}

die() { err "$*"; err "bootstrap ABORTED — full log: $LOG_FILE"; exit 1; }

on_error() {
  local exit_code=$? line="$1" cmd="$2"
  err "unexpected failure (exit $exit_code) at line $line: $cmd"
  err "bootstrap ABORTED — nothing further was changed. Full log: $LOG_FILE"
  exit "$exit_code"
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<USAGE
${SCRIPT_NAME} v${SCRIPT_VERSION}

Installs Nick's Stack (Hermes agent + 1Password + AgentPhone bridge +
Obsidian + supervised services) onto an existing Ubuntu/Debian machine.

  sudo bash platform/bootstrap.sh [--help]

Composio (required whenever platform.yaml declares a composio: block):
  --skip-composio         do not install the Composio SDK on this machine.
                          Without it, a Composio install failure ABORTS the
                          run rather than leaving Jack silently without
                          Gmail/Calendar/Drive/Contacts/Notion.

Local AI (Ollama) — all optional, nothing is downloaded unless you ask:
  --with-ollama           supervise an already-installed Ollama (no network)
  --install-ollama        also install the Ollama binary (needs the network)
  --ollama-models "A B"   pull these models after install (needs the network)

Environment overrides:
  NICKS_STACK_FILES_DIR   source tree (default: <repo>/files)
  NICKS_STACK_LOG         log file    (default: /var/log/nicks-stack-bootstrap.log)
  NICKS_STACK_LOCK        lock file   (default: /var/lock/nicks-stack-bootstrap.lock,
                          falling back to /run/lock then /tmp)
USAGE
}

# curl with retries; every download in this script goes through it.
download() {
  local url="$1" dest="$2"
  info "downloading $url"
  curl -fsSL --connect-timeout 20 --max-time 900 \
       --retry 4 --retry-delay 2 --retry-connrefused \
       -o "$dest" "$url"
}

BACKUP_DIR=""
backup_of() {
  local target="$1"
  [[ -e "$target" ]] || return 0
  if [[ -z "$BACKUP_DIR" ]]; then
    BACKUP_DIR="${STACK_ROOT}/backups/$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    info "backups for this run: $BACKUP_DIR"
  fi
  local dest="${BACKUP_DIR}/${target#/}"
  mkdir -p "$(dirname "$dest")"
  cp -a "$target" "$dest"
  info "backed up $target -> $dest"
}

# MANAGED file: install when absent, replace (after backup) when content or
# mode drifts, skip silently when already correct.
install_managed() {
  local src="$1" dest="$2" mode="$3"
  [[ -f "$src" ]] || die "source file missing: $src"
  if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then
    local cur; cur="$(stat -c '%a' "$dest")"
    if [[ "$cur" == "${mode#0}" || "$cur" == "$mode" ]]; then
      skip "unchanged: $dest"
      return 0
    fi
    chmod "$mode" "$dest"
    ok "mode fixed ($mode): $dest"
    CHANGES=$((CHANGES + 1))
    return 0
  fi
  if [[ -e "$dest" ]]; then backup_of "$dest"; fi
  install -D -m "$mode" "$src" "$dest"
  ok "installed: $dest ($mode)"
  CHANGES=$((CHANGES + 1))
}

# PRESERVED file: never clobber an existing one.
install_preserved() {
  local src="$1" dest="$2" mode="$3"
  [[ -f "$src" ]] || die "source file missing: $src"
  if [[ -e "$dest" ]]; then
    skip "preserved existing user file: $dest"
    return 0
  fi
  install -D -m "$mode" "$src" "$dest"
  ok "installed: $dest ($mode)"
  CHANGES=$((CHANGES + 1))
}

# Mode rule copied verbatim from build_template.py payload_b64():
#   0755 for *.sh and for anything living under a scripts/ directory,
#   0644 for everything else.
tree_mode() {
  local rel="$1" root="$2"
  if [[ "$rel" == *.sh || "$rel" == */scripts/* || "$rel" == scripts/* || "$root" == "scripts" ]]; then
    printf '0755'
  else
    printf '0644'
  fi
}

# MANAGED tree copy (mirrors `cp -rf {STAGE}/…/. dest/`): adds and updates,
# never deletes anything the user put there.
sync_tree_managed() {
  local src="$1" dest="$2" root_label="$3"
  [[ -d "$src" ]] || die "source tree missing: $src"
  mkdir -p "$dest"
  local count=0 rel mode
  while IFS= read -r -d '' file; do
    rel="${file#"$src"/}"
    case "$rel" in
      *__pycache__*|*.pyc|*.DS_Store) continue ;;
    esac
    mode="$(tree_mode "$rel" "$root_label")"
    install_managed "$file" "$dest/$rel" "$mode"
    count=$((count + 1))
  done < <(find "$src" -type f -print0 | sort -z)
  ok "$root_label: $count file(s) reconciled into $dest"
}

# PRESERVED tree copy (the vault): only files that do not exist yet.
sync_tree_preserved() {
  local src="$1" dest="$2" label="$3"
  [[ -d "$src" ]] || die "source tree missing: $src"
  mkdir -p "$dest"
  local added=0 kept=0 rel
  while IFS= read -r -d '' file; do
    rel="${file#"$src"/}"
    case "$rel" in
      *__pycache__*|*.pyc|*.DS_Store) continue ;;
    esac
    if [[ -e "$dest/$rel" ]]; then
      kept=$((kept + 1))
      continue
    fi
    install -D -m 0644 "$file" "$dest/$rel"
    ok "installed: $dest/$rel (0644)"
    added=$((added + 1))
    CHANGES=$((CHANGES + 1))
  done < <(find "$src" -type f -print0 | sort -z)
  ok "$label: $added file(s) added, $kept existing file(s) preserved"
}

# env files: create from the baked defaults when absent; otherwise append ONLY
# the default keys that are not already defined. Never rewrites, reorders or
# removes an existing line — that file holds the user's keys.
merge_env_defaults() {
  local src="$1" dest="$2" mode="$3"
  if [[ ! -e "$dest" ]]; then
    install -D -m "$mode" "$src" "$dest"
    ok "installed: $dest ($mode)"
    CHANGES=$((CHANGES + 1))
    return 0
  fi
  info "existing env file found, merging missing defaults only: $dest"
  local added=() key line
  while IFS= read -r line; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$dest"; then
      continue
    fi
    added+=("$line")
  done < "$src"
  if ((${#added[@]} == 0)); then
    skip "no missing defaults: $dest"
  else
    backup_of "$dest"
    {
      printf '\n# --- added by %s v%s on %s ---\n' "$SCRIPT_NAME" "$SCRIPT_VERSION" "$(_ts)"
      printf '%s\n' "${added[@]}"
    } >> "$dest"
    for key in "${added[@]}"; do ok "added default to $dest: ${key%%=*}"; done
    CHANGES=$((CHANGES + 1))
  fi
  chmod "$mode" "$dest"
}

# "pkg@1.2.3" -> "pkg";  "@scope/pkg@1.2.3" -> "@scope/pkg";  "@scope/pkg" -> itself
npm_pkg_name() {
  local spec="$1" body="${1#@}"
  if [[ "$body" == *@* ]]; then
    printf '%s' "${spec%@*}"
  else
    printf '%s' "$spec"
  fi
}

apt_install_missing() {
  local pkgs=("$@") missing=()
  local p
  for p in "${pkgs[@]}"; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed"; then
      skip "apt package already installed: $p"
    else
      missing+=("$p")
    fi
  done
  if ((${#missing[@]} == 0)); then
    return 0
  fi
  apt_update_once
  info "apt-get install: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
  ok "apt installed: ${missing[*]}"
  CHANGES=$((CHANGES + 1))
}

APT_UPDATED=0
apt_update_once() {
  if ((APT_UPDATED)); then
    return 0
  fi
  info "apt-get update"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  APT_UPDATED=1
}

health_check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    ok "health: $label"
  else
    err "health: $label — FAILED"
    HEALTH_FAILURES+=("$label")
  fi
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
SKIP_COMPOSIO=0      # opt out of the Composio SDK on a machine that must not have it
WITH_OLLAMA=0        # configure the supervised Ollama service
INSTALL_OLLAMA=0     # also install the Ollama binary (needs the network)
OLLAMA_PULL=""       # optional, explicit model pulls (needs the network)

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --skip-composio)  SKIP_COMPOSIO=1 ;;
    --with-ollama)    WITH_OLLAMA=1 ;;
    --install-ollama) INSTALL_OLLAMA=1; WITH_OLLAMA=1 ;;
    --ollama-models)
      shift
      [[ $# -gt 0 ]] || { usage >&2; die "--ollama-models needs a value"; }
      OLLAMA_PULL="$1"; WITH_OLLAMA=1
      ;;
    *) usage; die "unknown argument: $1" ;;
  esac
  shift
done

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_PATH")")"
FILES_DIR="${NICKS_STACK_FILES_DIR:-$REPO_ROOT/files}"

printf '\n%s%s v%s%s\n' "$C_BOLD" "$SCRIPT_NAME" "$SCRIPT_VERSION" "$C_RESET"
log "started $(_ts)"
log "script      : $SCRIPT_PATH"
log "repo root   : $REPO_ROOT"
log "source files: $FILES_DIR"
log "log file    : $LOG_FILE"

[[ "$(id -u)" -eq 0 ]] || die "this installer must run as root (try: sudo bash $0)"
[[ -d "$FILES_DIR" ]] || die "source tree not found: $FILES_DIR (run from a nicks-stack checkout, or set NICKS_STACK_FILES_DIR)"

# Single-instance lock — two concurrent bootstraps would race on apt and on
# the supervisor conf. Portable across images: the lock directory is created
# when missing (resolving a dangling symlink such as /var/lock -> /run/lock to
# its target first), and each candidate falls through to the next, ending at
# /tmp. Opened with >> so re-runs reuse the same file instead of truncating it.
LOCK_PATH=""
lock_tried=()
for lock_candidate in "${LOCK_CANDIDATES[@]}"; do
  lock_seen=0
  for lock_prev in ${lock_tried[@]+"${lock_tried[@]}"}; do
    if [[ "$lock_prev" == "$lock_candidate" ]]; then lock_seen=1; fi
  done
  if ((lock_seen)); then
    continue
  fi
  lock_tried+=("$lock_candidate")

  lock_dir="$(dirname "$lock_candidate")"
  # readlink -f resolves a dangling symlink to the path it points at, so
  # mkdir -p creates the real target rather than failing on EEXIST.
  lock_real_dir="$(readlink -f "$lock_dir" 2>/dev/null || true)"
  [[ -n "$lock_real_dir" ]] || lock_real_dir="$lock_dir"

  if [[ ! -d "$lock_real_dir" ]]; then
    if mkdir -p "$lock_real_dir" 2>/dev/null; then
      info "created lock directory: $lock_real_dir"
    else
      info "lock directory unavailable, trying the next candidate: $lock_real_dir"
      continue
    fi
  fi

  if touch "$lock_candidate" 2>/dev/null && exec 9>>"$lock_candidate"; then
    LOCK_PATH="$lock_candidate"
    break
  fi
  info "lock file unavailable, trying the next candidate: $lock_candidate"
done

[[ -n "$LOCK_PATH" ]] || die "could not create a lock file in any of: ${LOCK_CANDIDATES[*]}"
log "lock file   : $LOCK_PATH"
flock -n 9 || die "another bootstrap is already running (lock: $LOCK_PATH)"

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
export HERMES_HOME
export PATH="/usr/local/bin:${HERMES_HOME}/bin:${HERMES_HOME}/node/bin:/root/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

# ==========================================================================
# 1. Verify Ubuntu/Debian
# ==========================================================================
step 1 "Verify the host is Ubuntu/Debian"

[[ -r /etc/os-release ]] || die "/etc/os-release not readable — cannot identify this OS"
# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_LIKE="${ID_LIKE:-}"
OS_VER="${VERSION_ID:-unknown}"
log "detected: ${PRETTY_NAME:-$OS_ID $OS_VER} (id=$OS_ID id_like=${OS_LIKE:-none})"

case " $OS_ID $OS_LIKE " in
  *" ubuntu "*|*" debian "*) ok "supported distribution: $OS_ID $OS_VER" ;;
  *) die "unsupported distribution '$OS_ID' — this installer targets Ubuntu/Debian (Ubuntu 24.04 reference)" ;;
esac

if [[ "$OS_ID" == "ubuntu" && "$OS_VER" != "24.04" ]]; then
  warn "reference platform is Ubuntu 24.04; this host reports $OS_VER — continuing"
fi

# Init system — this stack supervises its services with Supervisor. Knowing
# which init is actually running matters for Ollama: the upstream installer
# writes a systemd unit that would never start here (and, on a box where
# systemd IS running, would fight our supervised service for port 11434).
INIT_SYSTEM="unknown"
INIT_COMM="$(cat /proc/1/comm 2>/dev/null || echo unknown)"
case "$INIT_COMM" in
  systemd) INIT_SYSTEM="systemd" ;;
  supervisord) INIT_SYSTEM="supervisord" ;;
  *) INIT_SYSTEM="$INIT_COMM" ;;
esac
if have supervisorctl || have supervisord; then
  HAS_SUPERVISOR=1
else
  HAS_SUPERVISOR=0
fi
log "init system: PID 1 is '$INIT_COMM'; supervisor present: $((HAS_SUPERVISOR))"
if [[ "$INIT_SYSTEM" == "systemd" ]]; then
  log "systemd is PID 1, but this stack still manages its services through Supervisor"
else
  ok "Supervisor-managed environment (no systemd) — services come from $PWD/platform/bootstrap.sh"
fi

ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
log "architecture: $ARCH"
[[ "$ARCH" == "amd64" || "$ARCH" == "x86_64" ]] || \
  die "unsupported architecture '$ARCH' — the pinned op/cloudflared/Obsidian binaries are linux-amd64"

have apt-get || die "apt-get not found — this installer requires a Debian-family package manager"

# ==========================================================================
# 2. Install required apt packages  (xz-utils first — Hermes needs it)
# ==========================================================================
step 2 "Install required apt packages"

# curl is how everything else is fetched, so it comes before the rest.
if have curl; then
  skip "curl already present: $(curl --version | head -1)"
else
  apt_install_missing curl ca-certificates
fi

# ==========================================================================
# 4 (ordered before Hermes). Install xz-utils
# ==========================================================================
info "xz-utils must land BEFORE the Hermes installer (it unpacks Node with it)"
apt_install_missing xz-utils
ok "xz-utils present: $(xz --version 2>/dev/null | head -1)"

info "installing the remaining template build.apt set"
apt_install_missing "${APT_PACKAGES[@]}"

# Supervisor is how apps[].services are realised on a plain box.
if have supervisorctl || have supervisord; then
  skip "supervisor already present"
else
  warn "supervisor not found — installing it so the stack's services can be supervised"
  apt_install_missing supervisor
fi

ok "apt phase complete"

# ==========================================================================
# 3. Install Hermes if missing
# ==========================================================================
step 3 "Install the Hermes agent (if missing)"

if have hermes; then
  skip "hermes already installed: $(command -v hermes)"
  hermes --version 2>&1 | head -1 || warn "hermes --version did not report cleanly"
else
  info "running the official Hermes installer (non-interactive, no wizard, no Playwright)"
  # Same invocation as apps[].install step 1 in build_template.py.
  curl -fsSL --connect-timeout 20 --retry 4 --retry-delay 2 "$HERMES_INSTALL_URL" \
    | bash -s -- --non-interactive --skip-setup --skip-browser
  hash -r || true
  have hermes || die "Hermes install finished but 'hermes' is not on PATH ($PATH)"
  ok "hermes installed: $(command -v hermes)"
  CHANGES=$((CHANGES + 1))
fi

[[ -x "$VENV_PY" ]] || warn "Hermes venv python not found at $VENV_PY — venv-scoped steps will be skipped"

# ==========================================================================
# 4. xz-utils  (already installed above, before Hermes — verified here)
# ==========================================================================
step 4 "Verify xz-utils landed before Hermes"
have xz || die "xz-utils missing after install"
ok "xz-utils verified: $(command -v xz)"

# ==========================================================================
# 5. Install Node helpers
# ==========================================================================
step 5 "Install global Node helpers (filesystem MCP, agent-cards, xurl)"

if ! have npm; then
  warn "npm not on PATH after the Hermes install — falling back to apt nodejs/npm"
  apt_install_missing nodejs npm
fi
have npm || die "npm is required for the MCP helpers but could not be installed"
log "node: $(node --version 2>/dev/null || echo 'n/a')   npm: $(npm --version 2>/dev/null || echo 'n/a')"

NPM_TO_INSTALL=()
for spec in "${NPM_HELPERS[@]}"; do
  pkg="$(npm_pkg_name "$spec")"
  if npm ls -g --depth=0 "$pkg" >/dev/null 2>&1; then
    skip "npm helper already installed: $pkg"
  else
    NPM_TO_INSTALL+=("$spec")
  fi
done

if ((${#NPM_TO_INSTALL[@]} > 0)); then
  info "npm install -g ${NPM_TO_INSTALL[*]}"
  # Pinned first, unpinned fallback — exactly as the template's install does.
  if ! npm install -g "${NPM_TO_INSTALL[@]}"; then
    warn "pinned npm install failed — retrying unpinned"
    UNPINNED=()
    for spec in "${NPM_TO_INSTALL[@]}"; do
      UNPINNED+=("$(npm_pkg_name "$spec")")
    done
    npm install -g "${UNPINNED[@]}"
  fi
  ok "npm helpers installed"
  CHANGES=$((CHANGES + 1))
fi

info "pre-warming npx caches so cold stdio MCP servers do not burn their retries"
for target in "${NPX_PREWARM[@]}"; do
  if timeout 90 npx -y "$target" </dev/null >/dev/null 2>&1; then
    ok "npx pre-warmed: $target"
  else
    log "npx pre-warm finished non-zero (expected for stdio servers): $target"
  fi
done

# qrcode inside the Hermes venv — the Telegram pairing QR renders with it.
if [[ -x "$VENV_PY" ]]; then
  if "$VENV_PY" -c 'import qrcode' >/dev/null 2>&1; then
    skip "qrcode already available in the Hermes venv"
  else
    info "installing qrcode[pil] into the Hermes venv (uv-managed, may ship no pip)"
    if uv pip install --python "$VENV_PY" "qrcode[pil]" >/dev/null 2>&1 \
       || "${HERMES_HOME}/bin/uv" pip install --python "$VENV_PY" "qrcode[pil]" >/dev/null 2>&1 \
       || "$VENV_PY" -m pip install "qrcode[pil]" >/dev/null 2>&1; then
      ok "qrcode installed into the Hermes venv"
      CHANGES=$((CHANGES + 1))
    else
      warn "could not install qrcode[pil] into $VENV_PY — the Telegram QR step may fail"
    fi
  fi
fi

# ==========================================================================
# 6. Install cloudflared
# ==========================================================================
step 6 "Install cloudflared (the AgentPhone bridge's quick tunnel)"

if have cloudflared; then
  skip "cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
else
  download "$CLOUDFLARED_URL" "${PREFIX_BIN}/cloudflared.tmp"
  chmod 0755 "${PREFIX_BIN}/cloudflared.tmp"
  mv -f "${PREFIX_BIN}/cloudflared.tmp" "${PREFIX_BIN}/cloudflared"
  hash -r || true
  ok "cloudflared installed: $(cloudflared --version 2>&1 | head -1)"
  CHANGES=$((CHANGES + 1))
fi

# ==========================================================================
# 7. Install the 1Password CLI
# ==========================================================================
step 7 "Install the 1Password CLI (the stack's secret plane)"

if have op; then
  skip "op already installed: $(op --version 2>&1 | head -1)"
else
  # Direct binary from the official CDN, pinned to Dewey's v2.34.1 — the
  # apt repo route fails on this base image, and the zip needs no unzip.
  OP_TMP="$(mktemp -d)"
  download "$OP_ZIP_URL" "$OP_TMP/op.zip"
  info "extracting op from the release zip"
  python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('op', sys.argv[2])" \
          "$OP_TMP/op.zip" "$OP_TMP/opx"
  install -m 0755 "$OP_TMP/opx/op" /usr/bin/op
  rm -rf "$OP_TMP"
  hash -r || true
  ok "op installed: $(op --version 2>&1 | head -1)"
  CHANGES=$((CHANGES + 1))
fi

# ==========================================================================
# 8. Install Obsidian
# ==========================================================================
step 8 "Install Obsidian 1.12.7 (pinned)"

if [[ -x /opt/Obsidian/obsidian ]]; then
  skip "Obsidian already installed at /opt/Obsidian/obsidian"
else
  OBS_TMP="$(mktemp -d)"
  download "$OBSIDIAN_DEB_URL" "$OBS_TMP/obsidian.deb"
  info "verifying the .deb checksum"
  echo "${OBSIDIAN_DEB_SHA}  ${OBS_TMP}/obsidian.deb" | sha256sum -c -
  ok "checksum verified"
  info "extracting the .deb into / (no dpkg install — GUI deps already present)"
  dpkg-deb -x "$OBS_TMP/obsidian.deb" /
  rm -rf "$OBS_TMP"
  ok "Obsidian extracted to /opt/Obsidian"
  CHANGES=$((CHANGES + 1))
fi

if [[ -L /usr/bin/obsidian && "$(readlink -f /usr/bin/obsidian)" == "/opt/Obsidian/obsidian" ]]; then
  skip "/usr/bin/obsidian symlink already correct"
else
  ln -sf /opt/Obsidian/obsidian /usr/bin/obsidian
  ok "linked /usr/bin/obsidian -> /opt/Obsidian/obsidian"
  CHANGES=$((CHANGES + 1))
fi

# ==========================================================================
# 8b. Ollama — optional local AI, never installed or downloaded implicitly
# ==========================================================================
if ((WITH_OLLAMA)) || have ollama; then
  step 8 "Local AI (Ollama)"

  if have ollama; then
    skip "Ollama already installed: $(command -v ollama)"
    WITH_OLLAMA=1
  elif ((INSTALL_OLLAMA)); then
    info "installing Ollama from $OLLAMA_INSTALL_URL (explicitly requested)"
    if curl -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 "$OLLAMA_INSTALL_URL" | sh; then
      hash -r || true
      if have ollama; then
        ok "Ollama installed: $(ollama --version 2>&1 | head -1)"
        CHANGES=$((CHANGES + 1))
      else
        warn "Ollama installer finished but the binary is not on PATH"
      fi
    else
      warn "Ollama install failed (no network?) — the service will stay dormant until it is installed"
    fi
    # The upstream installer drops a systemd unit. On a Supervisor box it never
    # runs; where systemd IS present it would race our supervised service for
    # port 11434, so make sure it is not enabled.
    if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
      systemctl disable --now ollama.service >/dev/null 2>&1 || true
      warn "disabled the systemd ollama.service — this stack supervises Ollama itself"
    fi
  else
    info "Ollama is not installed. Pass --install-ollama to install it, or install it"
    info "yourself; the supervised service stays dormant until the binary exists."
  fi

  if [[ -n "$OLLAMA_PULL" ]]; then
    if have ollama; then
      # Explicit pulls only. Never implied by --with-ollama, never a default.
      for model in $OLLAMA_PULL; do
        if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$model"; then
          skip "model already present: $model"
        else
          info "pulling model (network required): $model"
          if ollama pull "$model"; then
            ok "pulled: $model"
            CHANGES=$((CHANGES + 1))
          else
            warn "could not pull '$model' — pull it later with: ollama pull $model"
          fi
        fi
      done
    else
      warn "--ollama-models given but Ollama is not installed — skipping pulls"
    fi
  fi
else
  log "Ollama not requested and not installed — skipping (use --with-ollama to enable)"
fi

# ==========================================================================
# 9. Create the Hermes directories
# ==========================================================================
step 9 "Create the Hermes / stack directories"

DIRS=(
  "$STACK_ROOT" "$STAGE"
  "$HERMES_HOME" "$HERMES_HOME/plugins" "$HERMES_HOME/skills" "$HERMES_HOME/scripts"
  "$HERMES_HOME/local-packages" "$HERMES_HOME/memories" "$HERMES_HOME/state"
  "$BRIDGE_DIR" "$VAULT_DIR" "$OBSIDIAN_CFG_DIR"
  "$DESKTOP_DIR" "$AUTOSTART_DIR" "$WALLPAPER_DIR"
  "$ORGO_LOG" "$ORGO_LIB"
)
for d in "${DIRS[@]}"; do
  if [[ -d "$d" ]]; then
    skip "directory exists: $d"
  else
    mkdir -p "$d"
    ok "created directory: $d"
    CHANGES=$((CHANGES + 1))
  fi
done
chmod 0700 "$HERMES_HOME" 2>/dev/null || true

# hooks.on_first_boot parity — the stamp other tooling looks for.
if [[ -f "$ORGO_LIB/nicks-stack.stamp" ]]; then
  skip "first-boot stamp already present"
else
  echo "nicks-stack first boot $(date -Iseconds)" > "$ORGO_LIB/nicks-stack.stamp"
  ok "wrote $ORGO_LIB/nicks-stack.stamp"
fi

# ==========================================================================
# 10. Copy every file from files/ into its Hermes location
#     (staged under /opt/nicks-stack/stage first, exactly like the template)
# ==========================================================================
step 10 "Stage files/ and place them in Nick's Hermes locations"

info "staging the source tree under $STAGE (template parity)"
mkdir -p "$STAGE/hermes" "$STAGE/agentphone-bridge" "$STAGE/vault"

# --- stage: config / identity / env ---------------------------------------
install -D -m 0600 "$FILES_DIR/config.yaml"  "$STAGE/hermes/config.yaml"
install -D -m 0644 "$FILES_DIR/SOUL.md"      "$STAGE/hermes/SOUL.md"
install -D -m 0600 "$FILES_DIR/hermes.env"   "$STAGE/hermes/env"
install -D -m 0644 "$FILES_DIR/routing.yaml" "$STAGE/hermes/routing.yaml"
install -D -m 0644 "$FILES_DIR/platform.yaml" "$STAGE/hermes/platform.yaml"
install -D -m 0644 "$FILES_DIR/obsidian-registry.json" "$STAGE/obsidian.json"
ok "staged config.yaml, SOUL.md, env, routing.yaml, platform.yaml, obsidian.json"

# --- stage: the four Dewey trees ------------------------------------------
for pair in "plugins:hermes/plugins" "skills:hermes/skills" "scripts:hermes/scripts" "local-packages:hermes/local-packages"; do
  src_root="${pair%%:*}"; stage_rel="${pair##*:}"
  info "staging $src_root -> $STAGE/$stage_rel"
  mkdir -p "$STAGE/$stage_rel"
  while IFS= read -r -d '' f; do
    rel="${f#"$FILES_DIR/$src_root"/}"
    case "$rel" in *__pycache__*|*.pyc|*.DS_Store) continue ;; esac
    install -D -m "$(tree_mode "$rel" "$src_root")" "$f" "$STAGE/$stage_rel/$rel"
  done < <(find "$FILES_DIR/$src_root" -type f -print0 | sort -z)
done
ok "staged plugins/, skills/, scripts/, local-packages/"

# --- stage: bridge + vault -------------------------------------------------
install -D -m 0700 "$FILES_DIR/agentphone-bridge/agentphone_bridge.py"    "$STAGE/agentphone-bridge/agentphone_bridge.py"
install -D -m 0600 "$FILES_DIR/agentphone-bridge/env"                     "$STAGE/agentphone-bridge/env"
install -D -m 0644 "$FILES_DIR/agentphone-bridge/test_event_ordering.py"  "$STAGE/agentphone-bridge/test_event_ordering.py"
while IFS= read -r -d '' f; do
  rel="${f#"$FILES_DIR/vault"/}"
  install -D -m 0644 "$f" "$STAGE/vault/$rel"
done < <(find "$FILES_DIR/vault" -type f -print0 | sort -z)
ok "staged agentphone-bridge/ and vault/"

# --- place: Hermes config / identity / env --------------------------------
info "placing staged files (our files win over anything the installer wrote)"
install_managed  "$STAGE/hermes/config.yaml" "$HERMES_HOME/config.yaml" 0600
install_managed  "$STAGE/hermes/SOUL.md"     "$HERMES_HOME/SOUL.md"     0644
merge_env_defaults "$STAGE/hermes/env"       "$HERMES_HOME/.env"        0600
# Provider routing map — declarative, no secrets, read by nicks-stack-route.
install_managed  "$STAGE/hermes/routing.yaml" "$HERMES_HOME/routing.yaml" 0644
# Declared platform spec: version, services, identity, company builds.
install_managed  "$STAGE/hermes/platform.yaml" "$HERMES_HOME/platform.yaml" 0644

# --- place: the four Dewey trees ------------------------------------------
sync_tree_managed "$STAGE/hermes/plugins"        "$HERMES_HOME/plugins"        "plugins"
sync_tree_managed "$STAGE/hermes/skills"         "$HERMES_HOME/skills"         "skills"
sync_tree_managed "$STAGE/hermes/scripts"        "$HERMES_HOME/scripts"        "scripts"
sync_tree_managed "$STAGE/hermes/local-packages" "$HERMES_HOME/local-packages" "local-packages"

# --- prune the always-on skill index  (v1.1.15) ---------------------------
# Runs AFTER the skills tree is synced, so a re-seed by `hermes update` is
# pruned again on the next deploy rather than silently re-inflating the index.
# Declared in platform.yaml skills.prune; never touches an unlisted skill.
# Non-fatal: a bigger prompt is a cost problem, not a broken deployment.
SKILLS_PRUNE="$HERMES_HOME/scripts/platform/skills_prune.py"
if [[ -f "$SKILLS_PRUNE" ]]; then
  if PRUNE_OUT="$(python3 "$SKILLS_PRUNE" apply 2>&1)"; then
    while IFS= read -r line; do [[ -n "$line" ]] && ok "$line"; done <<< "$PRUNE_OUT"
  else
    warn "skill prune reported a problem: ${PRUNE_OUT:-unknown}"
  fi
fi

# --- place: vault + Obsidian registry (user data — never clobbered) -------
sync_tree_preserved "$STAGE/vault" "$VAULT_DIR" "vault"
install_preserved "$STAGE/obsidian.json" "$OBSIDIAN_CFG_DIR/obsidian.json" 0644

# --- place: wallpaper ------------------------------------------------------
install_managed "$FILES_DIR/wallpaper.jpg" "$WALLPAPER_DIR/wallpaper.jpg" 0644

chmod 0600 "$HERMES_HOME/config.yaml" "$HERMES_HOME/.env"
ok "file placement complete"

# --- Latitude telemetry plugin + core reasoning_config patch --------------
# apps[].install step 10. Non-fatal here: on an already-running box a
# different Hermes build can move the patch anchor, and that must not take
# the rest of the stack down with it.
LAT_PATCH="$HERMES_HOME/scripts/latitude/install_local_telemetry_patch.sh"
if [[ -x "$VENV_PY" && -f "$LAT_PATCH" ]]; then
  info "installing the Latitude telemetry package + core hook patch"
  if bash "$LAT_PATCH"; then
    ok "Latitude telemetry installed"
  else
    warn "Latitude telemetry patch failed (Hermes core anchor may have moved) — stack continues without tracing"
  fi
else
  warn "skipping Latitude telemetry (missing $VENV_PY or $LAT_PATCH)"
fi

# ==========================================================================
# 11. Install the AgentPhone bridge
# ==========================================================================
step 11 "Install the AgentPhone webhook bridge"

install_managed   "$STAGE/agentphone-bridge/agentphone_bridge.py"   "$BRIDGE_DIR/agentphone_bridge.py"   0700
install_managed   "$STAGE/agentphone-bridge/test_event_ordering.py" "$BRIDGE_DIR/test_event_ordering.py" 0644
merge_env_defaults "$STAGE/agentphone-bridge/env"                   "$BRIDGE_DIR/env"                    0600
chmod 0700 "$BRIDGE_DIR/agentphone_bridge.py"
chmod 0600 "$BRIDGE_DIR/env"
ok "AgentPhone bridge installed (dormant until AGENTPHONE_API_KEY + AGENTPHONE_AGENT_ID exist)"

# ==========================================================================
# 12. Install the launcher scripts + desktop entries
# ==========================================================================
step 12 "Install the launcher scripts and desktop entries"

install_managed "$FILES_DIR/gateway-run.sh"            "$PREFIX_BIN/hermes-gateway-run.sh"                  0755
install_managed "$FILES_DIR/agentphone-bridge-run.sh"  "$PREFIX_BIN/nicks-stack-agentphone-bridge-run.sh"   0755
install_managed "$FILES_DIR/onboard.sh"                "$PREFIX_BIN/nicks-stack-onboard.sh"                 0755
install_managed "$FILES_DIR/op-enable.py"              "$PREFIX_BIN/nicks-stack-op-enable"                  0755

install_managed "$FILES_DIR/onboard-launch.sh"         "$PREFIX_BIN/nicks-stack-onboard-launch.sh"          0755
install_managed "$FILES_DIR/telegram-pair.py"          "$PREFIX_BIN/nicks-stack-telegram-pair.py"           0755
install_managed "$FILES_DIR/obsidian-launch"           "$PREFIX_BIN/obsidian-launch"                        0755
install_managed "$FILES_DIR/ollama-run.sh"             "$PREFIX_BIN/nicks-stack-ollama-run.sh"              0755
install_managed "$FILES_DIR/jack.sh"                   "$PREFIX_BIN/jack"                                   0755

# Routing CLI: a symlink so the tree copy stays the single source of the code.
ROUTE_TARGET="$HERMES_HOME/scripts/provider-routing/route.py"
if [[ -L "$PREFIX_BIN/nicks-stack-route" \
      && "$(readlink -f "$PREFIX_BIN/nicks-stack-route")" == "$ROUTE_TARGET" ]]; then
  skip "routing CLI symlink already correct"
elif [[ -f "$ROUTE_TARGET" ]]; then
  ln -sf "$ROUTE_TARGET" "$PREFIX_BIN/nicks-stack-route"
  ok "linked $PREFIX_BIN/nicks-stack-route -> $ROUTE_TARGET"
  CHANGES=$((CHANGES + 1))
else
  warn "routing CLI not found at $ROUTE_TARGET — /mode commands will not work"
fi

# Composio is REQUESTED whenever platform.yaml declares a composio: block —
# that block is what makes Gmail/Calendar/Drive/Contacts/Notion resolvable at
# all. If it is requested and cannot be completed, that is a failed install,
# not a warning: continuing would hand over a machine whose declared
# capabilities silently do not exist. --skip-composio opts out deliberately.
COMPOSIO_REQUESTED=0
if ((SKIP_COMPOSIO == 0)) && grep -q '^composio:' "$HERMES_HOME/platform.yaml" 2>/dev/null; then
  COMPOSIO_REQUESTED=1
fi

# Composio session manager: symlink, NOT a copy. composio_session.py does
# `sys.path.insert(0, Path(__file__).resolve().parent); import lib`, and
# Path.resolve() FOLLOWS a symlink back to the scripts tree — so the relative
# import works. A copy resolves to /usr/local/bin, where lib.py does not exist,
# and the launcher dies with ModuleNotFoundError before any Composio code runs.
# Same reason route.py and doctor.py are linked. (op-enable.py is copied safely
# because it has no relative import.) A symlink also means there is no second
# copy whose version header can drift from the deployed platform.
COMPOSIO_TARGET="$HERMES_HOME/scripts/platform/composio_session.py"
if [[ -L "$PREFIX_BIN/nicks-stack-composio-session" \
      && "$(readlink -f "$PREFIX_BIN/nicks-stack-composio-session")" == "$COMPOSIO_TARGET" ]]; then
  skip "composio session symlink already correct"
elif [[ -f "$COMPOSIO_TARGET" ]]; then
  # A previous release installed a COPY here; replace it.
  if [[ -e "$PREFIX_BIN/nicks-stack-composio-session" && ! -L "$PREFIX_BIN/nicks-stack-composio-session" ]]; then
    backup_of "$PREFIX_BIN/nicks-stack-composio-session"
    rm -f "$PREFIX_BIN/nicks-stack-composio-session"
    info "replaced the copied composio launcher with a symlink"
  fi
  ln -sf "$COMPOSIO_TARGET" "$PREFIX_BIN/nicks-stack-composio-session"
  ok "linked $PREFIX_BIN/nicks-stack-composio-session -> $COMPOSIO_TARGET"
  CHANGES=$((CHANGES + 1))
else
  warn "composio session manager not found at $COMPOSIO_TARGET"
fi

# Unified runtime secrets (v1.1.8): same symlink pattern, same reason.
SECRETS_TARGET="$HERMES_HOME/scripts/platform/secrets_runtime.py"
if [[ -L "$PREFIX_BIN/nicks-stack-secrets" \
      && "$(readlink -f "$PREFIX_BIN/nicks-stack-secrets")" == "$SECRETS_TARGET" ]]; then
  skip "runtime secrets symlink already correct"
elif [[ -f "$SECRETS_TARGET" ]]; then
  if [[ -e "$PREFIX_BIN/nicks-stack-secrets" && ! -L "$PREFIX_BIN/nicks-stack-secrets" ]]; then
    backup_of "$PREFIX_BIN/nicks-stack-secrets"
    rm -f "$PREFIX_BIN/nicks-stack-secrets"
  fi
  ln -sf "$SECRETS_TARGET" "$PREFIX_BIN/nicks-stack-secrets"
  ok "linked $PREFIX_BIN/nicks-stack-secrets -> $SECRETS_TARGET"
  CHANGES=$((CHANGES + 1))
else
  warn "runtime secrets manager not found at $SECRETS_TARGET"
fi

# Render the runtime secrets NOW, so the composio init below (and the first
# gateway start) have the credentials they need. Non-fatal: the renderer is
# fail-closed and says exactly which required secret is missing.
if [[ -x "$PREFIX_BIN/nicks-stack-secrets" ]]; then
  if "$PREFIX_BIN/nicks-stack-secrets" render; then
    ok "runtime secrets rendered into $HERMES_HOME/runtime/secrets.env (0600)"
  else
    warn "runtime secrets NOT rendered — the gateway will start without a required
    credential. Diagnose with:  sudo jack secrets status"
  fi
fi

# Provider doctor: same symlink pattern as the router.
DOCTOR_TARGET="$HERMES_HOME/scripts/provider-routing/doctor.py"
if [[ -L "$PREFIX_BIN/nicks-stack-provider-doctor" \
      && "$(readlink -f "$PREFIX_BIN/nicks-stack-provider-doctor")" == "$DOCTOR_TARGET" ]]; then
  skip "provider doctor symlink already correct"
elif [[ -f "$DOCTOR_TARGET" ]]; then
  ln -sf "$DOCTOR_TARGET" "$PREFIX_BIN/nicks-stack-provider-doctor"
  ok "linked $PREFIX_BIN/nicks-stack-provider-doctor -> $DOCTOR_TARGET"
  CHANGES=$((CHANGES + 1))
else
  warn "provider doctor not found at $DOCTOR_TARGET"
fi

install_managed "$FILES_DIR/Obsidian.desktop"          "$DESKTOP_DIR/Obsidian.desktop"                      0755
install_managed "$FILES_DIR/NicksStackSetup.desktop"   "$DESKTOP_DIR/NicksStackSetup.desktop"               0755
ok "launchers and desktop entries installed"

# ==========================================================================
# Composio Sessions SDK — installed HERE, in the install phase, immediately
# after the launcher that needs it.
#
# v1.1.4: this block used to sit AFTER `step 16 "Summary"` and after the
# `exit 1` health-failure gate. Any run with a failing health check exited
# before reaching it, so the SDK was never installed — while the launcher
# symlink (step 12, above) had already been placed. That is exactly the
# reported state: launcher works, venv exists from an older run, composio
# absent, and no pip error anywhere because pip was never invoked.
# ==========================================================================
COMPOSIO_VENV="${STACK_ROOT}/composio-venv"

# THE acceptance test, verbatim: if this fails, the SDK is not installed.
composio_installed() {
  [[ -x "$COMPOSIO_VENV/bin/python" ]] || return 1
  "$COMPOSIO_VENV/bin/python" -c "import composio" 2>/dev/null
}

# The runtime path is whatever the LAUNCHER does, so run the launcher rather
# than reimplementing its import logic here. `status --offline --json` reports
# sdk_importable, which is the result of composio_session._sdk() itself — the
# exact code the gateway will execute. Reimplementing it is how a verification
# drifts from the thing it claims to verify.  (v1.1.5)
composio_runtime_ok() {
  "$PREFIX_BIN/nicks-stack-composio-session" status --offline --json 2>/dev/null \
    | python3 -c "import json,sys
try: sys.exit(0 if json.load(sys.stdin).get('sdk_importable') else 1)
except Exception: sys.exit(1)"
}

# Why the runtime path can fail even when the venv's own python succeeds:
# composio pulls in compiled extensions (pydantic_core, jiter,
# charset_normalizer) tagged for one CPython minor version. The launcher runs
# under the SYSTEM python3 and appends the venv's site-packages, so if the venv
# was built by a different interpreter the .so files will not load — while
# <venv>/bin/python imports them perfectly. Rebuilding the venv with the
# current python3 is the repair; weakening the check is not.
composio_interpreter_mismatch() {
  local venv_v sys_v
  [[ -x "$COMPOSIO_VENV/bin/python" ]] || return 1
  venv_v="$("$COMPOSIO_VENV/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  sys_v="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  [[ -n "$venv_v" && -n "$sys_v" && "$venv_v" != "$sys_v" ]]
}

if ((COMPOSIO_REQUESTED == 0)); then
  log "Composio not requested (no composio: block, or --skip-composio) — skipping the SDK"
elif composio_installed && ! composio_interpreter_mismatch; then
  skip "composio SDK already installed ($COMPOSIO_VENV)"
else
  info "installing the composio SDK into $COMPOSIO_VENV"
  COMPOSIO_ERR=""

  # A venv with no pip is a half-built venv (ensurepip failed on a previous
  # run). Rebuild rather than trying to pip-install with a pip that is absent.
  if [[ -d "$COMPOSIO_VENV" && ! -x "$COMPOSIO_VENV/bin/pip" ]]; then
    warn "$COMPOSIO_VENV exists but has no pip — rebuilding it"
    rm -rf "$COMPOSIO_VENV"
  fi

  # A venv built by a different interpreter than the one that runs the launcher
  # cannot serve it — its compiled extensions are ABI-locked to that version.
  if composio_interpreter_mismatch; then
    warn "$COMPOSIO_VENV was built by python $("$COMPOSIO_VENV/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null) but the launcher runs under python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') — rebuilding it"
    rm -rf "$COMPOSIO_VENV"
  fi

  if [[ ! -x "$COMPOSIO_VENV/bin/python" ]]; then
    if ! COMPOSIO_ERR="$(python3 -m venv "$COMPOSIO_VENV" 2>&1)"; then
      err "could not create $COMPOSIO_VENV:"
      printf '%s\n' "$COMPOSIO_ERR" | sed 's/^/    /'
      die "Composio is required by platform.yaml but its venv could not be created.
    Most often this is the missing python3-venv package:
        sudo apt-get install -y python3-venv python3-pip
    Then re-run this script, or pass --skip-composio to install without Composio."
    fi
    ok "created $COMPOSIO_VENV"
  fi

  if ! COMPOSIO_ERR="$("$COMPOSIO_VENV/bin/pip" install --disable-pip-version-check composio 2>&1)"; then
    err "pip could not install the composio SDK:"
    printf '%s\n' "$COMPOSIO_ERR" | tail -25 | sed 's/^/    /'
    die "Composio is required by platform.yaml but the SDK could not be installed.
    Check network/proxy egress to PyPI (pip honours HTTPS_PROXY/PIP_INDEX_URL),
    then re-run, or pass --skip-composio."
  fi

  # THE acceptance test. Nothing below runs unless this passes.
  if ! composio_installed; then
    err "pip reported success but the SDK is still not importable:"
    "$COMPOSIO_VENV/bin/python" -c "import composio" 2>&1 | sed 's/^/    /' || true
    printf '%s\n' "$COMPOSIO_ERR" | tail -10 | sed 's/^/    pip: /'
    die "acceptance test failed:
        $COMPOSIO_VENV/bin/python -c \"import composio\"
    The venv exists but the SDK is not in it. Rebuild with:
        sudo rm -rf $COMPOSIO_VENV && sudo bash platform/bootstrap.sh"
  fi
  ok "composio SDK installed ($("$COMPOSIO_VENV/bin/python" -c 'import composio; print(composio.__version__)' 2>/dev/null || echo 'version unknown'))"
  CHANGES=$((CHANGES + 1))
fi

# Verify the EXACT runtime path: run the launcher and read its own verdict.
if ((COMPOSIO_REQUESTED)) && [[ -x "$PREFIX_BIN/nicks-stack-composio-session" ]]; then
  if composio_runtime_ok; then
    ok "composio SDK importable by the launcher (the runtime path itself)"
    # Import ORDER, not just importability: the venv's dependency set must win
    # over Ubuntu's /usr/lib/python3/dist-packages. A system typing_extensions
    # shadowing the venv's is what produced
    #   cannot import name 'Sentinel' from 'typing_extensions'
    if "$PREFIX_BIN/nicks-stack-composio-session" deps >/dev/null 2>&1; then
      ok "composio dependencies resolve from the venv, not dist-packages"
    else
      err "a system package is shadowing a Composio dependency:"
      "$PREFIX_BIN/nicks-stack-composio-session" deps 2>&1 | sed 's/^/    /' || true
      die "the launcher is not running under the dependency set bootstrap installed.
    Run for detail:  sudo nicks-stack-composio-session deps"
    fi
  else
    # Never hide the reason — the exception text IS the diagnosis. A
    # pydantic_core/jiter/charset_normalizer error means an interpreter
    # mismatch; anything else is a genuinely broken install.
    err "the launcher cannot import the composio SDK. Its own report:"
    "$PREFIX_BIN/nicks-stack-composio-session" status --offline --json 2>&1 \
      | python3 -c "import json,sys
try: print('    ' + (json.load(sys.stdin).get('sdk_error') or 'no error reported'))
except Exception: print('    launcher produced no parseable status')" || true
    die "the composio SDK is installed in $COMPOSIO_VENV (its own python imports it)
    but the launcher cannot. Rebuild the venv with the interpreter that runs the
    launcher:
        sudo rm -rf $COMPOSIO_VENV && sudo bash platform/bootstrap.sh"
  fi
fi

# Mint/resume Jack's Composio session now so the first gateway start is warm.
# Prints presence only — no key, no URL.
if ((COMPOSIO_REQUESTED)) && [[ -x "$PREFIX_BIN/nicks-stack-composio-session" ]]; then
  if "$PREFIX_BIN/nicks-stack-composio-session" init; then
    ok "composio session ready"
  else
    info "composio session not established yet (usually a missing COMPOSIO_API_KEY)"
    info "  sudo nicks-stack-composio-session status"
  fi
fi

# ==========================================================================
# 13. Install the Supervisor services
# ==========================================================================
step 13 "Install the Supervisor services (hermes-gateway, agentphone-bridge)"

# Find the include dir supervisord actually reads.
SUPERVISOR_CONFD=""
for conf in /etc/supervisor/supervisord.conf /etc/supervisord.conf; do
  [[ -f "$conf" ]] || continue
  inc="$(awk -F= '/^\[include\]/{i=1;next} /^\[/{i=0} i && $1 ~ /^[[:space:]]*files/ {sub(/^[[:space:]]*/,"",$2); print $2; exit}' "$conf" || true)"
  if [[ -n "$inc" ]]; then
    SUPERVISOR_CONFD="$(dirname "${inc%% *}")"
    [[ "$SUPERVISOR_CONFD" == /* ]] || SUPERVISOR_CONFD="$(dirname "$conf")/$SUPERVISOR_CONFD"
    log "supervisord config: $conf   include dir: $SUPERVISOR_CONFD"
    break
  fi
done
if [[ -z "$SUPERVISOR_CONFD" ]]; then
  SUPERVISOR_CONFD="/etc/supervisor/conf.d"
  warn "could not read an [include] section — defaulting to $SUPERVISOR_CONFD"
fi
mkdir -p "$SUPERVISOR_CONFD"

STACK_CONF="$SUPERVISOR_CONFD/nicks-stack.conf"

# Never fight an existing definition of the same program name.
conflicts=()
for prog in hermes-gateway agentphone-bridge ollama; do
  while IFS= read -r f; do
    if [[ "$f" == "$STACK_CONF" ]]; then
      continue
    fi
    conflicts+=("$prog in $f")
  done < <(grep -rls "^\[program:${prog}\]" "$SUPERVISOR_CONFD" 2>/dev/null || true)
done

if ((${#conflicts[@]} > 0)); then
  for c in "${conflicts[@]}"; do
    warn "supervisor program already defined elsewhere: $c"
  done
  warn "leaving the existing supervisor definitions in place — NOT writing $STACK_CONF"
else
  SUPERVISOR_TMP="$(mktemp)"
  cat > "$SUPERVISOR_TMP" <<'SUPERVISORCONF'
; ==========================================================================
; Nick's Stack — supervised services
; Mirrors apps[].services in build_template.py: both run as root with
; restart=always. Both wrappers self-gate (the gateway waits for config.yaml
; + auth.json; the bridge waits for its two AgentPhone keys), so neither
; crash-loops before the stack is configured.
; Managed by platform/bootstrap.sh — edits here are overwritten on re-run.
; ==========================================================================

[program:hermes-gateway]
command=/usr/local/bin/hermes-gateway-run.sh
directory=/root
user=root
autostart=true
autorestart=true
startsecs=10
startretries=999
stopasgroup=true
killasgroup=true
stopwaitsecs=30
environment=HOME="/root",HERMES_HOME="/root/.hermes",USER="root"
stdout_logfile=/var/log/orgo/hermes-gateway.out.log
stderr_logfile=/var/log/orgo/hermes-gateway.err.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB

[program:agentphone-bridge]
command=/usr/local/bin/nicks-stack-agentphone-bridge-run.sh
directory=/root/.hermes_agentphone_bridge
user=root
autostart=true
autorestart=true
startsecs=10
startretries=999
stopasgroup=true
killasgroup=true
stopwaitsecs=30
environment=HOME="/root",USER="root"
stdout_logfile=/root/.hermes_agentphone_bridge/supervisor.out.log
stderr_logfile=/root/.hermes_agentphone_bridge/supervisor.err.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
SUPERVISORCONF

  # Take ownership of any hand-started server before Supervisor claims the
  # port. A manual `ollama serve` and the supervised one fight over :11434 and
  # the supervised one loses silently.  (v1.0.1 bug 3)
  if ((WITH_OLLAMA)) || have ollama; then
    MANUAL_OLLAMA="$(pgrep -f "ollama serve" 2>/dev/null | tr '\n' ' ' || true)"
    if [[ -n "${MANUAL_OLLAMA// /}" ]]; then
      SUPERVISED_OLLAMA=0
      if have supervisorctl && supervisorctl status ollama 2>/dev/null | grep -q RUNNING; then
        SUPERVISED_OLLAMA=1
      fi
      if ((SUPERVISED_OLLAMA)); then
        skip "ollama already running under Supervisor — leaving it alone"
      else
        info "stopping hand-started 'ollama serve' (pids:${MANUAL_OLLAMA}) so Supervisor can own it"
        pkill -TERM -f "ollama serve" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          pgrep -f "ollama serve" >/dev/null 2>&1 || break
          sleep 1
        done
        if pgrep -f "ollama serve" >/dev/null 2>&1; then
          pkill -KILL -f "ollama serve" 2>/dev/null || true
          sleep 1
        fi
        if pgrep -f "ollama serve" >/dev/null 2>&1; then
          warn "could not stop the manual ollama process — the supervised service may fail to bind :11434"
        else
          ok "manual Ollama stopped; Supervisor will start it"
          CHANGES=$((CHANGES + 1))
        fi
      fi
    fi
  fi

  # Ollama is a managed platform service like the other two, but only when it
  # is wanted: writing a program for a machine that will never run local AI
  # just adds a permanently-stopped entry to `supervisorctl status`.
  if ((WITH_OLLAMA)) || have ollama; then
    cat >> "$SUPERVISOR_TMP" <<'SUPERVISOROLLAMA'

[program:ollama]
command=/usr/local/bin/nicks-stack-ollama-run.sh
directory=/root
user=root
autostart=true
autorestart=true
startsecs=10
startretries=999
stopasgroup=true
killasgroup=true
stopwaitsecs=30
environment=HOME="/root",USER="root"
stdout_logfile=/var/log/orgo/ollama.out.log
stderr_logfile=/var/log/orgo/ollama.err.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
SUPERVISOROLLAMA
    info "supervisor conf includes the ollama service"
  else
    info "supervisor conf omits ollama (not requested, not installed)"
  fi

  install_managed "$SUPERVISOR_TMP" "$STACK_CONF" 0644
  rm -f "$SUPERVISOR_TMP"
fi

# Make supervisord pick the programs up (and start it if it is not running).
if supervisorctl status >/dev/null 2>&1; then
  info "supervisorctl reread / update"
  supervisorctl reread || warn "supervisorctl reread reported an error"
  supervisorctl update || warn "supervisorctl update reported an error"
  supervisorctl status || true
  ok "supervisor services registered"
elif have supervisord; then
  warn "supervisord is not responding — attempting to start it"
  if have systemctl && systemctl start supervisor >/dev/null 2>&1; then
    ok "started supervisor via systemd"
  elif service supervisor start >/dev/null 2>&1; then
    ok "started supervisor via service(8)"
  else
    warn "could not start supervisord automatically — start it, then run: supervisorctl reread && supervisorctl update"
  fi
  supervisorctl reread >/dev/null 2>&1 || true
  supervisorctl update >/dev/null 2>&1 || true
else
  warn "no supervisord on this host — services are configured at $STACK_CONF but nothing is supervising them"
fi

# ==========================================================================
# 14. Enable autostart
# ==========================================================================
step 14 "Enable desktop autostart (onboarding +10s, Obsidian +16s)"

# apps[].autostart parity: same two commands, same delays, as XDG entries.
write_autostart() {
  local file="$1" name="$2" comment="$3" delay="$4" exec_path="$5"
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<AUTOSTART
[Desktop Entry]
Type=Application
Version=1.0
Name=$name
Comment=$comment
Exec=/bin/sh -c 'sleep $delay; exec $exec_path'
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=$delay
AUTOSTART
  install_managed "$tmp" "$file" 0644
  rm -f "$tmp"
}

write_autostart "$AUTOSTART_DIR/nicks-stack-onboard.desktop" \
  "Nick's Stack Setup" "First-boot onboarding (Nous, Telegram, 1Password)" \
  10 "$PREFIX_BIN/nicks-stack-onboard-launch.sh"

write_autostart "$AUTOSTART_DIR/nicks-stack-obsidian.desktop" \
  "Obsidian" "Hermes Knowledge Base" \
  16 "$PREFIX_BIN/obsidian-launch"

ok "autostart entries enabled in $AUTOSTART_DIR"

# ==========================================================================
# 15. Hermes health checks
# ==========================================================================
step 15 "Run Hermes health checks"

health_check "hermes binary on PATH"            command -v hermes
health_check "hermes --version responds"        bash -c 'hermes --version >/dev/null 2>&1'
health_check "config.yaml present"              test -s "$HERMES_HOME/config.yaml"
health_check "config.yaml is mode 0600"         bash -c "[ \"\$(stat -c '%a' '$HERMES_HOME/config.yaml')\" = 600 ]"
health_check "config.yaml parses as YAML"       python3 -c "import yaml,sys;yaml.safe_load(open('$HERMES_HOME/config.yaml'))"
health_check "SOUL.md present"                  test -s "$HERMES_HOME/SOUL.md"
health_check ".env present and mode 0600"       bash -c "[ -s '$HERMES_HOME/.env' ] && [ \"\$(stat -c '%a' '$HERMES_HOME/.env')\" = 600 ]"
health_check "plugins tree populated"           bash -c "[ -n \"\$(ls -A '$HERMES_HOME/plugins' 2>/dev/null)\" ]"
health_check "skills tree populated"            bash -c "[ -n \"\$(ls -A '$HERMES_HOME/skills' 2>/dev/null)\" ]"
health_check "scripts tree populated"           bash -c "[ -n \"\$(ls -A '$HERMES_HOME/scripts' 2>/dev/null)\" ]"
health_check "local-packages tree populated"    bash -c "[ -n \"\$(ls -A '$HERMES_HOME/local-packages' 2>/dev/null)\" ]"
health_check "Obsidian vault present"           test -d "$VAULT_DIR"
health_check "AgentPhone bridge installed"      test -x "$BRIDGE_DIR/agentphone_bridge.py"
health_check "gateway wrapper installed"        test -x "$PREFIX_BIN/hermes-gateway-run.sh"
health_check "bridge wrapper installed"         test -x "$PREFIX_BIN/nicks-stack-agentphone-bridge-run.sh"
health_check "onboarding script installed"      test -x "$PREFIX_BIN/nicks-stack-onboard.sh"
health_check "platform spec installed"          test -s "$HERMES_HOME/platform.yaml"
health_check "jack command installed"           test -x "$PREFIX_BIN/jack"
health_check "platform library installed"       test -f "$HERMES_HOME/scripts/platform/lib.py"
health_check "routing map installed"            test -s "$HERMES_HOME/routing.yaml"
health_check "routing CLI installed"            test -x "$PREFIX_BIN/nicks-stack-route"
health_check "provider doctor installed"        test -x "$PREFIX_BIN/nicks-stack-provider-doctor"
if ((WITH_OLLAMA)) || have ollama; then
  health_check "ollama wrapper installed"       test -x "$PREFIX_BIN/nicks-stack-ollama-run.sh"
fi
health_check "op CLI works"                     bash -c 'op --version >/dev/null 2>&1'
health_check "cloudflared works"                bash -c 'cloudflared --version >/dev/null 2>&1'
health_check "Obsidian binary present"          test -x /opt/Obsidian/obsidian
health_check "filesystem MCP helper present"    bash -c 'npm ls -g --depth=0 @modelcontextprotocol/server-filesystem >/dev/null 2>&1'

# BEHAVIOUR, not implementation: the launcher must actually execute. This is
# what catches a broken import path, a bad interpreter or a missing library —
# regardless of whether the launcher is a symlink, a copy or a wrapper.
# `status --offline` touches no network and no provider.
#
# The test is "produced parseable JSON", NOT "exited 0": status exits 1 when
# Composio is merely unconfigured (no session yet), which is a normal state on
# a fresh machine and says nothing about whether the launcher works.
if ((COMPOSIO_REQUESTED)); then
  health_check "nicks-stack-composio-session executes" bash -c \
    "'$PREFIX_BIN/nicks-stack-composio-session' status --offline --json | python3 -c 'import json,sys; json.load(sys.stdin)'"
  # The acceptance test, verbatim. The install above already dies on failure;
  # this re-asserts it here so a machine that drifted is caught by a plain
  # health-check run too.
  health_check "composio SDK importable in its venv" \
    "$COMPOSIO_VENV/bin/python" -c "import composio"
  health_check "composio deps resolve from the venv" \
    "$PREFIX_BIN/nicks-stack-composio-session" deps
fi

# Informational only — these are what the onboarding is FOR.
if [[ -s "$HERMES_HOME/auth.json" ]]; then
  ok "model account already connected (auth.json present)"
else
  info "model account not connected yet — the onboarding does this (step 1/4)"
fi
if grep -q '^TELEGRAM_BOT_TOKEN=..' "$HERMES_HOME/.env" 2>/dev/null; then
  ok "Telegram bot already configured"
else
  info "Telegram bot not configured yet — the onboarding does this (step 2/4)"
fi
if [[ -s "$HERMES_HOME/.op.env" ]]; then
  ok "1Password service-account token present"
else
  info "1Password not connected yet — optional, the onboarding does this (step 3/4)"
fi

if supervisorctl status >/dev/null 2>&1; then
  SERVICE_LIST=(hermes-gateway agentphone-bridge)
  if ((WITH_OLLAMA)) || have ollama; then SERVICE_LIST+=(ollama); fi
  for prog in "${SERVICE_LIST[@]}"; do
    line="$(supervisorctl status "$prog" 2>/dev/null || true)"
    if [[ -n "$line" ]]; then
      log "supervisor: $line"
    else
      warn "supervisor program not registered: $prog"
    fi
  done
fi

# ==========================================================================
# 16. Summary + next onboarding command
# ==========================================================================
step 16 "Summary"

log "changes applied this run: $CHANGES"

if ((${#WARNINGS[@]} > 0)); then
  printf '\n%sWarnings (%s):%s\n' "$C_YELLOW" "${#WARNINGS[@]}" "$C_RESET"
  for w in "${WARNINGS[@]}"; do printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$w"; done
fi

if ((${#HEALTH_FAILURES[@]} > 0)); then
  printf '\n%sHealth check failures (%s):%s\n' "$C_RED" "${#HEALTH_FAILURES[@]}" "$C_RESET"
  for f in "${HEALTH_FAILURES[@]}"; do printf '  %s✗%s %s\n' "$C_RED" "$C_RESET" "$f"; done
  err "bootstrap finished with failing health checks — review the log: $LOG_FILE"
  exit 1
fi

# ==========================================================================
# Runtime profiles — rebuild the derived validation profile from the config
# that was just installed, so the first `jack doctor --providers` after a
# deploy already runs lean instead of building it on demand.  (v1.0.3)
# ==========================================================================
PLATFORM_LIB_SCRIPT="$HERMES_HOME/scripts/platform/lib.py"
if [[ -f "$PLATFORM_LIB_SCRIPT" ]]; then
  PROFILE_RESULT="$(python3 - "$PLATFORM_LIB_SCRIPT" <<'PROFILEBUILD'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import lib
for name, spec in lib.profiles_spec().items():
    if name == "use" or not isinstance(spec, dict) or spec.get("kind") == "full":
        continue
    res = lib.profile_ensure(name, force=True)
    print(f"{name}: {res['reason']}")
PROFILEBUILD
)" || PROFILE_RESULT=""
  if [[ -n "$PROFILE_RESULT" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && ok "runtime profile $line"
    done <<< "$PROFILE_RESULT"
  else
    # A profile is an optimisation: probes still work without one.
    info "no runtime profile built — validation will use the full runtime"
  fi
fi

# ==========================================================================
# Deployment report — generated from the same detection every other tool uses,
# and persisted as the machine-readable manifest.
# ==========================================================================
MANIFEST_SCRIPT="$HERMES_HOME/scripts/platform/manifest.py"
if [[ -f "$MANIFEST_SCRIPT" ]]; then
  if NICKS_STACK_REPO="$REPO_ROOT" python3 "$MANIFEST_SCRIPT" write >/dev/null 2>&1; then
    ok "platform manifest written: $STACK_ROOT/platform-manifest.json"
  else
    warn "could not write the platform manifest"
  fi
  printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
  NICKS_STACK_REPO="$REPO_ROOT" python3 "$MANIFEST_SCRIPT" report 2>/dev/null || \
    warn "could not render the deployment report"
  printf '%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
else
  warn "platform scripts missing — no deployment report (expected at $MANIFEST_SCRIPT)"
fi

printf '\n%sHealth report:%s  sudo jack doctor\n' "$C_BOLD" "$C_RESET"
printf '%sLog:%s           %s\n' "$C_BOLD" "$C_RESET" "$LOG_FILE"

ok "bootstrap complete"
exit 0
