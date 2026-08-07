#!/usr/bin/env bash
# ==========================================================================
# Taylor AI Platform — `jack` command
# ==========================================================================
# The single entry point for platform operations. Thin on purpose: every
# subcommand delegates to the tool that owns that job, so there is one
# implementation of each behaviour and nothing to drift.
#
#   jack doctor [--providers|--json|--quiet]   platform health report
#   jack version                                platform + component versions
#   jack manifest [show|write|report]           machine-readable manifest
#   jack mode [show|set <mode>|run ...]         provider routing (nicks-stack-route)
#   jack profiles [--json|--rebuild]            runtime profiles (v1.0.3)
#   jack secrets [status|render|clean]          unified runtime secrets (v1.1.8)
#   jack composio [status|init|resolve]         Composio session state
#   jack verify                                 full deployment verification
#
# Installed to /usr/local/bin/jack by platform/bootstrap.sh.
# ==========================================================================
set -Eeuo pipefail

readonly PLATFORM_SCRIPTS="/root/.hermes/scripts/platform"
readonly DOCTOR="${PLATFORM_SCRIPTS}/doctor.py"
readonly MANIFEST="${PLATFORM_SCRIPTS}/manifest.py"
readonly ROUTE="/usr/local/bin/nicks-stack-route"
readonly RUNTIME_SECRETS="/root/.hermes/runtime/secrets.env"

# Runtime secrets (v1.1.9). gateway-run.sh sources the rendered file before it
# execs the gateway, so the GATEWAY sees every declared credential — but `jack`
# did not, so `jack doctor` inspected a shell that had none and reported
#
#   ! provider anthropic unavailable (credential: 1Password map (disabled))
#
# about a provider the running gateway was authenticating with perfectly well.
# That is the v1.1.8 bug wearing a different hat: a credential that exists but
# never reaches the process that needs it. The report is only worth trusting if
# it inspects the same environment the gateway runs in, so read the same file
# the gateway reads.
#
# Read-only on purpose — this does NOT render. Rendering is the gateway's job
# (and `jack secrets render`); if the file is absent, that IS the finding and
# doctor should report the providers as unavailable, which is now true of this
# shell too. Existing environment wins: an operator who exported a key by hand
# to test something keeps it.
if [[ -r "$RUNTIME_SECRETS" ]]; then
  while IFS= read -r _line; do
    [[ "$_line" =~ ^[[:space:]]*# ]] && continue
    [[ "$_line" =~ ^[[:space:]]*$ ]] && continue
    _k="${_line%%=*}"
    _k="${_k#export }"
    _k="${_k//[[:space:]]/}"
    [[ "$_k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!_k:-}" ]] && continue
    _v="${_line#*=}"
    _v="${_v%\"}"; _v="${_v#\"}"
    _v="${_v%\'}"; _v="${_v#\'}"
    export "$_k=$_v"
  done < "$RUNTIME_SECRETS"
  unset _line _k _v
fi

usage() {
  cat <<'USAGE'
jack — Taylor AI Platform

  jack doctor [--providers] [--json] [--quiet]
        One health report: Platform, Services, Providers, Integrations,
        Identity, Companies, Versions, Warnings.
        --providers adds one real inference per available provider.

  jack version
        Platform and component versions.

  jack manifest [show|write|report]
        show    print the stored manifest (default)
        write   regenerate it from the live machine
        report  print the deployment report

  jack mode [show|modes|set <mode>|run <mode> -q "..."]
        Provider routing. Passes through to nicks-stack-route.

  jack profiles [--json] [--rebuild]
        Runtime profiles: what a validation one-shot loads versus what the
        gateway loads. --rebuild regenerates the derived profile from the
        current config.yaml (it also rebuilds itself whenever that changes).

  jack secrets [status|render|clean] [--json]
        Unified runtime secrets: which declared credentials resolve, and
        whether the gateway will actually see them. Never prints a value.
        render rebuilds /root/.hermes/runtime/secrets.env (0600).

  jack composio [status|init|resolve] [--json]
        Live Composio session state: API key, session validity, user, MCP URL,
        header type, scoped toolkits, last verification. Never prints secrets.

  jack verify
        Full deployment verification (platform/verify.sh on the repo).

Exit codes: 0 healthy · 1 problem found · 2 cannot run.
USAGE
}

die() { printf 'jack: %s\n' "$*" >&2; exit 2; }

[[ -f "$DOCTOR" ]] || die "platform scripts not found at $PLATFORM_SCRIPTS — redeploy with platform/bootstrap.sh"

cmd="${1:-doctor}"
[[ $# -gt 0 ]] && shift || true

case "$cmd" in
  doctor)
    exec python3 "$DOCTOR" "$@"
    ;;
  version|--version|-v)
    exec python3 - "$@" <<'PY'
import json, subprocess, sys
sys.path.insert(0, "/root/.hermes/scripts/platform")
import lib
spec = lib.load_yaml(lib.PLATFORM_FILE)
versions = lib.versions_detect()
print(spec.get("name", "Taylor AI Platform"))
print()
print(f"v{versions.get('platform', 'unknown')}")
print()
for key in ("bootstrap", "update", "verify", "routing", "doctor", "manifest"):
    if key in versions:
        print(f"{key.capitalize()}")
        print()
        print(f"v{versions[key]}")
        print()
PY
    ;;
  profiles)
    exec python3 - "$@" <<'PROFILES'
import json, sys
sys.path.insert(0, "/root/.hermes/scripts/platform")
import lib

args = sys.argv[1:]
if "--rebuild" in args:
    for name, spec in lib.profiles_spec().items():
        if name == "use" or not isinstance(spec, dict) or spec.get("kind") == "full":
            continue
        res = lib.profile_ensure(name, force=True)
        print(f"{name}: {res['reason']}" + (f"  -> {res['path']}" if res["path"] else ""))
    print()

report = lib.profile_report()
if "--json" in args:
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(0)

full = report.get("full_runtime") or {}
use = report.get("use") or {}
print("Runtime profiles\n")
print(f"  {'profile':<12} {'mcp':>4} {'plugins':>8} {'op refs':>8}   state")
for name, info in (report.get("profiles") or {}).items():
    if info.get("kind") == "full":
        state = "full runtime"
    elif not info.get("built"):
        state = "not built yet"
    elif info.get("isolated"):
        state = f"isolated via {info.get('selection_layout')}"
    elif info.get("isolated") is False:
        state = "NOT ISOLATED - hermes ignores it"
    else:
        state = "built, isolation not measured"
    print(f"  {name:<12} {info.get('mcp_servers', 0):>4} {info.get('plugins', 0):>8} "
          f"{info.get('op_references', 0):>8}   {state}")
print()
for kind, name in use.items():
    print(f"  {kind:<12} -> {name}")

lean_name = use.get("validation")
lean = (report.get("profiles") or {}).get(lean_name) or {}
if lean and lean.get("kind") != "full":
    print(f"\n  '{lean_name}' drops "
          f"{full.get('mcp_servers', 0) - lean.get('mcp_servers', 0)} MCP server(s), "
          f"{full.get('plugins', 0) - lean.get('plugins', 0)} plugin(s) and "
          f"{full.get('op_references', 0) - lean.get('op_references', 0)} op:// reference(s)")
    print(f"  provider plugins kept : {', '.join(lean.get('plugin_names') or []) or 'none'}")
    print(f"  credentials kept      : {', '.join(lean.get('op_reference_names') or []) or 'none'}")
    print(f"  path                  : {lean.get('path')}")
    if lean.get("isolated") is False:
        print(f"\n  WARNING: hermes does not honour this profile "
              f"({lean.get('isolation_detail')}).")
        print("  Probes run on the full runtime. Attempts:")
        for att in lean.get("isolation_attempts") or []:
            print(f"    {att.get('layout'):<12} refs={att.get('refs')}  "
                  f"{(att.get('detail') or '')[:60]}")
PROFILES
    ;;
  secrets)
    SECRETS="${PLATFORM_SCRIPTS}/secrets_runtime.py"
    [[ -f "$SECRETS" ]] || die "secrets_runtime.py not installed — redeploy with platform/bootstrap.sh"
    sub="${1:-status}"
    [[ $# -gt 0 ]] && shift || true
    exec python3 "$SECRETS" "$sub" "$@"
    ;;
  composio)
    COMPOSIO="${PLATFORM_SCRIPTS}/composio_session.py"
    [[ -f "$COMPOSIO" ]] || die "composio_session.py not installed — redeploy with platform/bootstrap.sh"
    sub="${1:-status}"
    [[ $# -gt 0 ]] && shift || true
    exec python3 "$COMPOSIO" "$sub" "$@"
    ;;
  manifest)
    sub="${1:-show}"
    [[ $# -gt 0 ]] && shift || true
    exec python3 "$MANIFEST" "$sub" "$@"
    ;;
  mode|route|routing)
    [[ -x "$ROUTE" ]] || die "routing CLI not installed at $ROUTE"
    exec "$ROUTE" "${@:-show}"
    ;;
  verify)
    # Discover the checkout instead of assuming a path.  (v1.0.1 bug 2)
    REPO="$(python3 -c "
import sys; sys.path.insert(0, '$PLATFORM_SCRIPTS')
import lib
root = lib.find_repo_root()
print(root or '')" 2>/dev/null)"
    if [[ -n "$REPO" && -f "$REPO/platform/verify.sh" ]]; then
      exec bash "$REPO/platform/verify.sh" "$@"
    fi
    die "no nicks-stack checkout found (looked in /opt/nicks-stack, /root/nicks-stack, \$HOME/nicks-stack, \$NICKS_STACK_REPO)"
    ;;
  help|--help|-h)
    usage
    ;;
  *)
    usage >&2
    die "unknown command: $cmd"
    ;;
esac
