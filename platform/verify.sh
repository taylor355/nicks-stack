#!/usr/bin/env bash
# ==========================================================================
# Nick's Stack — read-only deployment verification
# ==========================================================================
# Checks that a machine bootstrapped with platform/bootstrap.sh is complete
# and healthy. platform/bootstrap.sh and build_template.py are the sources of
# truth for every path, mode and service name asserted here.
#
#   sudo bash platform/verify.sh            # full report
#   sudo bash platform/verify.sh --quiet    # failures + summary only
#
# READ-ONLY GUARANTEE
#   This script never creates, modifies, moves or deletes a file, never
#   writes to 1Password, and never starts, stops or restarts a service. It
#   only stats files, reads config, parses YAML, queries `supervisorctl
#   status`, resolves op:// references, and GETs the bridge health endpoint.
#   There is no flag that changes that — the 1Password *write*-permission
#   test lives in `platform/update.sh --op-write-test` (and is documented in
#   platform/DEPLOYMENT.md), deliberately kept out of this script.
#
# SECRET SAFETY
#   No secret value is ever printed. Keys are reported as present/absent
#   only; op:// references are reported as "resolved" or "empty", never
#   echoed, never length-disclosed.
#
# EXIT CODES
#   0  every CRITICAL check passed (advisory warnings may still be present)
#   1  at least one CRITICAL check failed
#   2  could not run the checks at all (not root, unreadable /etc/os-release)
# ==========================================================================

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_NAME="Taylor AI Platform verify"
readonly SCRIPT_VERSION="1.1.30"

# Paths — identical to platform/bootstrap.sh.
readonly HERMES_HOME="/root/.hermes"
readonly VENV_PY="/usr/local/lib/hermes-agent/venv/bin/python"
readonly BRIDGE_DIR="/root/.hermes_agentphone_bridge"
readonly VAULT_DIR="/root/Documents/HermesVault"
readonly OBSIDIAN_CFG_DIR="/root/.config/obsidian"
readonly DESKTOP_DIR="/root/Desktop"
readonly AUTOSTART_DIR="/root/.config/autostart"
readonly WALLPAPER_DIR="/usr/share/backgrounds"
readonly ORGO_LIB="/var/lib/orgo"
readonly PREFIX_BIN="/usr/local/bin"
readonly STACK_ROOT="/opt/nicks-stack"

# The 1Password item the stack's secret map points at.
# Overridable so a second, isolated agent can point at its own vault/item
# (see platform/DEPLOYMENT.md → 5. Creating an isolated second agent).
readonly OP_VAULT="${NICKS_STACK_OP_VAULT:-Hermes}"
readonly OP_ITEM="${NICKS_STACK_OP_ITEM:-Hermes Agent Secrets}"

readonly BRIDGE_HEALTH_URL="http://127.0.0.1:8787/health"
readonly ROUTING_FILE="${HERMES_HOME}/routing.yaml"
readonly ROUTE_CLI="${PREFIX_BIN}/nicks-stack-route"
readonly DOCTOR_CLI="${PREFIX_BIN}/nicks-stack-provider-doctor"
readonly OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}/api/tags"
readonly PLATFORM_LIB="${HERMES_HOME}/scripts/platform/lib.py"
readonly PLATFORM_FILE="${HERMES_HOME}/platform.yaml"
readonly JACK_CLI="${PREFIX_BIN}/jack"
SUPERVISOR_CONFD="/etc/supervisor/conf.d"

QUIET=0

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_BLUE=$'\033[1;36m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
else
  C_RESET=''; C_DIM=''; C_BOLD=''; C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi

CRITICAL_FAILURES=()
ADVISORY_WARNINGS=()
PASS_COUNT=0

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  if ((QUIET == 0)); then
    printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"
  fi
}
fail() {
  CRITICAL_FAILURES+=("$*")
  printf '  %s✗ CRITICAL%s %s\n' "$C_RED" "$C_RESET" "$*"
}
warn() {
  ADVISORY_WARNINGS+=("$*")
  printf '  %s! advisory%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
}
note() {
  if ((QUIET == 0)); then
    printf '  %s·%s %s\n' "$C_DIM" "$C_RESET" "$*"
  fi
}
section() {
  if ((QUIET == 0)); then
    printf '\n%s%s%s\n' "$C_BLUE$C_BOLD" "$*" "$C_RESET"
  fi
}
die() {
  printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2
  exit 2
}

usage() {
  cat <<USAGE
${SCRIPT_NAME} v${SCRIPT_VERSION}

Read-only verification of a Nick's Stack deployment. Writes nothing.

  sudo bash platform/verify.sh [--quiet]

  --quiet   print only failures, advisories and the summary
  --help    this message

Exit: 0 = all critical checks passed, 1 = critical failure, 2 = cannot run.

The 1Password write-permission test is NOT part of this script (it would
break the read-only guarantee). Run it with:

  sudo bash platform/update.sh --op-write-test --check-only
USAGE
}

# --------------------------------------------------------------------------
# Check helpers — every one of these is read-only
# --------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# check_critical <label> <command...>
check_critical() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    pass "$label"
  else
    fail "$label"
  fi
}

# check_advisory <label> <command...>
check_advisory() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    pass "$label"
  else
    warn "$label"
  fi
}

# check_mode <path> <expected-octal> <critical|advisory>
check_mode() {
  local path="$1" want="$2" severity="${3:-critical}" got
  if [[ ! -e "$path" ]]; then
    if [[ "$severity" == "critical" ]]; then
      fail "missing: $path"
    else
      warn "missing: $path"
    fi
    return 0
  fi
  got="$(stat -c '%a' "$path" 2>/dev/null || echo '?')"
  if [[ "$got" == "${want#0}" || "$got" == "$want" ]]; then
    pass "$path is mode $want"
  elif [[ "$severity" == "critical" ]]; then
    fail "$path is mode $got, expected $want"
  else
    warn "$path is mode $got, expected $want"
  fi
}

# tree_populated <dir> — non-empty directory test
tree_populated() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  [[ -n "$(ls -A "$dir" 2>/dev/null)" ]]
}

count_files() {
  local dir="$1"
  if [[ -d "$dir" ]]; then
    find "$dir" -type f 2>/dev/null | wc -l | tr -d ' '
  else
    printf '0'
  fi
}

# env_key_present <file> <KEY> — true when KEY is set to a non-trivial value.
# Reads the file but never prints any value from it.
env_key_present() {
  local file="$1" key="$2"
  [[ -r "$file" ]] || return 1
  grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=.." "$file" 2>/dev/null
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --quiet|-q) QUIET=1 ;;
  "") ;;
  *) usage >&2; die "unknown argument: $1" ;;
esac

printf '\n%s%s v%s%s  —  %s\n' "$C_BOLD" "$SCRIPT_NAME" "$SCRIPT_VERSION" "$C_RESET" "$(date -Iseconds)"
printf '%sread-only: this run will not modify anything on this machine%s\n' "$C_DIM" "$C_RESET"

[[ "$(id -u)" -eq 0 ]] || die "must run as root — the stack's config and env files are mode 0600 (try: sudo bash $0)"
[[ -r /etc/os-release ]] || die "/etc/os-release not readable — cannot identify this host"

# ==========================================================================
section "1. Platform"
# ==========================================================================
# shellcheck disable=SC1091
. /etc/os-release
note "host: ${PRETTY_NAME:-${ID:-unknown}}  kernel: $(uname -r)  arch: $(uname -m)"

if [[ -r "$PLATFORM_FILE" ]]; then
  pass "platform spec present: $(python3 -c "
import yaml,sys
d=yaml.safe_load(open('$PLATFORM_FILE')) or {}
print(f\"{d.get('name','?')} v{d.get('version','?')} ({d.get('status','?')})\")" 2>/dev/null || echo unreadable)"
else
  fail "platform spec missing: $PLATFORM_FILE"
fi
check_critical "jack command installed"     test -x "$JACK_CLI"
check_critical "platform library installed" test -f "$PLATFORM_LIB"

case " ${ID:-} ${ID_LIKE:-} " in
  *" ubuntu "*|*" debian "*) pass "supported distribution (${ID:-unknown} ${VERSION_ID:-})" ;;
  *) warn "unsupported distribution '${ID:-unknown}' — the stack targets Ubuntu/Debian" ;;
esac

# ==========================================================================
section "2. Binaries"
# ==========================================================================
export PATH="/usr/local/bin:${HERMES_HOME}/bin:${HERMES_HOME}/node/bin:/root/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

check_critical "hermes on PATH"                 have hermes
check_critical "hermes venv python present"     test -x "$VENV_PY"
check_critical "op (1Password CLI) on PATH"     have op
check_critical "cloudflared on PATH"            have cloudflared
check_critical "Obsidian binary present"        test -x /opt/Obsidian/obsidian
check_critical "python3 on PATH"                have python3
check_critical "node on PATH"                   have node
check_critical "npm on PATH"                    have npm
check_critical "supervisorctl on PATH"          have supervisorctl

if have hermes; then
  note "hermes: $(hermes --version 2>&1 | head -1)"
fi
if have op; then
  note "op: $(op --version 2>&1 | head -1)"
fi
if have cloudflared; then
  note "cloudflared: $(cloudflared --version 2>&1 | head -1)"
fi

# Global npm helpers — the filesystem MCP backs the Obsidian vault server.
if have npm; then
  check_critical "npm helper: @modelcontextprotocol/server-filesystem" \
    npm ls -g --depth=0 @modelcontextprotocol/server-filesystem
  check_advisory "npm helper: agent-cards"    npm ls -g --depth=0 agent-cards
  check_advisory "npm helper: @xdevplatform/xurl" npm ls -g --depth=0 @xdevplatform/xurl
fi

# qrcode inside the Hermes venv — the Telegram pairing QR needs it.
if [[ -x "$VENV_PY" ]]; then
  check_advisory "qrcode available in the Hermes venv" "$VENV_PY" -c 'import qrcode'
fi

# ==========================================================================
section "3. Hermes files and permissions"
# ==========================================================================
check_critical "config.yaml present"    test -s "$HERMES_HOME/config.yaml"
check_mode     "$HERMES_HOME/config.yaml" 0600 critical
check_critical "SOUL.md present"        test -s "$HERMES_HOME/SOUL.md"
check_critical ".env present"           test -s "$HERMES_HOME/.env"
check_mode     "$HERMES_HOME/.env"        0600 critical

check_critical "plugins tree populated"        tree_populated "$HERMES_HOME/plugins"
check_critical "skills tree populated"         tree_populated "$HERMES_HOME/skills"
check_critical "scripts tree populated"        tree_populated "$HERMES_HOME/scripts"
check_critical "local-packages tree populated" tree_populated "$HERMES_HOME/local-packages"

check_critical "AgentPhone bridge installed"   test -x "$BRIDGE_DIR/agentphone_bridge.py"
check_mode     "$BRIDGE_DIR/agentphone_bridge.py" 0700 critical
check_mode     "$BRIDGE_DIR/env"                  0600 critical

for launcher in \
  hermes-gateway-run.sh \
  nicks-stack-agentphone-bridge-run.sh \
  nicks-stack-onboard.sh \
  nicks-stack-op-enable \
  nicks-stack-onboard-launch.sh \
  nicks-stack-telegram-pair.py \
  obsidian-launch
do
  check_critical "launcher installed: $PREFIX_BIN/$launcher" test -x "$PREFIX_BIN/$launcher"
done

check_advisory "desktop entry: Obsidian"          test -f "$DESKTOP_DIR/Obsidian.desktop"
check_advisory "desktop entry: Nick's Stack Setup" test -f "$DESKTOP_DIR/NicksStackSetup.desktop"
check_advisory "wallpaper installed"               test -f "$WALLPAPER_DIR/wallpaper.jpg"
check_advisory "obsidian vault registry present"   test -f "$OBSIDIAN_CFG_DIR/obsidian.json"
check_advisory "first-boot stamp present"          test -f "$ORGO_LIB/nicks-stack.stamp"

# ==========================================================================
section "4. Configuration integrity"
# ==========================================================================
CONFIG_OK=0
if [[ -r "$HERMES_HOME/config.yaml" ]] && python3 -c "
import yaml,sys
yaml.safe_load(open('$HERMES_HOME/config.yaml'))
" >/dev/null 2>&1; then
  pass "config.yaml parses as YAML"
  CONFIG_OK=1
else
  fail "config.yaml does not parse as YAML"
fi

MODEL_DEFAULT=""
MODEL_PROVIDER=""
OP_ENABLED="false"
OP_HAS_MODEL_KEY=0
MODEL_KEY_ENV=""

if ((CONFIG_OK)); then
  # One python read, emitting only non-secret metadata (names and booleans;
  # op:// references are locations, not values).
  CFG_SUMMARY="$(python3 - "$HERMES_HOME/config.yaml" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
if not isinstance(cfg, dict):
    cfg = {}
model = cfg.get("model") or {}
op = ((cfg.get("secrets") or {}).get("onepassword") or {})
env_map = op.get("env") or {}
provider = str(model.get("provider", ""))
key_env = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
}.get(provider, "")
print(f"default={model.get('default','')}")
print(f"provider={provider}")
print(f"base_url={model.get('base_url','')}")
print(f"op_enabled={str(bool(op.get('enabled'))).lower()}")
print(f"op_vars={len(env_map)}")
print(f"key_env={key_env}")
print(f"op_has_key={'true' if key_env and key_env in env_map else 'false'}")
PY
)" 2>/dev/null || CFG_SUMMARY=""

  while IFS='=' read -r k v; do
    case "$k" in
      default)   MODEL_DEFAULT="$v" ;;
      provider)  MODEL_PROVIDER="$v" ;;
      op_enabled) OP_ENABLED="$v" ;;
      key_env)   MODEL_KEY_ENV="$v" ;;
      op_has_key) if [[ "$v" == "true" ]]; then OP_HAS_MODEL_KEY=1; fi ;;
      *) ;;
    esac
  done <<< "$CFG_SUMMARY"

  note "model.default: ${MODEL_DEFAULT:-<unset>}   model.provider: ${MODEL_PROVIDER:-<unset>}"

  if [[ -n "$MODEL_DEFAULT" ]]; then
    pass "model.default is set"
  else
    fail "model.default is not set — the agent has no model to run"
  fi
  if [[ -n "$MODEL_PROVIDER" ]]; then
    pass "model.provider is set (${MODEL_PROVIDER})"
  else
    fail "model.provider is not set"
  fi

  if [[ "$OP_ENABLED" == "true" ]]; then
    pass "secrets.onepassword.enabled = true"
  elif [[ -f "$HERMES_HOME/runtime/secrets.env" ]]; then
    # Disabled ON PURPOSE since v1.1.8: enabling it makes Hermes resolve all 21
    # op:// references at every start — sequential `op read`, /dev/tty prompts,
    # cold-start stalls, MCP timeouts. The runtime secrets renderer resolves the
    # declared subset instead, and section 8a is what proves that worked. A
    # standing warning about the intended configuration is noise.
    note "secrets.onepassword.enabled = false (by design) — declared secrets arrive via the runtime env; see section 8a"
  else
    warn "secrets.onepassword.enabled = false and no runtime env is rendered — nothing will resolve op:// references (run: sudo nicks-stack-secrets render)"
  fi
fi

# ==========================================================================
section "5. Model credentials (presence only — no values read out)"
# ==========================================================================
MODEL_KEY_AVAILABLE=0
if [[ -n "$MODEL_KEY_ENV" ]]; then
  # The rendered runtime env FIRST (v1.1.9). This is the plane the gateway
  # actually reads: gateway-run.sh sources this 0600 file before exec. Checking
  # the 1Password map first and this second is how verify.sh came to report
  #   ✗ no source for ANTHROPIC_API_KEY — the agent cannot reach its model
  # about a gateway that was authenticating with Anthropic at that moment, and
  # to file three advisories about a map that is disabled BY DESIGN. Section 8a
  # already proves this file is rendered, 0600 and complete; section 5 just has
  # to stop ignoring it.
  if env_key_present "$HERMES_HOME/runtime/secrets.env" "$MODEL_KEY_ENV"; then
    pass "$MODEL_KEY_ENV is in the gateway runtime env (the plane the gateway reads)"
    MODEL_KEY_AVAILABLE=1
  fi
  if ((OP_HAS_MODEL_KEY)) && [[ "$OP_ENABLED" == "true" ]]; then
    pass "$MODEL_KEY_ENV is mapped in the 1Password secret plane"
    MODEL_KEY_AVAILABLE=1
  elif ((OP_HAS_MODEL_KEY)) && ((MODEL_KEY_AVAILABLE == 0)); then
    # Only worth saying when nothing else supplied the key. The map being
    # disabled is the deliberate v1.1.8 posture, not a finding in itself.
    warn "$MODEL_KEY_ENV is mapped in 1Password but the map is disabled, and it is not in the runtime env"
  fi
  if env_key_present "$HERMES_HOME/.env" "$MODEL_KEY_ENV"; then
    pass "$MODEL_KEY_ENV is present in ~/.hermes/.env"
    MODEL_KEY_AVAILABLE=1
  fi
  if ((MODEL_KEY_AVAILABLE == 0)); then
    fail "no source for $MODEL_KEY_ENV (provider '${MODEL_PROVIDER}') — the agent cannot reach its model"
  fi
else
  # OAuth-style providers (nous, openai-codex, anthropic OAuth) authenticate
  # through auth.json rather than an API-key env var.
  if [[ -s "$HERMES_HOME/auth.json" ]]; then
    pass "provider '${MODEL_PROVIDER:-unknown}' uses OAuth and auth.json is present"
  else
    fail "provider '${MODEL_PROVIDER:-unknown}' has no API-key mapping and auth.json is missing/empty"
  fi
fi

# ==========================================================================
section "6. 1Password secret plane (read-only)"
# ==========================================================================
if [[ -s "$HERMES_HOME/.op.env" ]]; then
  pass "service-account token file present (~/.hermes/.op.env)"
  check_mode "$HERMES_HOME/.op.env" 0600 critical

  if have op; then
    # Read the token into this shell's env only; it is never printed.
    OP_TOKEN="$(sed -n 's/^OP_SERVICE_ACCOUNT_TOKEN=//p' "$HERMES_HOME/.op.env" | head -1)"
    if [[ -n "$OP_TOKEN" ]]; then
      if OP_SERVICE_ACCOUNT_TOKEN="$OP_TOKEN" op whoami >/dev/null 2>&1; then
        pass "1Password service account authenticates"

        if OP_SERVICE_ACCOUNT_TOKEN="$OP_TOKEN" \
           op item get "$OP_ITEM" --vault "$OP_VAULT" --format json >/dev/null 2>&1; then
          pass "item readable: op://$OP_VAULT/$OP_ITEM (read permission confirmed)"
        else
          fail "cannot read op://$OP_VAULT/$OP_ITEM — the service account lacks read access or the item is misnamed"
        fi

        if [[ -n "$MODEL_KEY_ENV" ]] && ((OP_HAS_MODEL_KEY)); then
          # Resolve the reference and test only that it is non-empty. The
          # value is consumed by `wc -c` and never printed or stored.
          if [[ "$(OP_SERVICE_ACCOUNT_TOKEN="$OP_TOKEN" \
                   op read "op://$OP_VAULT/$OP_ITEM/$MODEL_KEY_ENV" 2>/dev/null | wc -c)" -gt 1 ]]; then
            pass "op:// reference for $MODEL_KEY_ENV resolves to a non-empty value"
          else
            fail "op:// reference for $MODEL_KEY_ENV resolves empty — the field is missing or blank in 1Password"
          fi
        fi
      else
        fail "1Password service-account token present but 'op whoami' fails (expired or revoked token)"
      fi
    else
      warn "$HERMES_HOME/.op.env exists but contains no OP_SERVICE_ACCOUNT_TOKEN"
    fi
    unset OP_TOKEN
  fi
  note "write permission is NOT tested here (read-only run) — see: sudo bash platform/update.sh --op-write-test --check-only"
else
  warn "1Password not connected (~/.hermes/.op.env absent) — onboarding step 3/4 wires it"
fi

# ==========================================================================
section "7. Provider routing (config only — no model calls, no secrets)"
# ==========================================================================
check_critical "routing map present"     test -s "$ROUTING_FILE"
check_critical "routing CLI installed"   test -x "$ROUTE_CLI"
check_critical "provider doctor installed" test -x "$DOCTOR_CLI"
check_advisory "routing skill installed"   test -f "$HERMES_HOME/skills/provider-routing/SKILL.md"

if [[ -r "$ROUTING_FILE" ]]; then
  # Cross-checks routing.yaml against config.yaml: every mode must name a
  # provider Hermes actually has enabled and a key the secret plane can
  # resolve. Names and booleans only — never a key value.
  ROUTING_REPORT="$(python3 "$HERMES_HOME/scripts/provider-routing/verify_routes.py" \
                      "$ROUTING_FILE" "$HERMES_HOME/config.yaml" 2>/dev/null || true)"
  if [[ -z "$ROUTING_REPORT" ]]; then
    fail "routing map could not be evaluated (unparseable, or verify_routes.py missing)"
  else
    while IFS='|' read -r verdict message; do
      [[ -n "$message" ]] || continue
      case "$verdict" in
        PASS) pass "$message" ;;
        # Optional providers (native Gemini) are reported, never blocking.
        FAIL) case "$message" in
                *"optional"*) warn "$message" ;;
                *)            fail "$message" ;;
              esac ;;
        WARN) warn "$message" ;;
        *)    note "$message" ;;
      esac
    done <<< "$ROUTING_REPORT"
  fi

  # Selected mode lives in preserved state; absence just means "never set".
  MODE_STATE="$HERMES_HOME/state/nicks-stack-mode.json"
  if [[ -r "$MODE_STATE" ]]; then
    note "selected mode: $(sed -n 's/.*"mode"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' "$MODE_STATE" | head -1)"
  else
    note "selected mode: none recorded yet (falls back to the map's default_mode)"
  fi
fi

# ==========================================================================
section "8. Hermes runtime parity and profiles (no model calls)"
# ==========================================================================
# The gateway is a warm, long-lived process launched by hermes-gateway-run.sh,
# which bridges /root/.env and ~/.hermes/.env into its environment and passes
# --accept-hooks. Every one-shot `hermes chat` is a COLD start that must be
# given the same runtime initialisation, or it stalls where the gateway does
# not. This section proves the inputs to that parity exist. Names and booleans
# only — no secret value is read.  (v1.0.2)
RUNTIME_JSON="$(python3 "$PLATFORM_LIB" runtime 2>/dev/null || true)"
if [[ -z "$RUNTIME_JSON" ]]; then
  fail "runtime parity could not be evaluated (platform library not runnable)"
else
  json_bool() { printf '%s' "$RUNTIME_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p" | head -1; }
  json_num()  { printf '%s' "$RUNTIME_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" | head -1; }

  check_critical "hermes reachable from a non-interactive shell" have hermes
  if [[ "$(json_bool hermes_env_present)" == "true" ]]; then
    pass "$HERMES_HOME/.env present — keys can be bridged into a one-shot"
  else
    fail "$HERMES_HOME/.env missing — a one-shot hermes gets no provider keys"
  fi
  BRIDGED="$(json_num bridged_key_count)"
  note "keys bridged into a one-shot: ${BRIDGED:-0} (names only, never values)"

  if [[ "$(json_bool op_enabled)" == "true" ]]; then
    OP_REFS="$(json_num op_references)"
    if [[ "$(json_bool op_token_reachable)" == "true" ]]; then
      pass "1Password enabled (${OP_REFS:-0} refs) and the service-account token is reachable"
      if [[ "$(json_bool op_binary)" == "true" ]]; then
        pass "op binary present — references resolve without prompting"
      else
        fail "1Password map enabled but the op binary is missing — every hermes start stalls"
      fi
    else
      # This is the exact condition that makes `op read` prompt on /dev/tty.
      fail "1Password map enabled (${OP_REFS:-0} refs) but no OP_SERVICE_ACCOUNT_TOKEN is reachable — every cold hermes start will stall on /dev/tty"
    fi
  else
    note "1Password map disabled — keys resolve from .env only (no op prompt risk)"
  fi

  # Runtime profiles (v1.0.3): a validation one-shot should not be building
  # the gateway's MCP/plugin/browser stack just to answer "ok".
  PROFILE_JSON="$(python3 "$PLATFORM_LIB" profiles 2>/dev/null || true)"
  if [[ -z "$PROFILE_JSON" ]]; then
    warn "runtime profiles could not be evaluated"
  else
    VAL_PROFILE="$(printf '%s' "$RUNTIME_JSON" | sed -n 's/.*"validation_profile"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
    if [[ -z "$VAL_PROFILE" || "$VAL_PROFILE" == "null" ]]; then
      warn "no validation runtime profile declared — one-shot validation loads the full runtime"
    elif [[ "$(json_bool validation_profile_usable)" == "true" ]]; then
      # v1.0.4 measured this by running `hermes config get` per candidate.
      # That was removed in v1.0.5: on this Hermes a config read performs the
      # CLI's full startup first, so measuring cost a whole runtime boot. The
      # layout is now DECLARED in routing.yaml (profiles.selection) and checked
      # here by confirming the config file that layout points at exists.
      if [[ "$(json_bool validation_profile_isolated)" == "true" ]]; then
        VAL_LAYOUT="$(printf '%s' "$RUNTIME_JSON" | sed -n 's/.*"validation_profile_layout"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
        pass "validation profile '$VAL_PROFILE' uses selection layout '${VAL_LAYOUT:-unknown}' (declared in routing.yaml; config file present)"
      else
        warn "validation profile '$VAL_PROFILE' cannot be selected — probes fall back to the full runtime and resolve every op:// reference. Run: sudo jack profiles"
      fi
      # The delta below describes what the profile WOULD save; only report it
      # as a live saving when the profile is actually honoured.
      PROFILE_DELTA="$(python3 - "$PROFILE_JSON" "$VAL_PROFILE" <<'PROFILECHECK'
import json, sys
rep = json.loads(sys.argv[1]); name = sys.argv[2]
full = rep.get("full_runtime") or {}
lean = (rep.get("profiles") or {}).get(name) or {}
for label, key in (("MCP servers", "mcp_servers"), ("plugins", "plugins"),
                   ("op:// references", "op_references")):
    f, l = full.get(key, 0), lean.get(key, 0)
    verdict = "PASS" if l < f else "WARN"
    print(f"{verdict}|{label}: {f} in the full runtime -> {l} in '{name}'")
PROFILECHECK
)"
      while IFS='|' read -r verdict message; do
        [[ -n "$message" ]] || continue
        if [[ "$(json_bool validation_profile_isolated)" != "true" ]]; then
          note "$message  (not in effect — profile is not honoured)"
        elif [[ "$verdict" == "PASS" ]]; then
          pass "$message"
        else
          warn "$message"
        fi
      done <<< "$PROFILE_DELTA"
    else
      warn "validation profile '$VAL_PROFILE' is not usable — probes fall back to the full runtime"
    fi
  fi

  # Anything else the shared diagnosis flagged, verbatim.
  printf '%s' "$RUNTIME_JSON" \
    | sed -n '/"findings"/,/\]/p' \
    | sed -n 's/^[[:space:]]*"\(.*\)",\?$/\1/p' \
    | while IFS= read -r finding; do
        [[ -n "$finding" ]] && note "$finding"
      done
fi

# ==========================================================================
section "8a. Unified runtime secrets (v1.1.8)"
# ==========================================================================
# The gateway does NOT resolve op:// references itself (config.yaml keeps
# secrets.onepassword.enabled false on purpose). Every runtime credential
# therefore has to arrive through the derived 0600 secrets.env. "Can we
# resolve it?" and "will the GATEWAY see it?" are two different facts and are
# checked separately — conflating them is what hid the Anthropic failure.
SECRETS_JSON="$(python3 "$HERMES_HOME/scripts/platform/secrets_runtime.py" status --json 2>/dev/null || true)"
if [[ -z "$SECRETS_JSON" ]]; then
  fail "runtime secrets status unavailable — secrets_runtime.py not deployed; run: sudo bash platform/bootstrap.sh"
else
  sec_get() { printf '%s' "$SECRETS_JSON" | python3 -c "
import json,sys
try: v=json.load(sys.stdin).get(sys.argv[1])
except Exception: v=None
print(','.join(map(str,v)) if isinstance(v,list) else ('' if v is None else v))" "$1"; }

  # Per-key resolution, presence only — never a value.
  while IFS='|' read -r K REQ AVAIL INRT ERR; do
    [[ -n "$K" ]] || continue
    if [[ "$AVAIL" == "True" && "$INRT" == "True" ]]; then
      pass "$K resolves and is in the gateway runtime env"
    elif [[ "$AVAIL" == "True" && "$REQ" == "True" ]]; then
      fail "$K resolves but is NOT in the runtime env — the gateway will not see it; run: sudo nicks-stack-secrets render"
    elif [[ "$REQ" == "True" ]]; then
      fail "$K is REQUIRED and does not resolve — $ERR"
    else
      note "$K optional and absent"
    fi
  done < <(printf '%s' "$SECRETS_JSON" | python3 -c "
import json,sys
for k in json.load(sys.stdin).get('keys',[]):
    print('|'.join([k['key'],str(k['required']),str(k['available']),str(k['in_runtime_env']),k['error']]))" 2>/dev/null)

  [[ "$(sec_get gateway_ready)" == "True" ]] \
    && pass "gateway will start with every required credential present" \
    || fail "gateway would start WITHOUT a required credential — run: sudo jack secrets status"

  SEC_PATH="$(sec_get path)"
  if [[ -f "$SEC_PATH" ]]; then
    [[ "$(sec_get mode_ok)" == "True" ]] \
      && pass "$SEC_PATH is mode 0600" \
      || fail "$SEC_PATH is mode $(sec_get mode), expected 0600 — it holds secrets"
  fi

  # The derived secret file must never be tracked by git.
  if [[ -d "$STACK_ROOT/.git" ]]; then
    if git -C "$STACK_ROOT" ls-files --error-unmatch "$SEC_PATH" >/dev/null 2>&1; then
      fail "$SEC_PATH is TRACKED BY GIT — a secret-bearing file is committed"
    else
      pass "the derived runtime secret file is not tracked by git"
    fi
  fi

  # Plaintext op:// value caches must not persist.
  PLAINTEXT_CACHES=()
  while IFS= read -r c; do [[ -f "$c" ]] && PLAINTEXT_CACHES+=("$c"); done < <(python3 -c "
import sys; sys.path.insert(0,'$HERMES_HOME/scripts/platform')
import lib
for p in lib.runtime_secrets_spec()['purge']: print(p)" 2>/dev/null)
  if ((${#PLAINTEXT_CACHES[@]})); then
    fail "plaintext op:// value cache(s) present: ${PLAINTEXT_CACHES[*]} — run: sudo nicks-stack-secrets render"
  else
    pass "no plaintext op:// value cache persists provider credentials"
  fi
fi

section "8b. Capability routing (Composio-backed Google + Notion)"
# ==========================================================================
# Architectural decision: this agent never uses a Hermes-native Google
# integration. Gmail, Calendar, Drive, Contacts and Notion all go through
# Composio; AgentMail stays installed but is reserved for the business agents.
# platform.yaml declares it; this proves the machine still matches. Names and
# booleans only — no secret value is read.
CAP_JSON="$(python3 "$PLATFORM_LIB" capabilities 2>/dev/null || true)"
if [[ -z "$CAP_JSON" ]]; then
  warn "capability routing could not be evaluated"
else
  cap_bool() { printf '%s' "$CAP_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p" | head -1; }

  if [[ "$(cap_bool composio_wired)" == "true" ]]; then
    pass "composio MCP server configured — the backing for every declared capability"
  else
    # The entry is runtime-managed: absent means the session is not established
    # (or was torn down), not that the platform is mis-wired. The session checks
    # immediately below give the actual reason.
    fail "composio MCP entry not active — Gmail/Calendar/Drive/Contacts/Notion unavailable; see the session checks below"
  fi
  # ── Composio Sessions: the five hardening checks (v1.1.1) ─────────────
  # `--offline` keeps verify fast and network-free for everything except the
  # session-validity probe, which is the one check that needs a live call.
  # BEHAVIOURAL INVARIANT: the installed launcher must execute. We do not
  # assert how it is installed (symlink/copy/wrapper) — only that running it
  # works, which is what a broken import path or interpreter actually breaks.
  # The invariant is "it produced parseable JSON", not "it exited 0": status
  # exits 1 whenever Composio is unconfigured, which is a legitimate state and
  # is reported separately below. Only a launcher that cannot RUN fails here.
  if [[ -e "$PREFIX_BIN/nicks-stack-composio-session" ]]; then
    COMPOSIO_LAUNCHER_OUT="$("$PREFIX_BIN/nicks-stack-composio-session" status --offline --json 2>&1 || true)"
    if printf '%s' "$COMPOSIO_LAUNCHER_OUT" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
      pass "nicks-stack-composio-session executes (status --offline)"
    else
      fail "nicks-stack-composio-session FAILED to execute: $(printf '%s' "$COMPOSIO_LAUNCHER_OUT" | tail -1)"
    fi
  else
    warn "nicks-stack-composio-session is not installed"
  fi

  COMPOSIO_JSON="$(python3 "$HERMES_HOME/scripts/platform/composio_session.py" status --json 2>/dev/null || true)"
  cs_get() { printf '%s' "$COMPOSIO_JSON" | python3 -c "
import json,sys
try: v=json.load(sys.stdin).get(sys.argv[1])
except Exception: v=None
print(','.join(v) if isinstance(v,list) else ('' if v is None else v))" "$1"; }

  # 1. missing Project API key — the SAME real resolution the session manager
  #    uses (composio_session.resolve_credential), not presence-by-declaration.
  #    lib.key_presence() would say "present" merely because an op:// reference
  #    is mapped, even when the vault field does not exist.
  if [[ -n "$COMPOSIO_JSON" && "$(cs_get api_key_available)" == "True" ]]; then
    pass "COMPOSIO_API_KEY resolves (source: $(cs_get api_key_source))"
  elif [[ -n "$COMPOSIO_JSON" ]]; then
    fail "COMPOSIO_API_KEY not resolvable — $(cs_get api_key_error)"
  else
    fail "COMPOSIO_API_KEY status unavailable — composio_session.py not deployed"
  fi

  # 1b. the gateway's own copy: config.yaml expands ${COMPOSIO_API_KEY} from the
  #     gateway environment, which is fed only by the derived runtime env.
  if [[ -n "$COMPOSIO_JSON" ]]; then
    if [[ "$(cs_get gateway_key_present)" == "True" ]]; then
      pass "gateway will resolve \${COMPOSIO_API_KEY} from the derived runtime env"
    elif [[ "$(cs_get config_entry_active)" == "True" ]]; then
      fail "composio config entry is active but the runtime env carries no COMPOSIO_API_KEY — the x-api-key header would be EMPTY (silent 401)"
    else
      note "no gateway COMPOSIO_API_KEY (composio not active — consistent)"
    fi
  fi

  if [[ -z "$COMPOSIO_JSON" ]]; then
    warn "composio session status unavailable (composio_session.py not deployed?)"
  else
    # Two distinct facts, reported separately: the SDK is installed in the venv
    # (the acceptance test), and the launcher's own interpreter can import it.
    COMPOSIO_VENV_PY="/opt/nicks-stack/composio-venv/bin/python"
    if [[ -x "$COMPOSIO_VENV_PY" ]] && "$COMPOSIO_VENV_PY" -c "import composio" 2>/dev/null; then
      pass "composio SDK installed in its venv ($COMPOSIO_VENV_PY -c 'import composio')"
    else
      fail "composio SDK NOT installed in /opt/nicks-stack/composio-venv — run: sudo bash platform/bootstrap.sh"
    fi
    [[ "$(cs_get sdk_importable)" == "True" ]] \
      && pass "composio SDK importable by the launcher" \
      || fail "composio SDK not importable by the launcher — run: sudo bash platform/bootstrap.sh"
    # Import ORDER: the venv must win over /usr/lib/python3/dist-packages.
    if "$PREFIX_BIN/nicks-stack-composio-session" deps >/dev/null 2>&1; then
      pass "composio dependencies resolve from the venv (typing_extensions.Sentinel present)"
    else
      fail "a system package is shadowing a Composio dependency — run: sudo nicks-stack-composio-session deps"
    fi

    # 2. session invalid
    CS_VALID="$(cs_get session_validity)"
    case "$CS_VALID" in
      valid)   pass "composio session $(cs_get session_id) is valid (user $(cs_get user_id))" ;;
      absent)  warn "no composio session yet — run: sudo nicks-stack-composio-session init" ;;
      invalid) fail "composio session INVALID: $(cs_get session_validity_detail)" ;;
      *)       warn "composio session validity unknown ($(cs_get session_validity_detail))" ;;
    esac

    # 3. empty MCP URL / runtime config coherence
    CS_URL="$(cs_get mcp_url_present)"; CS_CFG="$(cs_get config_entry_active)"
    if [[ "$CS_URL" == "True" && "$CS_CFG" == "True" ]]; then
      pass "composio MCP url present and the config entry is active (header: $(cs_get header_type))"
    elif [[ "$CS_URL" == "False" && "$CS_CFG" == "False" ]]; then
      note "composio not configured — no MCP url and no config entry (clean absence, not an error)"
    else
      fail "composio runtime INCOHERENT: url_present=$CS_URL config_entry=$CS_CFG — one without the other"
    fi
    if grep -qE '^\s*url:\s*(""|'"''"'|$)' "$HERMES_HOME/config.yaml" 2>/dev/null; then
      fail "config.yaml contains an MCP server with an EMPTY url"
    else
      pass "no MCP server in config.yaml has an empty url"
    fi

    # 4. stale mcp.env
    if [[ "$(cs_get stale_runtime)" == "True" ]]; then
      fail "STALE composio runtime — mcp.env/config point at a session that no longer resolves. Run: sudo nicks-stack-composio-session init"
    else
      pass "no stale composio runtime"
    fi

    # 5. toolkit scope mismatch. A DEFERRED toolkit — one platform.yaml parked
    #    in composio.auth_configs with an empty id because Composio has no
    #    managed OAuth app for it — is a declared narrowing, so it is reported
    #    and not failed. Anything declared that is neither in the session nor
    #    deferred is still a hard mismatch.
    CS_MISSING="$(cs_get toolkits_missing)"
    CS_DEFERRED="$(cs_get toolkits_deferred)"
    if [[ -n "$CS_MISSING" ]]; then
      fail "composio toolkit scope MISMATCH — declared but not in the session: $CS_MISSING"
    else
      TK="$(cs_get toolkits_resolved)"
      if [[ -n "$TK" ]]; then
        pass "composio toolkit scope matches the declaration: $TK"
      else
        note "no composio toolkits scoped yet"
      fi
    fi
    if [[ -n "$CS_DEFERRED" ]]; then
      note "composio toolkits deferred pending an auth config (declared, not a failure): $CS_DEFERRED"
    fi
    if [[ "$(cs_get scope_drift)" == "True" ]]; then
      note "composio declared scope differs from the live session — the next init re-mints it"
    fi
    note "composio last verified: $(cs_get last_verified)"
  fi

  # The legacy wiring must not come back.
  if grep -q "connect.composio.dev\|x-consumer-api-key" "$HERMES_HOME/config.yaml" 2>/dev/null; then
    fail "config.yaml still contains the legacy Composio endpoint or x-consumer-api-key header"
  else
    pass "legacy Composio wiring absent (no connect.composio.dev, no x-consumer-api-key)"
  fi

  # A forbidden implementation reappearing is a critical regression, not a nit.
  CAP_VIOLATIONS="$(printf '%s' "$CAP_JSON" \
    | sed -n '/"violations"/,/\]/p' \
    | sed -n 's/^[[:space:]]*"\(.*\)",\?$/\1/p')"
  if [[ -z "$CAP_VIOLATIONS" ]]; then
    pass "no Hermes-native Google/Notion implementation present"
  else
    while IFS= read -r v; do
      [[ -n "$v" ]] && fail "capability routing: $v"
    done <<< "$CAP_VIOLATIONS"
  fi

  # AgentMail must stay installed (the business agents need it) and stay
  # reserved (Jack must not reach for it).
  if grep -q "agentmail" "$HERMES_HOME/config.yaml" 2>/dev/null; then
    note "AgentMail MCP still installed — reserved for the autonomous business agents"
  else
    warn "AgentMail MCP is absent — it is meant to remain installed for future business agents"
  fi
  if grep -q "reserved" "$HERMES_HOME/platform.yaml" 2>/dev/null; then
    pass "AgentMail declared reserved in platform.yaml"
  else
    warn "platform.yaml does not declare AgentMail reserved"
  fi
  if grep -qi "Composio" "$HERMES_HOME/SOUL.md" 2>/dev/null; then
    pass "SOUL.md carries the capability-routing rule into the agent prompt"
  else
    fail "SOUL.md does not mention Composio — the agent has not been told the routing rule"
  fi
fi

# ==========================================================================
section "8c. TK Family document processing (v1.1.25)"
# ==========================================================================
# Jack reads, renames and files the Covey family's scanned documents. Three
# things can break this silently, so all three are checked here:
#
#   1. the folder table drifting between platform.yaml (what the scan script
#      and this file read) and the skill (what the agent reads). A file filed
#      by a stale id lands somewhere real and wrong, with no error anywhere.
#   2. TK_FAMILY_TOKEN not reaching the gateway, which makes the MCP server
#      present-but-401 rather than absent.
#   3. the every-minute cron job missing, which is completely quiet: no error,
#      no delivery, documents simply never get read.
#
# Presence and equality only. No token value is read and no document is touched.
TK_SKILL="$HERMES_HOME/skills/productivity/tk-family-documents/SKILL.md"
TK_SCAN="$HERMES_HOME/scripts/platform/tkfamily_scan.py"

TK_JSON="$(python3 - "$HERMES_HOME/platform.yaml" "$TK_SKILL" <<'PYEOF' 2>/dev/null || true
import json, re, sys, pathlib
try:
    import yaml
    spec = (yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}).get("tk_family") or {}
except Exception:
    print(json.dumps({"ok": False, "reason": "platform.yaml tk_family unreadable"})); raise SystemExit
folders = spec.get("folders") or {}
inboxes = spec.get("inboxes") or {}
out = {"ok": True, "enabled": bool(spec.get("enabled")), "folders": len(folders),
       "inboxes": len(inboxes), "account": bool(spec.get("drive_account")),
       "job": spec.get("cron_job_name") or "", "skill": "missing", "match": None}
try:
    text = pathlib.Path(sys.argv[2]).read_text()
    out["skill"] = "present"
    m = re.search(r'```json\n(\{\n  "0 Inbox".*?\n\})\n```', text, re.S)
    out["match"] = bool(m) and json.loads(m.group(1)) == folders
except OSError:
    pass
# Every inbox must also be a legal folder, or the scan watches a path the
# agent is forbidden to file out of.
out["inboxes_known"] = all(k in folders and folders[k] == v for k, v in inboxes.items())
print(json.dumps(out))
PYEOF
)"

tk_get() { printf '%s' "$TK_JSON" | python3 -c "
import json,sys
try: v=json.load(sys.stdin).get(sys.argv[1])
except Exception: v=None
print('' if v is None else v)" "$1"; }

if [[ -z "$TK_JSON" || "$(tk_get ok)" != "True" ]]; then
  warn "tk_family block not declared in platform.yaml — document processing is not configured"
else
  if [[ "$(tk_get folders)" -ge 40 && "$(tk_get inboxes)" == "3" ]]; then
    pass "tk_family folder table declared ($(tk_get folders) destinations, $(tk_get inboxes) drop zones)"
  else
    fail "tk_family folder table looks wrong ($(tk_get folders) destinations, $(tk_get inboxes) drop zones)"
  fi

  if [[ "$(tk_get inboxes_known)" == "True" ]]; then
    pass "every drop zone is also a declared folder id"
  else
    fail "a tk_family inbox id disagrees with the folders table — the scan would watch a path the agent cannot file out of"
  fi

  if [[ "$(tk_get account)" == "True" ]]; then
    pass "tk_family.drive_account pinned — Drive calls will not fall back to the non-owner default"
  else
    warn "tk_family.drive_account is unset — Composio would use the default Drive account, which does NOT own Covey Files"
  fi

  case "$(tk_get skill)/$(tk_get match)" in
    present/True)  pass "tk-family-documents skill installed, folder table matches platform.yaml" ;;
    present/False) fail "tk-family-documents folder table DRIFTED from platform.yaml — the agent would file by stale ids" ;;
    present/*)     warn "tk-family-documents skill installed but its folder table could not be parsed" ;;
    *)             warn "tk-family-documents skill not installed — the agent has no filing instructions" ;;
  esac
fi

if [[ -f "$TK_SCAN" ]]; then
  if python3 "$TK_SCAN" --status >/dev/null 2>&1; then
    pass "tkfamily_scan.py installed and runs"
  else
    warn "tkfamily_scan.py installed but --status did not succeed (TK Family or Composio may be unreachable)"
  fi
else
  warn "tkfamily_scan.py not installed — nothing polls for new documents"
fi

# The MCP server itself: enabled, and pointing its header at the runtime secret
# rather than an inline token.
if python3 - "$HERMES_HOME/config.yaml" <<'PYEOF' 2>/dev/null
import sys, pathlib, yaml
d = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
s = ((d.get("mcp_servers") or {}).get("tkfamily") or {})
hdr = (s.get("headers") or {}).get("x-mcp-token", "")
sys.exit(0 if s.get("enabled") and hdr == "${TK_FAMILY_TOKEN}" else 1)
PYEOF
then
  pass "tkfamily MCP server enabled, token read from \${TK_FAMILY_TOKEN} (never inline)"
else
  warn "tkfamily MCP server is absent, disabled, or does not source its token from \${TK_FAMILY_TOKEN}"
fi

# The keepalive default of 180s PARKS this server (v1.1.24). It is stateless,
# so the ping fails every interval, the session never proves healthy, and after
# five reconnects all 23 tools are deregistered for 300s. A parked server is
# silent: Jack sees an empty tool list and reports no documents.
TK_KA="$(python3 - "$HERMES_HOME/config.yaml" <<'PYEOF' 2>/dev/null || true
import sys, pathlib, yaml
d = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
print(((d.get("mcp_servers") or {}).get("tkfamily") or {}).get("keepalive_interval") or 0)
PYEOF
)"
if [[ -n "$TK_KA" ]] && (( ${TK_KA%%.*} >= 900 )); then
  pass "tkfamily keepalive_interval ${TK_KA}s — no spurious pings against a stateless endpoint"
else
  fail "tkfamily keepalive_interval is ${TK_KA:-unset} — at the 180s default this server gets PARKED and its tools silently vanish"
fi

# And the live proof: parks in the last hour of agent log.
if [[ -f "$HERMES_HOME/logs/agent.log" ]]; then
  TK_PARKS="$(grep -c "tkfamily.*parking" "$HERMES_HOME/logs/agent.log" 2>/dev/null || echo 0)"
  if [[ "${TK_PARKS:-0}" -eq 0 ]]; then
    pass "tkfamily has never been parked in this log"
  else
    note "tkfamily was parked ${TK_PARKS} time(s) historically — expected if the log predates the keepalive fix"
  fi
fi

# Jack has the full tool surface by Taylor's decision (v1.1.24), so the thing
# that keeps him from inventing records is no longer the tool list. It is the
# five guards, and they live in two files that have to agree: platform.yaml
# declares them and the skill is where they actually bind. Check both, because
# a guard declared and not taught is worse than one that was never claimed.
TK_GUARDS="$(python3 - "$HERMES_HOME/platform.yaml" "$TK_SKILL" <<'PYEOF' 2>/dev/null || true
import sys, pathlib, yaml, json
try:
    spec = (yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}).get("tk_family") or {}
except Exception:
    print("unreadable"); raise SystemExit
wa = spec.get("write_authority") or {}
g = wa.get("guards") or {}
want = ["provenance", "search_before_create", "verify_after_write", "quote_dont_infer"]
missing = [k for k in want if not g.get(k)]
floor = g.get("confidence_floor")
denied = " ".join(str(x).lower() for x in (wa.get("denied") or []))
# The skill has to actually teach the two guards that are pure behaviour: a
# declared verify-after-write nobody was told about protects nothing.
try:
    skill = pathlib.Path(sys.argv[2]).read_text().lower()
except OSError:
    skill = ""
taught = ("read it back" in skill or "read the file back" in skill) and "provenance" in skill
print(json.dumps({"missing": missing, "floor": floor,
                  "denies_permissions": "permission" in denied or "sharing" in denied,
                  "denies_hard_delete": "permanently delete" in denied,
                  "denies_folders": "create a folder" in denied,
                  "taught": taught}))
PYEOF
)"
if [[ -z "$TK_GUARDS" || "$TK_GUARDS" == "unreadable" ]]; then
  warn "tk_family.write_authority is not declared — nothing bounds what Jack may do to the family's files"
else
  tg() { printf '%s' "$TK_GUARDS" | python3 -c "
import json,sys
try: v=json.load(sys.stdin).get(sys.argv[1])
except Exception: v=None
print(','.join(map(str,v)) if isinstance(v,list) else ('' if v is None else v))" "$1"; }

  if [[ -z "$(tg missing)" ]]; then
    pass "all four fabrication guards declared (provenance, search-before-create, verify-after-write, quote-dont-infer)"
  else
    fail "fabrication guard(s) NOT declared: $(tg missing) — Jack has full write access with nothing holding it"
  fi

  if [[ "$(tg taught)" == "True" ]]; then
    pass "the skill teaches provenance and read-back, so the guards are more than a yaml claim"
  else
    fail "tk_family guards are declared in platform.yaml but the skill does not teach them"
  fi

  FLOOR="$(tg floor)"
  if [[ -n "$FLOOR" ]]; then
    pass "confidence floor $FLOOR — below this a document waits for a person"
  else
    warn "no tk_family confidence floor declared — Jack has no stated point at which to stop guessing"
  fi

  # The two irreversible ones, plus Taylor's own folder rule.
  if [[ "$(tg denies_permissions)" == "True" && "$(tg denies_hard_delete)" == "True" ]]; then
    pass "sharing/permission changes and permanent deletion are both denied"
  else
    fail "the irreversible operations are not denied — Jack could widen access to family records or delete beyond recovery"
  fi
  [[ "$(tg denies_folders)" == "True" ]] \
    && pass "folder creation denied — the tree cannot grow a '2027' on its own" \
    || warn "folder creation is not denied; the folder table stops being the source of truth"
fi

# The routing memory (v1.1.25). It is Taylor's data, it grows from his
# corrections, and it must survive `hermes update` — so it lives outside the
# repo and outside the files/ tree that bootstrap syncs. Check it is readable
# and that every rule points at a folder that actually exists: a rule aimed at
# a path that has been renamed away would file documents into nowhere.
TK_ROUTE="$(python3 - "$HERMES_HOME/platform.yaml" <<'PYEOF' 2>/dev/null || true
import sys, json, pathlib, yaml
spec = (yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}).get("tk_family") or {}
folders = spec.get("folders") or {}
path = pathlib.Path(spec.get("routing_file") or "")
if not path:
    print(json.dumps({"state": "undeclared"})); raise SystemExit
if not path.exists():
    print(json.dumps({"state": "absent", "path": str(path)})); raise SystemExit
try:
    d = json.loads(path.read_text())
except Exception:
    print(json.dumps({"state": "corrupt", "path": str(path)})); raise SystemExit
rules, bad = 0, []
for kind in ("vendors", "addresses", "accounts"):
    for value, dest in (d.get(kind) or {}).items():
        rules += 1
        if dest not in folders:
            bad.append("%s=%s" % (value, dest))
print(json.dumps({"state": "ok", "rules": rules, "bad": bad,
                  "corrections": len(d.get("corrections") or []),
                  "mode": oct(path.stat().st_mode & 0o777)}))
PYEOF
)"
TR_STATE="$(printf '%s' "$TK_ROUTE" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('state',''))
except Exception: print('')" 2>/dev/null)"
case "$TR_STATE" in
  ok)
    TR_BAD="$(printf '%s' "$TK_ROUTE" | python3 -c "
import json,sys; print(','.join(json.load(sys.stdin).get('bad') or []))" 2>/dev/null)"
    TR_N="$(printf '%s' "$TK_ROUTE" | python3 -c "
import json,sys; d=json.load(sys.stdin); print('%s rule(s) from %s correction(s)' % (d['rules'], d['corrections']))" 2>/dev/null)"
    if [[ -z "$TR_BAD" ]]; then
      pass "routing memory readable, $TR_N, every rule points at a real folder"
    else
      fail "routing memory has rule(s) pointing at folders that do not exist: $TR_BAD"
    fi
    ;;
  absent)  warn "routing memory file has not been created yet — Jack will ask about every ambiguous document and learn nothing" ;;
  corrupt) fail "routing memory file is not valid JSON — Jack cannot read Taylor's filing corrections" ;;
  *)       warn "tk_family.routing_file is not declared — corrections cannot be remembered" ;;
esac

# Presence of the credential, via the one canonical resolver. Never its value.
# Section 8a already walks every declared key, but it reports an optional
# missing key as a quiet note. With the server ENABLED, a missing token is not
# quiet: Hermes connects and every call comes back 401, which reads to the
# agent as "the household has no documents" rather than as a broken credential.
if printf '%s' "$SECRETS_JSON" | python3 -c "
import json,sys
k=[x for x in json.load(sys.stdin).get('keys',[]) if x.get('key')=='TK_FAMILY_TOKEN']
sys.exit(0 if k and k[0].get('available') and k[0].get('in_runtime_env') else 1)" 2>/dev/null; then
  pass "TK_FAMILY_TOKEN resolves and is in the gateway runtime env"
else
  warn "TK_FAMILY_TOKEN is not in the gateway runtime env — the TK Family server will answer 401 to every call; run: sudo nicks-stack-secrets render"
fi

# The loop itself. A missing job is the quietest failure in this section.
TK_JOB="$(tk_get job)"
if [[ -n "$TK_JOB" ]]; then
  if hermes cron list 2>/dev/null | grep -q "$TK_JOB"; then
    pass "cron job '$TK_JOB' registered — documents are being polled"
  else
    warn "cron job '$TK_JOB' is not registered — captured documents will never be read"
  fi
fi

# ==========================================================================
section "8d. Agent memory headroom"
# ==========================================================================
# memory.memory_char_limit is enforced by TRUNCATION, with no error, no warning
# and no log line. A MEMORY.md over the cap does not fail; it quietly stops
# holding the oldest things Jack learned, and the first symptom is him not
# knowing something he was told last month. Found at 86% on 2026-08-09 because
# the people roster had been duplicated out of SOUL.md. That is the usual
# cause: it fills with reference material, not with live state.
MEMCHK="$HERMES_HOME/scripts/platform/memory_check.py"
if [[ -f "$MEMCHK" ]]; then
  MEM_JSON="$(python3 "$MEMCHK" --json 2>/dev/null || true)"
  if [[ -z "$MEM_JSON" ]]; then
    warn "memory headroom could not be evaluated"
  else
    while IFS='|' read -r LBL CHARS LIMIT PCT; do
      [[ -n "$LBL" ]] || continue
      if (( $(printf '%.0f' "$PCT") >= 95 )); then
        fail "$LBL is at ${PCT}% of its ${LIMIT} char cap ($CHARS) — Hermes truncates silently; prune to live state and move depth to Notion"
      elif (( $(printf '%.0f' "$PCT") >= 80 )); then
        warn "$LBL is at ${PCT}% of its ${LIMIT} char cap ($CHARS) — nothing lost yet, but it fills with reference material that belongs in Notion"
      else
        pass "$LBL at ${PCT}% of its ${LIMIT} char cap ($CHARS chars)"
      fi
    done < <(printf '%s' "$MEM_JSON" | python3 -c "
import json,sys
for e in json.load(sys.stdin).get('entries',[]):
    if e.get('exists'):
        print('|'.join([e['label'], str(e['chars']), str(e['limit']), '%.0f' % (e['pct']*100)]))" 2>/dev/null)
  fi
else
  warn "memory_check.py not installed — nothing watches the silent truncation cap"
fi

# ==========================================================================
section "9. Services"
# ==========================================================================
SUPERVISOR_UP=0
if have supervisorctl && supervisorctl status >/dev/null 2>&1; then
  SUPERVISOR_UP=1
  pass "supervisord is responding"
else
  fail "supervisord is not responding — no service in this stack is supervised"
fi

if ((SUPERVISOR_UP)); then
  for prog in hermes-gateway agentphone-bridge; do
    line="$(supervisorctl status "$prog" 2>/dev/null || true)"
    if [[ -z "$line" ]]; then
      fail "supervisor program not registered: $prog"
      continue
    fi
    note "supervisor: $line"
    case "$line" in
      *RUNNING*)  pass "$prog is RUNNING" ;;
      *STARTING*) warn "$prog is STARTING" ;;
      *FATAL*)    fail "$prog is FATAL — check its log under /var/log/orgo" ;;
      *)          warn "$prog is not running (state above) — the wrappers stay dormant until configured" ;;
    esac
  done
fi

check_advisory "autostart: onboarding"  test -f "$AUTOSTART_DIR/nicks-stack-onboard.desktop"
check_advisory "autostart: Obsidian"    test -f "$AUTOSTART_DIR/nicks-stack-obsidian.desktop"

# Bridge health endpoint — a plain GET, dormant until AgentPhone is keyed.
if env_key_present "$HERMES_HOME/.env" "AGENTPHONE_API_KEY" \
   && env_key_present "$HERMES_HOME/.env" "AGENTPHONE_AGENT_ID"; then
  if have curl && curl -fsS --max-time 5 "$BRIDGE_HEALTH_URL" >/dev/null 2>&1; then
    pass "AgentPhone bridge health endpoint responds"
  else
    warn "AgentPhone is keyed but $BRIDGE_HEALTH_URL does not respond yet"
  fi
else
  note "AgentPhone not keyed — bridge is intentionally dormant"
fi

# ==========================================================================
section "10. Local AI (Ollama)"
# ==========================================================================
# Ollama is optional: the stack runs fine without it. These checks are
# CRITICAL only when the machine is configured for local AI (a supervised
# ollama program exists) — otherwise they are advisory.
OLLAMA_MANAGED=0
if grep -qs '^\[program:ollama\]' "${SUPERVISOR_CONFD:-/etc/supervisor/conf.d}"/*.conf 2>/dev/null; then
  OLLAMA_MANAGED=1
fi

ollama_check() {
  # ollama_check <label> <ok 0|1> <detail>
  local label="$1" state="$2" detail="$3"
  if [[ "$state" == "1" ]]; then
    pass "$label${detail:+ — $detail}"
  elif ((OLLAMA_MANAGED)); then
    fail "$label${detail:+ — $detail}"
  else
    warn "$label${detail:+ — $detail}"
  fi
}

if ((OLLAMA_MANAGED)); then
  note "this machine is configured for local AI (supervised ollama program present)"
else
  note "local AI not configured — checks below are advisory (enable with: bootstrap.sh --with-ollama)"
fi

# 1. installed
if have ollama; then
  ollama_check "Ollama installed" 1 "$(ollama --version 2>&1 | head -1)"
else
  ollama_check "Ollama installed" 0 "binary not on PATH"
fi

# 2. serving (supervisor state — this platform uses Supervisor, not systemd)
if ((SUPERVISOR_UP)) && supervisorctl status ollama >/dev/null 2>&1; then
  OLLAMA_SUP_LINE="$(supervisorctl status ollama 2>/dev/null || true)"
  case "$OLLAMA_SUP_LINE" in
    *RUNNING*) ollama_check "Ollama serving (supervisor)" 1 "$(printf '%s' "$OLLAMA_SUP_LINE" | tr -s ' ')" ;;
    *)         ollama_check "Ollama serving (supervisor)" 0 "$(printf '%s' "$OLLAMA_SUP_LINE" | tr -s ' ')" ;;
  esac
elif ((OLLAMA_MANAGED)); then
  ollama_check "Ollama serving (supervisor)" 0 "program not registered — run: supervisorctl reread && supervisorctl update"
else
  note "Ollama not under supervisor on this machine"
fi

# 3. API reachable + 4. installed models — via the shared detection library,
# so this agrees with `jack doctor` and the router by construction.
OLLAMA_MODELS_LIST=""
OLLAMA_JSON="$(python3 "$PLATFORM_LIB" ollama 2>/dev/null || true)"
if [[ -n "$OLLAMA_JSON" ]] && printf '%s' "$OLLAMA_JSON" | grep -q '"serving": true'; then
  ollama_check "Ollama API reachable" 1 "$OLLAMA_URL"
  OLLAMA_MODELS_LIST="$(printf '%s' "$OLLAMA_JSON" | python3 -c "
import json,sys
try:
    print('\n'.join(json.load(sys.stdin).get('models') or []))
except Exception:
    pass")"
  OLLAMA_MODEL_COUNT="$(printf '%s' "$OLLAMA_MODELS_LIST" | grep -c . || true)"
  if [[ "${OLLAMA_MODEL_COUNT:-0}" -gt 0 ]]; then
    ollama_check "Ollama models installed" 1 "$OLLAMA_MODEL_COUNT: $(printf '%s' "$OLLAMA_MODELS_LIST" | tr '\n' ' ')"
  else
    ollama_check "Ollama models installed" 0 "none pulled — see DEPLOYMENT.md → Enabling local AI"
  fi
else
  ollama_check "Ollama API reachable" 0 "$OLLAMA_URL did not answer"
fi

# 5. + 6. the two models this platform expects. Never auto-pulled: a missing
# model is reported with the exact command, never fixed silently.
for want in qwen3 nomic-embed-text; do
  if printf '%s' "$OLLAMA_MODELS_LIST" | grep -q "^${want}"; then
    pass "model present: ${want} ($(printf '%s' "$OLLAMA_MODELS_LIST" | grep "^${want}" | tr '\n' ' '))"
  elif [[ -n "$OLLAMA_MODELS_LIST" ]]; then
    warn "model missing: ${want} — pull it with: ollama pull ${want}"
  else
    note "model ${want}: cannot check (no model list)"
  fi
done

# ==========================================================================
section "11. Onboarding state (informational)"
# ==========================================================================
if [[ -s "$HERMES_HOME/auth.json" ]]; then
  pass "model account connected (auth.json present)"
else
  warn "model account not connected — run $PREFIX_BIN/nicks-stack-onboard.sh (step 1/4)"
fi

if env_key_present "$HERMES_HOME/.env" "TELEGRAM_BOT_TOKEN"; then
  pass "Telegram bot paired"
  if env_key_present "$HERMES_HOME/.env" "TELEGRAM_ALLOWED_USERS"; then
    pass "Telegram allowlist configured"
  else
    warn "TELEGRAM_BOT_TOKEN set but TELEGRAM_ALLOWED_USERS is empty — nobody can talk to the bot"
  fi
else
  warn "Telegram not paired — run $PREFIX_BIN/nicks-stack-onboard.sh (step 2/4)"
fi

# ==========================================================================
section "12. User data inventory (must survive every update)"
# ==========================================================================
note "vault notes        : $(count_files "$VAULT_DIR") file(s) in $VAULT_DIR"
note "memories           : $(count_files "$HERMES_HOME/memories") file(s)"
note "state / sessions   : $(count_files "$HERMES_HOME/state") file(s)"
for extra in sessions logs; do
  if [[ -d "$HERMES_HOME/$extra" ]]; then
    note "$extra: $(count_files "$HERMES_HOME/$extra") file(s)"
  fi
done
if [[ -d "$STACK_ROOT/backups" ]]; then
  note "rollback points    : $(find "$STACK_ROOT/backups" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ') in $STACK_ROOT/backups"
else
  note "rollback points    : none yet ($STACK_ROOT/backups absent)"
fi

# ==========================================================================
# Summary
# ==========================================================================
printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_BLUE" "$C_RESET"
printf '%spassed: %s   advisories: %s   critical failures: %s%s\n' \
  "$C_BOLD" "$PASS_COUNT" "${#ADVISORY_WARNINGS[@]}" "${#CRITICAL_FAILURES[@]}" "$C_RESET"

if ((${#ADVISORY_WARNINGS[@]} > 0)); then
  printf '\n%sAdvisories (non-blocking):%s\n' "$C_YELLOW" "$C_RESET"
  for w in "${ADVISORY_WARNINGS[@]}"; do printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$w"; done
fi

if ((${#CRITICAL_FAILURES[@]} > 0)); then
  printf '\n%sCritical failures:%s\n' "$C_RED" "$C_RESET"
  for f in "${CRITICAL_FAILURES[@]}"; do printf '  %s✗%s %s\n' "$C_RED" "$C_RESET" "$f"; done
  printf '\n%sVERIFY FAILED%s — see platform/DEPLOYMENT.md → Troubleshooting\n' "$C_RED$C_BOLD" "$C_RESET"
  exit 1
fi

printf '\n%sVERIFY PASSED%s — the stack is deployed and healthy\n' "$C_GREEN$C_BOLD" "$C_RESET"
exit 0
