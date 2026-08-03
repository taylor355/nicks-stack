#!/usr/bin/env bash
# ==========================================================================
# Nick's Stack — safe in-place update for an existing deployment
# ==========================================================================
# Re-deploys the repo's managed files onto a machine that was installed with
# platform/bootstrap.sh, without touching anything the user or the agent
# created. platform/bootstrap.sh does the actual placement (it is idempotent
# and already implements the managed/preserved policy) — this script wraps it
# with the three things an update needs and an install does not:
#
#   1. a rollback point taken BEFORE anything changes
#   2. a cryptographic before/after proof that preserved data survived
#   3. the post-update steps a re-placed config.yaml requires
#      (nicks-stack-op-enable, then a gateway restart)
#
#   sudo bash platform/update.sh                     # update + restart + verify
#   sudo bash platform/update.sh --check-only        # dry run: no changes at all
#   sudo bash platform/update.sh --op-write-test     # + 1Password write probe
#   sudo bash platform/update.sh --no-restart        # update without restarting
#
# WHAT IS PRESERVED (never overwritten, and proved intact afterwards)
#   /root/.hermes/.env                 secrets + Telegram pairing (bootstrap
#                                      only appends default keys it is missing)
#   /root/.hermes/.op.env              1Password service-account token
#   /root/.hermes/auth.json            model account / OAuth credentials
#   /root/.hermes/memories/            agent memories
#   /root/.hermes/state/               sessions and runtime state
#   /root/.hermes/<anything else>      any file the agent wrote
#   /root/.hermes_agentphone_bridge/env  bridge tuning + AgentPhone identity
#   /root/Documents/HermesVault/       Obsidian vault: user notes, company data
#   /root/.config/obsidian/obsidian.json  vault registry
#
# WHAT IS UPDATED (backed up first, restorable from the rollback point)
#   config.yaml, SOUL.md, routing.yaml, plugins/, skills/, scripts/, local-packages/,
#   /usr/local/bin/* launchers, desktop entries, wallpaper, supervisor conf,
#   autostart entries.
#
# WHAT IS REGENERATED (derived, so neither preserved nor backed up)
#   /root/.hermes/profiles/            runtime profiles (v1.0.3). Generated
#                                      from config.yaml on every bootstrap run
#                                      and whenever config.yaml changes, so
#                                      they cannot drift from the runtime they
#                                      are a subset of. Nothing here is user
#                                      data; deleting the tree is harmless.
#
# EXIT CODES
#   0  update applied (or dry run completed) and all checks passed
#   1  update failed, or preserved data regressed, or verify.sh failed
#   2  could not run at all (not root, wrong OS, repo layout missing, lock held)
# ==========================================================================

set -Eeuo pipefail
IFS=$'\n\t'
umask 022

readonly SCRIPT_NAME="nicks-stack update"
readonly SCRIPT_VERSION="1.0.3"          # Taylor AI Platform

# Paths — identical to platform/bootstrap.sh.
readonly HERMES_HOME="/root/.hermes"
readonly BRIDGE_DIR="/root/.hermes_agentphone_bridge"
readonly VAULT_DIR="/root/Documents/HermesVault"
readonly OBSIDIAN_CFG_DIR="/root/.config/obsidian"
readonly PREFIX_BIN="/usr/local/bin"
readonly STACK_ROOT="/opt/nicks-stack"
readonly BACKUP_ROOT="${STACK_ROOT}/backups"

# Trees bootstrap.sh owns and replaces — everything else under $HERMES_HOME
# belongs to the user or the agent and must survive byte-for-byte.
readonly MANAGED_TREES=(plugins skills scripts local-packages)
readonly MANAGED_FILES=(config.yaml SOUL.md routing.yaml platform.yaml)

# Derived, not user data: runtime profiles (v1.0.3) are GENERATED from
# config.yaml, so their contents change whenever config.yaml legitimately
# changes. Fingerprinting them as preserved data would report a false
# "preserved data regressed" on any update that touches the config. They are
# rebuilt by bootstrap.sh on every run, so nothing is lost by excluding them.
# (Only the derived config.yaml and its stamp are real files here — the
# credential entries in a profile are symlinks, which `find -type f` skips.)
# `composio` holds the persisted session id and the regenerated MCP runtime
# env. Both are derived: the id is re-resumable and the env is re-minted on
# every gateway start, so neither belongs in the preserved-data proof.
readonly DERIVED_TREES=(profiles composio)

# Env-style files: bootstrap may APPEND default keys, so these are checked
# key-by-key (no key may vanish, no existing value may change) rather than by
# whole-file checksum.
readonly ENV_FILES=("${HERMES_HOME}/.env" "${BRIDGE_DIR}/env")

# Files copied into the rollback point verbatim (small and irreplaceable).
readonly SECRET_FILES=(
  "${HERMES_HOME}/.env"
  "${HERMES_HOME}/.op.env"
  "${HERMES_HOME}/auth.json"
  "${BRIDGE_DIR}/env"
  "${OBSIDIAN_CFG_DIR}/obsidian.json"
)

# Overridable so a second, isolated agent can point at its own vault/item
# (see platform/DEPLOYMENT.md → 5. Creating an isolated second agent).
readonly OP_VAULT="${NICKS_STACK_OP_VAULT:-Hermes}"
readonly OP_ITEM="${NICKS_STACK_OP_ITEM:-Hermes Agent Secrets}"

# ollama is restarted only when the machine actually has that program.
readonly SERVICES=(hermes-gateway agentphone-bridge ollama)

readonly LOG_FILE="${NICKS_STACK_UPDATE_LOG:-/var/log/nicks-stack-update.log}"
readonly LOCK_NAME="nicks-stack-update.lock"
# Orgo images ship /var/lock as a symlink to a /run/lock that may not exist —
# same portable fallback chain as platform/bootstrap.sh.
readonly LOCK_CANDIDATES=(
  "${NICKS_STACK_UPDATE_LOCK:-/var/lock/${LOCK_NAME}}"
  "/run/lock/${LOCK_NAME}"
  "/tmp/${LOCK_NAME}"
)

CHECK_ONLY=0
OP_WRITE_TEST=0
DO_RESTART=1
RUN_VERIFY=1

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

WARNINGS=()
REGRESSIONS=()

_ts() { date -Iseconds; }
log()  { printf '%s[%s] %s%s\n'    "$C_DIM"    "$(_ts)" "$*" "$C_RESET"; }
info() { printf '%s[%s] ›  %s%s\n' "$C_DIM"    "$(_ts)" "$*" "$C_RESET"; }
ok()   { printf '%s[%s] ✓  %s%s\n' "$C_GREEN"  "$(_ts)" "$*" "$C_RESET"; }
warn() { printf '%s[%s] !  %s%s\n' "$C_YELLOW" "$(_ts)" "$*" "$C_RESET"; WARNINGS+=("$*"); }
err()  { printf '%s[%s] ✗  %s%s\n' "$C_RED"    "$(_ts)" "$*" "$C_RESET"; }

step() {
  printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
  printf '%s[%s] %s%s\n' "$C_BLUE$C_BOLD" "$(_ts)" "$*" "$C_RESET"
  printf '%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
}

die()      { err "$*"; err "update ABORTED — full log: $LOG_FILE"; exit 2; }
die_fail() { err "$*"; err "update FAILED — full log: $LOG_FILE"; exit 1; }

on_error() {
  local exit_code=$? line="$1" cmd="$2"
  err "unexpected failure (exit $exit_code) at line $line: $cmd"
  if [[ -n "${BACKUP_DIR:-}" ]]; then
    err "rollback point: $BACKUP_DIR  (see platform/DEPLOYMENT.md → Rollback)"
  fi
  exit 1
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<USAGE
${SCRIPT_NAME} v${SCRIPT_VERSION}

Safe in-place update of a Nick's Stack deployment. Takes a rollback point,
re-runs platform/bootstrap.sh, then proves preserved data survived.

  sudo bash platform/update.sh [options]

  --check-only     dry run — take the inventory and report, change nothing
  --op-write-test  probe 1Password WRITE permission with a temporary field
                   (creates one uniquely-named field, reads it back, deletes
                   it; no existing field is touched, no value is printed)
  --no-restart     apply the update but leave the services running as-is
  --no-verify      skip the platform/verify.sh run at the end
  --help           this message

Exit: 0 = success, 1 = update/verification failure, 2 = could not run.
USAGE
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# is_managed_path <absolute-path> — true for files bootstrap.sh owns, and for
# generated trees that are rebuilt from those files rather than kept.
is_managed_path() {
  local path="$1" rel name
  case "$path" in
    "$HERMES_HOME"/*) rel="${path#"$HERMES_HOME"/}" ;;
    *) return 1 ;;
  esac
  for name in "${MANAGED_FILES[@]}"; do
    if [[ "$rel" == "$name" ]]; then return 0; fi
  done
  for name in "${MANAGED_TREES[@]}" "${DERIVED_TREES[@]}"; do
    if [[ "$rel" == "$name/"* ]]; then return 0; fi
  done
  return 1
}

# preserved_manifest <output-file>
# One "<sha256>  <path>" line per preserved file, sorted. Env files are
# excluded — bootstrap may legitimately append default keys to those, so they
# get a key-level comparison instead (see env_pairs).
preserved_manifest() {
  local out="$1" f skip
  : > "$out"
  {
    if [[ -d "$HERMES_HOME" ]]; then
      while IFS= read -r -d '' f; do
        if is_managed_path "$f"; then continue; fi
        skip=0
        for e in "${ENV_FILES[@]}"; do
          if [[ "$f" == "$e" ]]; then skip=1; fi
        done
        if ((skip)); then continue; fi
        printf '%s\0' "$f"
      done < <(find "$HERMES_HOME" -type f -print0 2>/dev/null)
    fi
    for d in "$VAULT_DIR" "$BRIDGE_DIR" "$OBSIDIAN_CFG_DIR"; do
      if [[ -d "$d" ]]; then
        while IFS= read -r -d '' f; do
          skip=0
          for e in "${ENV_FILES[@]}"; do
            if [[ "$f" == "$e" ]]; then skip=1; fi
          done
          if ((skip)); then continue; fi
          printf '%s\0' "$f"
        done < <(find "$d" -type f -print0 2>/dev/null)
      fi
    done
  } | sort -z | xargs -0 -r sha256sum > "$out" 2>/dev/null || true
  wc -l < "$out" | tr -d ' '
}

# env_pairs <env-file> — "KEY <sha256 of value>" lines. Values are hashed, so
# this file can be diffed and logged without ever exposing a secret.
env_pairs() {
  local file="$1" line key value
  [[ -r "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    key="${key##*[[:space:]]}"
    value="${line#*=}"
    printf '%s %s\n' "$key" "$(printf '%s' "$value" | sha256sum | cut -d' ' -f1)"
  done < "$file" | sort -u
}

count_files() {
  local dir="$1"
  if [[ -d "$dir" ]]; then
    find "$dir" -type f 2>/dev/null | wc -l | tr -d ' '
  else
    printf '0'
  fi
}

# --------------------------------------------------------------------------
# 1Password write-permission probe (opt-in)
# --------------------------------------------------------------------------
# Adds ONE uniquely-named temporary field to the stack's item, reads it back,
# and deletes it — including on failure, via a cleanup trap. It never reads,
# writes or prints any existing field, and the probe value is the literal
# string "ok", not a secret.
OP_PROBE_FIELD=""
OP_PROBE_TOKEN=""

op_probe_cleanup() {
  if [[ -n "$OP_PROBE_FIELD" && -n "$OP_PROBE_TOKEN" ]]; then
    if OP_SERVICE_ACCOUNT_TOKEN="$OP_PROBE_TOKEN" \
       op item edit "$OP_ITEM" --vault "$OP_VAULT" "${OP_PROBE_FIELD}[delete]=" >/dev/null 2>&1; then
      ok "1Password probe field removed: $OP_PROBE_FIELD"
    else
      warn "could not remove the 1Password probe field '$OP_PROBE_FIELD' — delete it by hand in the '$OP_ITEM' item"
    fi
  fi
  OP_PROBE_FIELD=""
  OP_PROBE_TOKEN=""
}

op_write_test() {
  step "1Password write-permission probe"

  if ! have op; then
    warn "op CLI not installed — skipping the write probe"
    return 0
  fi

  local token=""
  if [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    token="$OP_SERVICE_ACCOUNT_TOKEN"
  elif [[ -r "$HERMES_HOME/.op.env" ]]; then
    token="$(sed -n 's/^OP_SERVICE_ACCOUNT_TOKEN=//p' "$HERMES_HOME/.op.env" | head -1)"
  fi
  if [[ -z "$token" ]]; then
    warn "no 1Password service-account token — skipping the write probe"
    return 0
  fi

  if ! OP_SERVICE_ACCOUNT_TOKEN="$token" op whoami >/dev/null 2>&1; then
    err "1Password token present but 'op whoami' failed — cannot probe write permission"
    REGRESSIONS+=("1Password token does not authenticate")
    return 0
  fi
  ok "service account authenticates"

  # Unique per run: PID + epoch, so a stale probe can never collide with a
  # real field or with a concurrent run.
  OP_PROBE_FIELD="nicks_stack_write_probe_$$_$(date +%s)"
  OP_PROBE_TOKEN="$token"
  trap 'op_probe_cleanup' EXIT

  info "writing temporary field '$OP_PROBE_FIELD' to op://$OP_VAULT/$OP_ITEM (value: the literal string 'ok')"
  if ! OP_SERVICE_ACCOUNT_TOKEN="$token" \
       op item edit "$OP_ITEM" --vault "$OP_VAULT" "${OP_PROBE_FIELD}[text]=ok" >/dev/null 2>&1; then
    err "WRITE DENIED — the service account cannot add a field to op://$OP_VAULT/$OP_ITEM"
    err "  the agent will not be able to save keys it obtains itself (AgentMail inbox, Telegram token, …)"
    err "  fix: grant the service account write access to the '$OP_VAULT' vault"
    REGRESSIONS+=("1Password write permission denied")
    OP_PROBE_FIELD=""; OP_PROBE_TOKEN=""
    trap - EXIT
    return 0
  fi
  ok "write accepted"

  # Read back — compare against the known literal, so nothing secret is involved.
  local readback
  readback="$(OP_SERVICE_ACCOUNT_TOKEN="$token" \
              op read "op://$OP_VAULT/$OP_ITEM/$OP_PROBE_FIELD" 2>/dev/null || true)"
  if [[ "$readback" == "ok" ]]; then
    ok "read-back matches — read+write round trip confirmed"
  else
    warn "probe field written but did not read back as expected (op:// resolution may lag)"
  fi
  unset readback

  local probe_field="$OP_PROBE_FIELD"
  op_probe_cleanup
  trap - EXIT

  # Confirm the field is really gone, so we never leave residue behind.
  if OP_SERVICE_ACCOUNT_TOKEN="$token" \
     op read "op://$OP_VAULT/$OP_ITEM/$probe_field" >/dev/null 2>&1; then
    warn "probe field '$probe_field' may still be present in '$OP_ITEM' — remove it by hand"
  else
    ok "item is back to its original field set"
  fi
  unset token probe_field
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)       usage; exit 0 ;;
    --check-only)    CHECK_ONLY=1 ;;
    --op-write-test) OP_WRITE_TEST=1 ;;
    --no-restart)    DO_RESTART=0 ;;
    --no-verify)     RUN_VERIFY=0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PLATFORM_DIR="$(dirname "$SCRIPT_PATH")"
REPO_ROOT="$(dirname "$PLATFORM_DIR")"
BOOTSTRAP="$PLATFORM_DIR/bootstrap.sh"
VERIFY="$PLATFORM_DIR/verify.sh"

printf '\n%s%s v%s%s\n' "$C_BOLD" "$SCRIPT_NAME" "$SCRIPT_VERSION" "$C_RESET"
log "started   : $(_ts)"
log "repo root : $REPO_ROOT"
log "log file  : $LOG_FILE"
if ((CHECK_ONLY)); then
  log "mode      : CHECK-ONLY (nothing on this machine will be modified)"
fi

[[ "$(id -u)" -eq 0 ]] || die "must run as root (try: sudo bash $0)"
[[ -r /etc/os-release ]] || die "/etc/os-release not readable — cannot identify this host"
[[ -f "$BOOTSTRAP" ]] || die "platform/bootstrap.sh not found next to this script — run from a nicks-stack checkout"
[[ -d "$REPO_ROOT/files" ]] || die "source tree not found: $REPO_ROOT/files"
[[ -d "$HERMES_HOME" ]] || die "$HERMES_HOME does not exist — this machine has no deployment to update (run platform/bootstrap.sh first)"
have sha256sum || die "sha256sum is required for the preserved-data integrity proof"

# Single-instance lock, portable across images that lack /var/lock and /run/lock.
LOCK_PATH=""
for lock_candidate in "${LOCK_CANDIDATES[@]}"; do
  lock_dir="$(dirname "$lock_candidate")"
  lock_real_dir="$(readlink -f "$lock_dir" 2>/dev/null || true)"
  [[ -n "$lock_real_dir" ]] || lock_real_dir="$lock_dir"
  if [[ ! -d "$lock_real_dir" ]]; then
    if ! mkdir -p "$lock_real_dir" 2>/dev/null; then
      continue
    fi
  fi
  if touch "$lock_candidate" 2>/dev/null && exec 9>>"$lock_candidate"; then
    LOCK_PATH="$lock_candidate"
    break
  fi
done
[[ -n "$LOCK_PATH" ]] || die "could not create a lock file in any of: ${LOCK_CANDIDATES[*]}"
flock -n 9 || die "another update is already running (lock: $LOCK_PATH)"

if [[ -d "$REPO_ROOT/.git" ]] && have git; then
  GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  GIT_DIRTY="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | head -1)"
  log "repo      : $GIT_BRANCH @ $GIT_COMMIT"
  if [[ -n "$GIT_DIRTY" ]]; then
    warn "working tree has uncommitted changes — deploying them as-is"
  fi
else
  GIT_COMMIT="unknown"
  GIT_BRANCH="unknown"
fi

# ==========================================================================
step "1/6  Inventory: preserved data, before the update"
# ==========================================================================
if ((CHECK_ONLY)); then
  # A dry run must leave no trace on the machine: the inventory goes to a
  # temporary directory instead of a real rollback point under /opt.
  BACKUP_DIR="$(mktemp -d -t nicks-stack-check-XXXXXX)"
  ok "check-only workspace (temporary): $BACKUP_DIR"
else
  BACKUP_DIR="${BACKUP_ROOT}/$(date +%Y%m%dT%H%M%S)-update"
  mkdir -p "$BACKUP_DIR"
  ok "rollback point: $BACKUP_DIR"
fi

BEFORE_MANIFEST="$BACKUP_DIR/preserved-before.sha256"
PRESERVED_COUNT="$(preserved_manifest "$BEFORE_MANIFEST")"
ok "preserved files fingerprinted: $PRESERVED_COUNT"

for f in "${ENV_FILES[@]}"; do
  if [[ -r "$f" ]]; then
    env_pairs "$f" > "$BACKUP_DIR/envkeys-before$(printf '%s' "$f" | tr '/' '_')"
    log "env keys tracked in $f: $(env_pairs "$f" | wc -l | tr -d ' ') (names + value hashes only)"
  fi
done

log "vault notes  : $(count_files "$VAULT_DIR")"
log "memories     : $(count_files "$HERMES_HOME/memories")"
log "state        : $(count_files "$HERMES_HOME/state")"

# ==========================================================================
step "2/6  Rollback point: copy the files this update may replace"
# ==========================================================================
copy_into_backup() {
  local src="$1" dest="$BACKUP_DIR/${1#/}"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$src" "$dest"
    log "backed up: $src"
  fi
}

if ((CHECK_ONLY)); then
  info "check-only: skipping the file copy (no rollback point is created)"
else

# Managed files/trees — the rollback material.
for name in "${MANAGED_FILES[@]}"; do copy_into_backup "$HERMES_HOME/$name"; done
for name in "${MANAGED_TREES[@]}"; do copy_into_backup "$HERMES_HOME/$name"; done
for launcher in \
  hermes-gateway-run.sh nicks-stack-agentphone-bridge-run.sh nicks-stack-onboard.sh \
  nicks-stack-op-enable nicks-stack-onboard-launch.sh nicks-stack-telegram-pair.py obsidian-launch \
  nicks-stack-ollama-run.sh jack
do
  copy_into_backup "$PREFIX_BIN/$launcher"
done
copy_into_backup "/etc/supervisor/conf.d/nicks-stack.conf"
copy_into_backup "$BRIDGE_DIR/agentphone_bridge.py"

# Secrets and pairing state — tiny, irreplaceable, copied so a rollback can
# restore them even if something else on the machine damages them.
for f in "${SECRET_FILES[@]}"; do copy_into_backup "$f"; done
chmod -R go-rwx "$BACKUP_DIR"

cat > "$BACKUP_DIR/MANIFEST.txt" <<MANIFEST
nicks-stack rollback point
created      : $(_ts)
created by   : $SCRIPT_NAME v$SCRIPT_VERSION
repo         : $GIT_BRANCH @ $GIT_COMMIT
repo path    : $REPO_ROOT
preserved    : $PRESERVED_COUNT files fingerprinted in preserved-before.sha256
restore with : see platform/DEPLOYMENT.md → 4. Rollback
MANIFEST
ok "rollback point complete ($(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1))"
fi

# ==========================================================================
if ((OP_WRITE_TEST)); then
  op_write_test
fi

# ==========================================================================
if ((CHECK_ONLY)); then
  step "check-only: stopping before any change"
  ok "nothing on this machine was modified"
  log "inventory workspace (safe to delete): $BACKUP_DIR"
  if ((${#REGRESSIONS[@]} > 0)); then
    printf '\n%sProblems found:%s\n' "$C_RED" "$C_RESET"
    for r in "${REGRESSIONS[@]}"; do printf '  %s✗%s %s\n' "$C_RED" "$C_RESET" "$r"; done
    exit 1
  fi
  printf '\n%sCHECK-ONLY COMPLETE%s — re-run without --check-only to apply\n' "$C_GREEN$C_BOLD" "$C_RESET"
  exit 0
fi

# ==========================================================================
step "3/6  Apply: re-run platform/bootstrap.sh (the placement source of truth)"
# ==========================================================================
info "bootstrap.sh is idempotent: it installs only what is missing and backs up"
info "any managed file whose content changed before replacing it"
BOOTSTRAP_RC=0
bash "$BOOTSTRAP" || BOOTSTRAP_RC=$?
if ((BOOTSTRAP_RC != 0)); then
  err "bootstrap.sh exited $BOOTSTRAP_RC"
  err "rollback point: $BACKUP_DIR (see platform/DEPLOYMENT.md → Rollback)"
  die_fail "update did not complete"
fi
ok "bootstrap.sh completed"

# ==========================================================================
step "4/6  Post-update: re-enable the 1Password map and restart services"
# ==========================================================================
# bootstrap.sh re-places config.yaml from the repo, where the 1Password map
# ships DISABLED on purpose. nicks-stack-op-enable flips it back on when a
# token exists, exactly as onboarding and on_resume do.
if [[ -x "$PREFIX_BIN/nicks-stack-op-enable" ]]; then
  if python3 "$PREFIX_BIN/nicks-stack-op-enable"; then
    ok "1Password map reconciled"
  else
    warn "nicks-stack-op-enable reported an error — op:// references may not resolve"
  fi
else
  warn "$PREFIX_BIN/nicks-stack-op-enable missing — cannot re-enable the 1Password map"
fi

if ((DO_RESTART)); then
  if have supervisorctl && supervisorctl status >/dev/null 2>&1; then
    supervisorctl reread >/dev/null 2>&1 || warn "supervisorctl reread reported an error"
    supervisorctl update >/dev/null 2>&1 || warn "supervisorctl update reported an error"
    for svc in "${SERVICES[@]}"; do
      if ! supervisorctl status "$svc" >/dev/null 2>&1; then
        log "service not configured on this machine, skipping: $svc"
        continue
      fi
      if supervisorctl restart "$svc" >/dev/null 2>&1; then
        ok "restarted: $svc"
      else
        warn "could not restart $svc — check: supervisorctl status $svc"
      fi
    done
  else
    warn "supervisord is not responding — services were not restarted"
  fi
else
  info "--no-restart: services left as they were (config changes apply on next restart)"
fi

# Refresh the machine-readable manifest so it reflects what was just deployed.
MANIFEST_SCRIPT="$HERMES_HOME/scripts/platform/manifest.py"
if [[ -f "$MANIFEST_SCRIPT" ]]; then
  if NICKS_STACK_REPO="$REPO_ROOT" python3 "$MANIFEST_SCRIPT" write >/dev/null 2>&1; then
    ok "platform manifest refreshed"
  else
    warn "could not refresh the platform manifest"
  fi
fi

# ==========================================================================
step "5/6  Proof: preserved data must be byte-identical"
# ==========================================================================
AFTER_MANIFEST="$BACKUP_DIR/preserved-after.sha256"
AFTER_COUNT="$(preserved_manifest "$AFTER_MANIFEST")"
log "preserved files after update: $AFTER_COUNT (before: $PRESERVED_COUNT)"

# Every line in the BEFORE manifest must still be present in the AFTER
# manifest: same path, same checksum. New files are fine (the agent keeps
# working during an update); missing or altered ones are not.
MISSING_OR_CHANGED="$(comm -23 <(sort "$BEFORE_MANIFEST") <(sort "$AFTER_MANIFEST") || true)"
if [[ -n "$MISSING_OR_CHANGED" ]]; then
  err "preserved data regressed — these files are missing or were modified:"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    err "  ${line#*  }"
    REGRESSIONS+=("modified or removed: ${line#*  }")
  done <<< "$MISSING_OR_CHANGED"
else
  ok "all $PRESERVED_COUNT preserved files are byte-identical"
fi

# Env files: keys may be ADDED (bootstrap fills in missing non-secret
# defaults) but no existing key may disappear or change value.
for f in "${ENV_FILES[@]}"; do
  before_file="$BACKUP_DIR/envkeys-before$(printf '%s' "$f" | tr '/' '_')"
  [[ -r "$before_file" ]] || continue
  after_pairs="$(env_pairs "$f")"
  lost=""
  while IFS= read -r pair; do
    [[ -n "$pair" ]] || continue
    if ! grep -qxF "$pair" <<< "$after_pairs"; then
      lost+="${pair%% *} "
    fi
  done < "$before_file"
  if [[ -n "$lost" ]]; then
    err "keys lost or changed in $f: $lost"
    REGRESSIONS+=("env keys lost or changed in $f: $lost")
  else
    ok "$f: every pre-existing key survived unchanged ($(wc -l < "$before_file" | tr -d ' ') keys)"
  fi
done

# Data-volume sanity: a preserved tree must never shrink.
for pair in "$VAULT_DIR:vault notes" "$HERMES_HOME/memories:memories" "$HERMES_HOME/state:state"; do
  dir="${pair%%:*}"; label="${pair##*:}"
  after_n="$(count_files "$dir")"
  case "$label" in
    "vault notes") before_n="$(grep -c "^[0-9a-f]\{64\}  $VAULT_DIR/" "$BEFORE_MANIFEST" || true)" ;;
    memories)      before_n="$(grep -c "^[0-9a-f]\{64\}  $HERMES_HOME/memories/" "$BEFORE_MANIFEST" || true)" ;;
    state)         before_n="$(grep -c "^[0-9a-f]\{64\}  $HERMES_HOME/state/" "$BEFORE_MANIFEST" || true)" ;;
    *)             before_n=0 ;;
  esac
  before_n="${before_n:-0}"
  if ((after_n < before_n)); then
    err "$label shrank: $before_n → $after_n files"
    REGRESSIONS+=("$label shrank from $before_n to $after_n files")
  else
    ok "$label intact: $before_n → $after_n files"
  fi
done

# ==========================================================================
step "6/6  Verify"
# ==========================================================================
VERIFY_RC=0
if ((RUN_VERIFY)) && [[ -f "$VERIFY" ]]; then
  bash "$VERIFY" || VERIFY_RC=$?
  if ((VERIFY_RC == 0)); then
    ok "platform/verify.sh passed"
  else
    err "platform/verify.sh exited $VERIFY_RC"
  fi
elif ((RUN_VERIFY)); then
  warn "platform/verify.sh not found — skipping verification"
fi

# ==========================================================================
# Summary
# ==========================================================================
printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
log "rollback point : $BACKUP_DIR"
log "deployed       : $GIT_BRANCH @ $GIT_COMMIT"

if ((${#WARNINGS[@]} > 0)); then
  printf '\n%sWarnings (%s):%s\n' "$C_YELLOW" "${#WARNINGS[@]}" "$C_RESET"
  for w in "${WARNINGS[@]}"; do printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$w"; done
fi

if ((${#REGRESSIONS[@]} > 0)); then
  printf '\n%sData regressions (%s):%s\n' "$C_RED" "${#REGRESSIONS[@]}" "$C_RESET"
  for r in "${REGRESSIONS[@]}"; do printf '  %s✗%s %s\n' "$C_RED" "$C_RESET" "$r"; done
  printf '\n%sUPDATE FAILED SAFETY CHECKS%s\n' "$C_RED$C_BOLD" "$C_RESET"
  printf '  Roll back using platform/DEPLOYMENT.md → 4. Rollback\n'
  printf '  rollback point: %s\n' "$BACKUP_DIR"
  exit 1
fi

if ((VERIFY_RC != 0)); then
  printf '\n%sUPDATE APPLIED BUT VERIFICATION FAILED%s\n' "$C_RED$C_BOLD" "$C_RESET"
  printf '  preserved data is intact; the deployment itself has failing checks.\n'
  printf '  rollback point: %s\n' "$BACKUP_DIR"
  exit 1
fi

printf '\n%sUPDATE COMPLETE%s — preserved data proved intact, deployment verified\n' "$C_GREEN$C_BOLD" "$C_RESET"
exit 0
