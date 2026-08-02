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
#   jack verify                                 full deployment verification
#
# Installed to /usr/local/bin/jack by platform/bootstrap.sh.
# ==========================================================================
set -Eeuo pipefail

readonly PLATFORM_SCRIPTS="/root/.hermes/scripts/platform"
readonly DOCTOR="${PLATFORM_SCRIPTS}/doctor.py"
readonly MANIFEST="${PLATFORM_SCRIPTS}/manifest.py"
readonly ROUTE="/usr/local/bin/nicks-stack-route"

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
