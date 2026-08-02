#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — jack doctor
# ==========================================================================
# THE platform health command. One report, eight sections:
#
#   Platform · Services · Providers · Integrations · Identity · Companies
#   · Versions · Warnings
#
# Everything it reports comes from the shared detection library
# (scripts/platform/lib.py), the same code bootstrap.sh, update.sh, verify.sh
# and the routing CLI use — so no two tools can disagree about the machine.
#
#   jack doctor                  full report (no billable calls)
#   jack doctor --providers      + one real inference per cloud provider
#   jack doctor --json           machine-readable
#   jack doctor --quiet          warnings and the verdict only
#
# By default the doctor is FREE and FAST: it inspects configuration, services
# and local state, and makes no paid API call. --providers adds the end-to-end
# credential -> catalog -> inference validation.
#
# SECRET SAFETY: credentials are reported as present/absent with their source.
# No value is ever printed, and vendor errors are scrubbed before display.
#
# EXIT CODES
#   0  healthy (warnings allowed)
#   1  something critical is wrong
#   2  could not run
# ==========================================================================
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import manifest as manifest_mod  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "provider-routing"))

PROBE_PROMPT = "Reply with exactly: ok"


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
class Out:
    def __init__(self, quiet: bool = False, color: bool = True):
        self.quiet = quiet
        self.color = color and sys.stdout.isatty()
        self.warnings: list[str] = []
        self.criticals: list[str] = []

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def section(self, title: str) -> None:
        if self.quiet:
            return
        print()
        print(self._c("1;36", title))
        print(self._c("2", "─" * max(len(title), 24)))

    def line(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def item(self, label: str, value: str, state: str = "info") -> None:
        mark = {"ok": ("✓", "1;32"), "warn": ("!", "1;33"),
                "bad": ("✗", "1;31"), "info": ("·", "2")}[state]
        if state == "warn":
            self.warnings.append(f"{label}: {value}")
        if state == "bad":
            self.criticals.append(f"{label}: {value}")
        if self.quiet:
            return
        print(f"  {self._c(mark[1], mark[0])} {label:<26} {value}")


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def section_platform(out: Out, state: dict) -> None:
    p = state["platform"]
    out.section("Platform")
    out.item("name", p["name"], "info")
    out.item("version", p["version"], "ok" if p["version"] != "unknown" else "bad")
    out.item("host", f"{p['hostname']}  ({p['os']})", "info")
    out.item("init system", f"PID 1 is '{p['init_system']}' — services run under Supervisor", "info")
    git = state["git"]
    dirty = "  (uncommitted changes)" if git.get("dirty") else ""
    out.item("git", f"{git.get('short', '?')} on {git.get('branch', '?')}{dirty}",
             "warn" if git.get("dirty") else "info")
    man = manifest_mod.read(lib.MANIFEST_FILE)
    if man:
        out.item("manifest", f"{lib.MANIFEST_FILE}  (deployed {man.get('deployed_at', '?')})", "ok")
    else:
        out.item("manifest", f"missing — run: jack manifest write", "warn")


def section_services(out: Out, state: dict) -> None:
    svc = state["services"]
    out.section("Services")
    if not svc["supervisor_available"]:
        out.item("supervisor", "supervisorctl not installed", "bad")
        return
    if not svc["supervisor_responding"]:
        out.item("supervisor", "not responding — no service is supervised", "bad")
        return
    out.item("supervisor", "responding", "ok")
    for name, info in svc["programs"].items():
        if not info["configured"]:
            out.item(name, "not configured on this machine", "info")
        elif info["state"] == "RUNNING":
            out.item(name, f"RUNNING  {info.get('detail', '')}".strip(), "ok")
        elif info["state"] in ("STARTING", "BACKOFF"):
            out.item(name, info["state"], "warn")
        else:
            # The gateway and bridge stay dormant by design until configured.
            state_txt = f"{info['state']}  {info.get('detail', '')}".strip()
            out.item(name, state_txt, "warn")


def section_providers(out: Out, state: dict, probe: bool, timeout: int) -> None:
    out.section("Providers")
    providers = state["providers"]
    if not providers:
        out.item("routing map", "no providers declared", "bad")
        return
    for name, info in providers.items():
        label = info["display"]
        if not info["enabled"]:
            out.item(label, "disabled in routing.yaml", "info")
            continue
        if not info["wired"]:
            out.item(label, f"{info['provider']} is not an enabled plugin or custom provider", "bad")
            continue
        if info["available"]:
            extra = info.get("detail") or ""
            cred = info["credential"]["source"]
            out.item(label, f"{info['provider']}  (credential: {cred}) {extra}".strip(), "ok")
        else:
            out.item(label, info.get("detail") or "unavailable", "warn")

    if probe:
        out.line()
        out.line("  live inference (one minimal call per provider):")
        for name, info in providers.items():
            if not info["enabled"] or not info["available"]:
                continue
            ok, detail = _probe_provider(name, info, state, timeout)
            out.item(f"  {info['display']} inference", detail, "ok" if ok else "bad")


def _probe_provider(name: str, info: dict, state: dict, timeout: int) -> tuple[bool, str]:
    """One real call. Ollama goes to the local daemon; cloud providers go
    through Hermes when present so the runtime path itself is exercised."""
    if name == "ollama":
        model = lib.ollama_pick_model({"models": info.get("candidates")}, state["ollama"])
        if not model:
            return False, "no chat model installed"
        status, body, err = lib.http_json(
            f"{state['ollama']['host']}/api/generate",
            payload={"model": model, "prompt": PROBE_PROMPT, "stream": False},
            timeout=timeout,
        )
        if status == 200:
            return True, f"{model} → {lib.scrub((body or {}).get('response', ''))[:40]}"
        return False, f"HTTP {status} {lib.scrub(err)[:80]}"

    model = (info.get("candidates") or [""])[0]
    if not model:
        return False, "no candidate model configured"
    if not lib.have("hermes"):
        return False, "hermes CLI not installed — cannot exercise the runtime path"
    import subprocess
    # Same runtime initialisation the supervised gateway gets — bridged env,
    # 1Password token, --accept-hooks, bounded turn, stdin closed.  (v1.0.2)
    cmd = lib.hermes_chat_cmd(PROBE_PROMPT, source="jack-doctor", model=model,
                              provider=info["provider"], max_turns=1)
    try:
        proc = lib.hermes_run(cmd, timeout, env=lib.hermes_child_env(yolo=True))
    except subprocess.TimeoutExpired:
        findings = lib.hermes_runtime_diagnose((info.get("credential") or {}).get("key"))
        return False, f"timed out after {timeout}s" + (f" — {findings[0]}" if findings else "")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, lib.scrub(str(exc))[:80]
    if proc.returncode == 0 and proc.stdout.strip():
        return True, f"{model} → {lib.scrub(proc.stdout)[:40]}"
    return False, lib.scrub(proc.stderr or proc.stdout or f"exit {proc.returncode}")[:100]


def section_integrations(out: Out, state: dict) -> None:
    out.section("Integrations")
    for name, info in state["integrations"].items():
        if info["configured"]:
            out.item(name, info["kind"], "ok")
        else:
            # Absent integrations are informational: no deployment needs all.
            out.item(name, f"{info['kind']} — not configured", "info")


def section_identity(out: Out, state: dict) -> None:
    ident = state["identity"]
    out.section("Identity")
    out.item("agent", f"{ident['name']}" + (f" — {ident['role']}" if ident["role"] else ""), "info")
    out.item("status", ident["status"],
             "warn" if ident["status"] == "platform-only" else "ok")
    if ident["soul_present"]:
        out.item("SOUL.md", f"{ident['soul_bytes']} bytes at {ident['soul_file']}", "ok")
    else:
        out.item("SOUL.md", "missing", "bad")


def section_companies(out: Out, state: dict) -> None:
    out.section("Companies")
    companies = state["companies"]
    if not companies:
        out.item("declared", "none", "info")
        return
    for entry in companies:
        out.item(entry.get("name", "unnamed"),
                 f"{entry.get('status', 'declared')} — {entry.get('note', '')}".strip(" —"),
                 "info")


def section_versions(out: Out, state: dict) -> None:
    out.section("Versions")
    versions = state["versions"]
    order = ["platform", "bootstrap", "update", "verify", "routing", "doctor", "manifest"]
    for key in order:
        if key in versions:
            out.item(key, str(versions[key]), "info")
    for key, value in versions.items():
        if key not in order:
            out.item(key, str(value), "info")


def section_warnings(out: Out) -> None:
    out.section("Warnings")
    if not out.warnings and not out.criticals:
        out.line("  none")
        return
    for w in out.criticals:
        out.line(f"  ✗ {w}")
    for w in out.warnings:
        out.line(f"  ! {w}")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(prog="jack doctor",
                                 description="Taylor AI Platform health report")
    ap.add_argument("--providers", action="store_true",
                    help="also run one real inference per available provider (costs a few tokens)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="warnings and verdict only")
    ap.add_argument("--no-color", action="store_true")
    # 120s, not 90: a cold `hermes chat` boots the whole MCP/plugin stack
    # before it emits anything, where the running gateway is already warm.
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()

    if not lib.PLATFORM_FILE.is_file():
        print(f"error: platform spec not found: {lib.PLATFORM_FILE}", file=sys.stderr)
        print("       redeploy with: sudo bash platform/bootstrap.sh", file=sys.stderr)
        return 2

    state = lib.detect_all(args.repo)

    if args.json:
        payload = dict(state)
        if args.providers:
            payload["inference"] = {
                name: dict(zip(("ok", "detail"), _probe_provider(name, info, state, args.timeout)))
                for name, info in state["providers"].items()
                if info["enabled"] and info["available"]
            }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    out = Out(quiet=args.quiet, color=not args.no_color)
    if not args.quiet:
        p = state["platform"]
        print()
        print(out._c("1", f"{p['name']}  v{p['version']}"))

    section_platform(out, state)
    section_services(out, state)
    section_providers(out, state, args.providers, args.timeout)
    section_integrations(out, state)
    section_identity(out, state)
    section_companies(out, state)
    section_versions(out, state)
    section_warnings(out)

    print()
    if out.criticals:
        print(out._c("1;31", f"UNHEALTHY — {len(out.criticals)} critical, {len(out.warnings)} warning(s)"))
        return 1
    if out.warnings:
        print(out._c("1;33", f"HEALTHY with {len(out.warnings)} warning(s)"))
        return 0
    print(out._c("1;32", "HEALTHY"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
