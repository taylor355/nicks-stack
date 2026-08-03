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
#   jack doctor --providers      + one CAPPED inference per provider
#   jack doctor --providers --via hermes   + the (uncapped) Hermes runtime leg
#   jack doctor --json           machine-readable
#   jack doctor --quiet          warnings and the verdict only
#
# By default the doctor is FREE and FAST: it inspects configuration, services
# and local state, and makes no paid API call. --providers adds the end-to-end
# credential -> catalog -> inference validation, capped at routing.yaml's
# validation.max_tokens so a health check can never burn quota.  (v1.0.4)
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


def section_providers(out: Out, state: dict, probe, timeout: int) -> None:
    """probe is False, or the leg to use: "api" | "hermes" | "both"."""
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
        mode = probe if isinstance(probe, str) else "api"
        budget = lib.validation_spec()["max_tokens"]
        out.line()
        out.line(f"  live inference (one capped call per provider, "
                 f"max_tokens={budget}):")
        for name, info in providers.items():
            if not info["enabled"] or not info["available"]:
                continue
            if mode in ("api", "both"):
                ok, detail = _probe_provider(name, info, state, timeout)
                out.item(f"  {info['display']} inference", detail, "ok" if ok else "bad")
            if mode in ("hermes", "both") and name != "ollama":
                # Uncapped by necessity — opt-in only.
                h_ok, h_detail = _probe_provider_via_hermes(name, info, timeout)
                out.item(f"  {info['display']} hermes runtime", h_detail,
                         "ok" if h_ok else "bad")


def _probe_provider(name: str, info: dict, state: dict, timeout: int) -> tuple[bool, str]:
    """One real, CAPPED call per provider.

    v1.0.4: this goes to the vendor API through lib.api_probe(), the same
    implementation the provider doctor uses, with the output cap from
    routing.yaml (validation.max_tokens). A health report must never be the
    expensive way to find out a key works — an uncapped probe advertising the
    model's full completion budget is what OpenRouter answers with HTTP 402.
    The Hermes runtime leg lives in `nicks-stack-provider-doctor --via hermes`,
    which is opt-in because it cannot be capped."""
    budget = lib.validation_spec()["max_tokens"]

    if name == "ollama":
        model = lib.ollama_pick_model({"models": info.get("candidates")}, state["ollama"])
        if not model:
            return False, "no chat model installed"
        ok, detail, _ = lib.api_probe("ollama", model=model,
                                      base_url=state["ollama"]["host"],
                                      max_tokens=budget, timeout=timeout)
        return (True, f"{model} → {lib.scrub(detail)[:40]}") if ok else (False, detail[:80])

    model = (info.get("candidates") or [""])[0]
    if not model:
        return False, "no candidate model configured"

    key_env = (info.get("credential") or {}).get("key")
    key, source = lib.resolve_key_value(key_env) if key_env else (None, "not required")
    if key_env and not key:
        return False, f"{key_env} not resolvable ({source})"
    ok, detail, _ = lib.api_probe(
        lib.vendor_for(info.get("provider", name)), model=model, key=key,
        max_tokens=budget, timeout=timeout)
    if ok:
        return True, f"{model} → {lib.scrub(detail)[:40]}  (max_tokens={budget})"
    cause, action = lib.classify_failure(detail)
    return False, f"{detail[:80]}  [{cause}: {action[:60]}]"


def _probe_provider_via_hermes(name: str, info: dict, timeout: int) -> tuple[bool, str]:
    """The Hermes runtime leg — uncapped, so never on the default path."""
    model = (info.get("candidates") or [""])[0]
    if not model:
        return False, "no candidate model configured"
    if not lib.have("hermes"):
        return False, "hermes CLI not installed — cannot exercise the runtime path"
    # Same runtime initialisation the supervised gateway gets (v1.0.2), in the
    # lean validation profile (v1.0.3), with an automatic fallback to the full
    # runtime if that profile is not usable on this machine.
    res = lib.hermes_probe(
        PROBE_PROMPT, source="jack-doctor", timeout=timeout, model=model,
        provider=info["provider"], profile=lib.profile_for("validation"),
        key_env=(info.get("credential") or {}).get("key"), max_turns=1,
    )
    suffix = "  [full runtime]" if res.get("fell_back") else ""
    if res["ok"]:
        return True, f"{model} → {res['detail'][:40]}{suffix}"
    return False, f"{res['detail'][:100]}{suffix}"


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
                    help="also run one capped inference per available provider "
                         "(bounded by routing.yaml validation.max_tokens)")
    ap.add_argument("--via", choices=("api", "hermes", "both"), default=None,
                    help="which leg --providers uses: 'api' (default) is capped and cheap; "
                         "'hermes' exercises the Hermes runtime and cannot be capped")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="warnings and verdict only")
    ap.add_argument("--no-color", action="store_true")
    # 120s covers a cold `hermes chat` (--via hermes). The default --providers
    # leg is a capped HTTP call and is dropped to 20s below.  (v1.0.5)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()

    # A capped vendor call does not need a cold-Hermes-start budget.  (v1.0.5)
    if args.timeout == 120 and (args.via or "api") == "api":
        args.timeout = 20

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
    # --via selects the leg; the default stays the capped API call so a
    # health report can never be the expensive way to learn a key works.
    section_providers(out, state,
                      (args.via or "api") if args.providers else False,
                      args.timeout)
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
