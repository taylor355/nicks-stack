#!/usr/bin/env python3
# ==========================================================================
# Nick's Stack — routing map verifier (used by platform/verify.sh)
# ==========================================================================
# Cross-checks routing.yaml against config.yaml and prints one
# "VERDICT|message" line per finding, for verify.sh to classify:
#
#   PASS|…  a check passed
#   FAIL|…  critical: the route cannot work, or breaks a cost-control rule
#   WARN|…  advisory: probably fine, worth knowing
#   INFO|…  context only
#
# READ-ONLY and SECRET-SAFE: it opens two config files, resolves nothing, and
# prints only names, booleans and statuses. No key value is read or emitted.
#
#   verify_routes.py <routing.yaml> <config.yaml>
# ==========================================================================
from __future__ import annotations

import sys

try:
    import yaml
except ImportError:
    print("FAIL|PyYAML missing — cannot evaluate the routing map")
    sys.exit(0)


def load(path: str) -> dict:
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    if len(sys.argv) != 3:
        print("FAIL|verify_routes.py needs <routing.yaml> <config.yaml>")
        return 0

    routing, cfg = load(sys.argv[1]), load(sys.argv[2])

    modes = routing.get("modes") or {}
    if not modes:
        print("FAIL|routing map has no modes")
        return 0

    plugins = (cfg.get("plugins") or {}).get("enabled") or []
    custom = set((cfg.get("providers") or {}).keys())
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    op_env = op.get("env") or {}
    op_on = bool(op.get("enabled"))
    gateway_model = (cfg.get("model") or {}).get("default")
    gateway_provider = (cfg.get("model") or {}).get("provider")

    print(f"INFO|modes defined: {', '.join(modes)}")
    print(f"INFO|default mode: {routing.get('default_mode', '<unset>')}")

    for name, raw in modes.items():
        mode = raw or {}
        execution = mode.get("execution")

        if execution == "ollama":
            print(f"INFO|{name}: local placeholder — reports unavailable until Ollama is installed")
            continue

        if execution == "specialist-cli":
            specs = [s.get("name") for s in (mode.get("specialists") or []) if s.get("name")]
            if specs:
                print(f"INFO|{name}: local CLI specialists ({', '.join(specs)}), availability checked at run time")
            else:
                print(f"FAIL|mode '{name}' is specialist-cli but lists no specialists")
            if not mode.get("requires_approval"):
                print(f"FAIL|mode '{name}' can modify files but does not require approval")
            continue

        provider, model, key = mode.get("provider"), mode.get("model"), mode.get("key_env")
        if not provider or not model:
            print(f"FAIL|mode '{name}' has no provider/model")
            continue

        bare = provider.replace("custom:", "")
        known = provider in plugins or f"{provider}-provider" in plugins or bare in custom
        if known:
            print(f"PASS|mode '{name}' -> {provider}/{model} (provider enabled in config.yaml)")
        else:
            print(
                f"FAIL|mode '{name}' names provider '{provider}', which is neither an "
                f"enabled plugin nor a declared custom provider"
            )

        if execution == "gateway-default":
            if model == gateway_model and provider == gateway_provider:
                print(f"PASS|{name} matches the gateway's own model ({gateway_provider}/{gateway_model})")
            else:
                print(
                    f"FAIL|{name} claims to be the gateway default but config.yaml runs "
                    f"{gateway_provider}/{gateway_model}"
                )

        if key:
            if key in op_env and op_on:
                print(f"PASS|{name}: {key} resolves via the 1Password map")
            elif key in op_env:
                print(f"WARN|{name}: {key} is mapped but the 1Password map is disabled")
            else:
                print(f"WARN|{name}: {key} is not in the 1Password map (may still be set in .env)")

    # Cost-control invariants: a fallback must exist and must never escalate
    # into a mode that requires confirmation.
    for name, raw in modes.items():
        target = (raw or {}).get("fallback")
        if not target:
            continue
        if target not in modes:
            print(f"FAIL|mode '{name}' falls back to unknown mode '{target}'")
        elif (modes.get(target) or {}).get("requires_confirmation"):
            print(f"FAIL|mode '{name}' falls back into premium mode '{target}' — cost-control violation")
        else:
            print(f"PASS|{name} falls back to {target} (one hop, no escalation)")

    for name, raw in modes.items():
        mode = raw or {}
        if mode.get("requires_confirmation"):
            print(f"PASS|{name} requires explicit confirmation before it can run")

    for aname, raw in (routing.get("alternates") or {}).items():
        alt = raw or {}
        state = "enabled" if alt.get("enabled") else "inert"
        print(f"INFO|alternate '{aname}': {state} ({alt.get('status', 'unknown')})")
        if alt.get("enabled") and not alt.get("provider"):
            print(f"FAIL|alternate '{aname}' is enabled but names no provider")

    return 0


if __name__ == "__main__":
    sys.exit(main())
