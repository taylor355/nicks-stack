#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — manifest writer / reader
# ==========================================================================
# Writes the machine-readable record of what this host actually has:
#
#   /opt/nicks-stack/platform-manifest.json
#
# Generated from the shared detection library, so it can never disagree with
# what `jack doctor` or `verify.sh` report — they all call the same code.
#
#   manifest.py write [--repo PATH]   regenerate (bootstrap/update do this)
#   manifest.py show                  print the stored manifest
#   manifest.py report                the deployment report (human-readable)
#
# Contains no secrets: credentials appear as present/absent with their source.
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402  (path shim above is deliberate)


def build(repo: str | None = None) -> dict:
    state = lib.detect_all(repo)
    providers = state["providers"]
    services = state["services"]["programs"]

    return {
        "manifest_version": 1,
        "platform": state["platform"]["name"],
        "platform_version": state["platform"]["version"],
        "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {
            "hostname": state["platform"]["hostname"],
            "os": state["platform"]["os"],
            "init_system": state["platform"]["init_system"],
        },
        "git": state["git"],
        "versions": state["versions"],
        "providers": {
            "enabled": sorted(n for n, p in providers.items() if p["enabled"]),
            "available": sorted(n for n, p in providers.items() if p["available"]),
            "unavailable": sorted(n for n, p in providers.items()
                                  if p["enabled"] and not p["available"]),
            "detail": {n: {"provider": p["provider"], "wired": p["wired"],
                           "credential": p["credential"]["source"],
                           "available": p["available"]}
                       for n, p in providers.items()},
        },
        "services": {
            "configured": sorted(n for n, s in services.items() if s["configured"]),
            "running": sorted(n for n, s in services.items() if s.get("state") == "RUNNING"),
            "detail": services,
        },
        "integrations": {
            "configured": sorted(n for n, i in state["integrations"].items() if i["configured"]),
            "absent": sorted(n for n, i in state["integrations"].items() if not i["configured"]),
        },
        "models": {
            "ollama": state["ollama"]["models"],
            "ollama_chat": state["ollama"]["chat_models"],
            "routing": _routing_models(),
        },
        "identity": state["identity"],
        "companies": state["companies"],
    }


def _routing_models() -> dict:
    routing = lib.load_yaml(lib.ROUTING_FILE)
    out = {}
    for name, raw in (routing.get("modes") or {}).items():
        mode = raw or {}
        if mode.get("model"):
            out[name] = f"{mode.get('provider')}/{mode.get('model')}"
    return out


def write(path: Path, repo: str | None = None) -> dict:
    data = build(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)
    os.chmod(path, 0o644)
    return data


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Deployment report — printed at the end of bootstrap/update
# --------------------------------------------------------------------------
def report(data: dict) -> str:
    lines: list[str] = []
    add = lines.append

    add(data.get("platform", "Taylor AI Platform"))
    add("")
    add("Platform Version")
    add(f"  {data.get('platform_version', 'unknown')}")
    add("")
    add("Git Commit")
    git = data.get("git") or {}
    dirty = " (uncommitted changes)" if git.get("dirty") else ""
    add(f"  {git.get('short', 'unknown')} on {git.get('branch', 'unknown')}{dirty}")
    add("")

    add("Services Installed")
    svc = data.get("services") or {}
    detail = svc.get("detail") or {}
    if detail:
        for name, info in detail.items():
            if info.get("configured"):
                add(f"  {name:<20} {info.get('state', 'unknown')}")
            else:
                add(f"  {name:<20} not configured")
    else:
        add("  none detected")
    add("")

    add("Providers Available")
    prov = data.get("providers") or {}
    if prov.get("available"):
        for name in prov["available"]:
            add(f"  {name}")
    else:
        add("  none")
    if prov.get("unavailable"):
        add("  ---")
        for name in prov["unavailable"]:
            add(f"  {name} (unavailable)")
    add("")

    add("Models Installed")
    models = data.get("models") or {}
    ollama_models = models.get("ollama") or []
    if ollama_models:
        for m in ollama_models:
            add(f"  ollama   {m}")
    else:
        add("  ollama   none (local AI not enabled)")
    for mode, route in (models.get("routing") or {}).items():
        add(f"  {mode:<8} {route}")
    add("")

    add("Warnings")
    warnings = collect_warnings(data)
    if warnings:
        for w in warnings:
            add(f"  ! {w}")
    else:
        add("  none")
    add("")

    add("Next Steps")
    for step in next_steps(data):
        add(f"  - {step}")
    return "\n".join(lines)


def collect_warnings(data: dict) -> list[str]:
    out = []
    svc = (data.get("services") or {}).get("detail") or {}
    for name, info in svc.items():
        if info.get("configured") and info.get("state") not in ("RUNNING", ""):
            out.append(f"service {name} is {info.get('state')}")

    prov = data.get("providers") or {}
    for name in prov.get("unavailable") or []:
        detail = (prov.get("detail") or {}).get(name, {})
        out.append(f"provider {name} unavailable (credential: {detail.get('credential', 'unknown')})")

    ident = data.get("identity") or {}
    if ident.get("status") == "platform-only":
        out.append("identity not built yet — SOUL.md is the stock persona")

    if (data.get("git") or {}).get("dirty"):
        out.append("deployed from a working tree with uncommitted changes")

    integrations = data.get("integrations") or {}
    if "Telegram" in (integrations.get("absent") or []):
        out.append("Telegram not paired — run nicks-stack-onboard.sh")
    if "1Password" in (integrations.get("absent") or []):
        out.append("1Password not connected — secrets resolve from .env only")
    return out


def next_steps(data: dict) -> list[str]:
    steps = []
    integrations = data.get("integrations") or {}
    absent = integrations.get("absent") or []
    if "Telegram" in absent or "1Password" in absent:
        steps.append("finish onboarding:  sudo /usr/local/bin/nicks-stack-onboard.sh")
    steps.append("full health report: sudo jack doctor")
    if (data.get("providers") or {}).get("unavailable"):
        steps.append("validate providers: sudo jack doctor --providers")
    if not (data.get("models") or {}).get("ollama"):
        steps.append("optional local AI:  sudo bash platform/bootstrap.sh --with-ollama")
    steps.append("read the docs:      platform/DEPLOYMENT.md")
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(prog="manifest.py",
                                 description="Taylor AI Platform manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write", help="regenerate the manifest")
    p.add_argument("--repo", default=os.environ.get("NICKS_STACK_REPO", "."))
    p.add_argument("--path", default=str(lib.MANIFEST_FILE))
    p = sub.add_parser("show", help="print the stored manifest")
    p.add_argument("--path", default=str(lib.MANIFEST_FILE))
    p = sub.add_parser("report", help="print the deployment report")
    p.add_argument("--repo", default=os.environ.get("NICKS_STACK_REPO", "."))
    p.add_argument("--path", default=str(lib.MANIFEST_FILE))
    p.add_argument("--regenerate", action="store_true")
    args = ap.parse_args()

    path = Path(args.path)
    if args.cmd == "write":
        data = write(path, args.repo)
        print(f"manifest written: {path} (platform {data['platform_version']})")
        return 0
    if args.cmd == "show":
        data = read(path)
        if not data:
            print(f"no manifest at {path} — run: jack manifest write", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2))
        return 0
    data = write(path, args.repo) if args.regenerate else (read(path) or build(args.repo))
    print(report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
