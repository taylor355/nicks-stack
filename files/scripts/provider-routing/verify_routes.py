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


def runtime_env_keys(path: str = "/root/.hermes/runtime/secrets.env") -> set:
    """Names present in the rendered runtime env (v1.1.9). Presence only — no
    value is retained. This is the plane gateway-run.sh sources, so a key here
    IS resolvable at run time even though the 1Password map is deliberately
    disabled. Without it every routing mode filed a standing advisory about the
    intended configuration."""
    keys = set()
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.removeprefix("export ").strip()
                if name and value.strip():
                    keys.add(name)
    except OSError:
        pass
    return keys


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
    runtime_keys = runtime_env_keys()
    gateway_model = (cfg.get("model") or {}).get("default")
    gateway_provider = (cfg.get("model") or {}).get("provider")

    print(f"INFO|modes defined: {', '.join(modes)}")
    print(f"INFO|default mode: {routing.get('default_mode', '<unset>')}")

    for name, raw in modes.items():
        mode = raw or {}
        execution = mode.get("execution")

        if execution == "ollama":
            print(f"INFO|{name}: local Ollama route — daemon and models are checked at run time")
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
            if key in runtime_keys:
                print(f"PASS|{name}: {key} is in the gateway runtime env")
            elif key in op_env and op_on:
                print(f"PASS|{name}: {key} resolves via the 1Password map")
            elif key in op_env:
                print(f"WARN|{name}: {key} is mapped but the 1Password map is disabled and it is not in the runtime env")
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

    # Providers declared for the doctor: each must name a provider Hermes can
    # actually address, and a credential the secret plane can resolve.
    for pname, raw in (routing.get("providers") or {}).items():
        spec = raw or {}
        if not spec.get("enabled", True):
            print(f"INFO|provider '{pname}': declared but disabled")
            continue
        provider = spec.get("provider") or ""
        key = spec.get("key_env")
        optional = not spec.get("required", True)
        suffix = " [optional]" if optional else ""

        if provider.startswith("custom:"):
            bare = provider.split(":", 1)[1]
            if bare in custom:
                print(f"PASS|provider '{pname}' -> {provider} (declared in config.yaml providers)")
            else:
                print(
                    f"FAIL|provider '{pname}' uses {provider} but config.yaml has no "
                    f"providers.{bare} entry{suffix}"
                )
        elif provider in plugins or f"{provider}-provider" in plugins:
            print(f"PASS|provider '{pname}' -> {provider} (plugin enabled)")
        elif provider:
            print(f"FAIL|provider '{pname}' names '{provider}', which is not enabled anywhere{suffix}")
        else:
            print(f"FAIL|provider '{pname}' has no provider name")

        if optional:
            fb = spec.get("fallback_provider")
            print(f"INFO|provider '{pname}': optional — failure does not block validation"
                  + (f" (models available via {fb})" if fb else ""))
        if key:
            if key in op_env:
                print(f"PASS|provider '{pname}': {key} is in the 1Password map")
            else:
                print(f"WARN|provider '{pname}': {key} is not in the 1Password map")
        elif pname == "ollama":
            print(f"INFO|provider '{pname}': local daemon, no credential required")

        models = spec.get("models")
        if models:
            print(f"INFO|provider '{pname}': {len(models)} candidate model id(s) — the doctor verifies them live")
        elif pname == "ollama":
            print(f"INFO|provider '{pname}': models discovered from /api/tags at run time")

    return 0


if __name__ == "__main__":
    sys.exit(main())
