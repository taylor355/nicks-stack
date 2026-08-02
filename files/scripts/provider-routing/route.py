#!/usr/bin/env python3
# ==========================================================================
# Nick's Stack — provider routing CLI  (nicks-stack-route)
# ==========================================================================
# The smallest reliable routing layer the installed Hermes supports. It does
# four things and nothing else:
#
#   1. reads /root/.hermes/routing.yaml (the declarative mode map)
#   2. remembers which mode is selected, in /root/.hermes/state/
#   3. reports mode / provider / model / why, so a route is never opaque
#   4. runs a one-shot in a chosen route via the Hermes CLI flags that the
#      AgentPhone bridge already uses in production:
#          hermes chat -Q --source <src> -m <model> --provider <provider> -q <prompt>
#
# It deliberately does NOT: change config.yaml, restart the gateway, execute a
# coding agent, or pick a mode on its own.
#
#   nicks-stack-route show [--json]         current mode, provider, model, why
#   nicks-stack-route modes [--json]        every mode and its availability
#   nicks-stack-route set <mode>            select a mode (persisted)
#   nicks-stack-route explain <mode>        why that mode routes where it does
#   nicks-stack-route run <mode> -q "..."   one-shot in that route
#   nicks-stack-route probe [--mode M]      1-token liveness call per route
#   nicks-stack-route preflight [--json]    can each mode run right now?
#
# EXIT CODES
#   0 ok    1 error    2 usage    3 approval/confirmation required
#   4 mode unavailable (local before Ollama; build with no authed specialist)
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Shared platform detection — one implementation, used by every tool.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import lib as platform_lib  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - python3-yaml is an apt dependency
    print("PyYAML missing — apt-get install -y python3-yaml", file=sys.stderr)
    sys.exit(1)

ROUTING_FILE = Path(os.environ.get("NICKS_STACK_ROUTING", "/root/.hermes/routing.yaml"))
HERMES_ENV = Path(os.environ.get('NICKS_STACK_ENV', '/root/.hermes/.env'))
SOURCE_TAG = "nicks-stack-routing"
PROBE_PROMPT = "Reply with exactly: ok"

E_OK, E_ERR, E_USAGE, E_APPROVAL, E_UNAVAILABLE = 0, 1, 2, 3, 4


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_routing() -> dict:
    if not ROUTING_FILE.is_file():
        die(f"routing map not found: {ROUTING_FILE} (redeploy: platform/bootstrap.sh)")
    with ROUTING_FILE.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict) or "modes" not in data:
        die(f"routing map is malformed (no 'modes' key): {ROUTING_FILE}")
    return data


def state_path(cfg: dict) -> Path:
    return Path(cfg.get("state_file") or "/root/.hermes/state/nicks-stack-mode.json")


def read_state(cfg: dict) -> dict:
    path = state_path(cfg)
    if path.is_file():
        try:
            with path.open() as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def write_state(cfg: dict, mode: str) -> Path:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump({"mode": mode, "source": "nicks-stack-route"}, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)
    return path


def current_mode(cfg: dict) -> str:
    return read_state(cfg).get("mode") or cfg.get("default_mode") or "smart"


def get_mode(cfg: dict, name: str) -> dict:
    modes = cfg.get("modes") or {}
    if name not in modes:
        die(f"unknown mode '{name}' — known modes: {', '.join(sorted(modes))}", E_USAGE)
    entry = dict(modes[name] or {})
    entry["name"] = name
    return entry


def die(msg: str, code: int = E_ERR) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# Availability — never reports a route as usable when it is not
# --------------------------------------------------------------------------
def key_available(key_env: str | None) -> bool:
    """True when the provider key can be resolved. Presence only — the value is
    never read into a variable, printed, or logged."""
    if not key_env:
        return True
    if os.environ.get(key_env, "").strip():
        return True
    try:
        with HERMES_ENV.open() as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line.startswith(f"{key_env}=") and len(line.split("=", 1)[1].strip()) > 1:
                    return True
    except OSError:
        pass
    # Mapped through the 1Password secret plane: hermes resolves it at start,
    # so a key absent from .env is not necessarily missing.
    return op_mapped(key_env)


def op_mapped(key_env: str) -> bool:
    cfg_path = Path("/root/.hermes/config.yaml")
    try:
        with cfg_path.open() as fh:
            cfg = yaml.safe_load(fh) or {}
    except OSError:
        return False
    if not isinstance(cfg, dict):
        return False
    op = ((cfg.get("secrets") or {}).get("onepassword") or {})
    return bool(op.get("enabled")) and key_env in (op.get("env") or {})


def ollama_probe(mode: dict) -> tuple[bool, list[str], str]:
    """Thin wrapper over the shared platform detection (scripts/platform/lib.py)
    so the router, the doctor and verify.sh can never disagree about Ollama."""
    info = platform_lib.ollama_detect(mode)
    if not info["serving"] or not info["models"]:
        return False, info["models"], info["detail"]
    return True, info["models"], info["detail"]


def specialist_status(spec: dict) -> dict:
    """Auth status of a build-mode specialist CLI. Runs only its documented
    read-only status command."""
    binary = spec.get("binary") or ""
    out = {"name": spec.get("name"), "binary": binary, "installed": False, "authenticated": False}
    if not binary or not shutil.which(binary):
        return out
    out["installed"] = True
    check = spec.get("auth_check")
    if not check:
        return out
    try:
        rc = subprocess.run(
            check.split(), capture_output=True, text=True, timeout=30, check=False
        )
        out["authenticated"] = rc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        out["authenticated"] = False
    return out


def mode_availability(cfg: dict, name: str) -> dict:
    mode = get_mode(cfg, name)
    execution = mode.get("execution")
    info = {
        "mode": name,
        "execution": execution,
        "provider": mode.get("provider"),
        "model": mode.get("model"),
        "summary": (mode.get("summary") or "").strip(),
        "why": " ".join((mode.get("why") or "").split()),
        "available": False,
        "detail": "",
    }

    if execution == "ollama":
        reachable, models, detail = ollama_probe(mode)
        info["installed_models"] = models
        info["detail"] = detail
        if not reachable:
            return info
        # Pin from routing.yaml when set, otherwise the first chat-capable
        # model pulled locally. Embedding models are never selected — they
        # cannot answer a prompt.
        chat = [m for m in models if platform_lib.is_chat_model(m)]
        if not chat:
            info["detail"] = (
                f"only embedding models are installed ({', '.join(models)}) — "
                "pull a chat model, e.g. ollama pull qwen3:4b"
            )
            return info
        info["model"] = mode.get("model") or chat[0]
        info["available"] = True
        info["detail"] = f"{len(chat)} chat model(s): {', '.join(chat[:4])}"
        return info

    if execution == "specialist-cli":
        specs = [specialist_status(s) for s in (mode.get("specialists") or [])]
        info["specialists"] = specs
        usable = [s for s in specs if s["installed"] and s["authenticated"]]
        info["available"] = bool(usable)
        info["detail"] = (
            f"available specialist: {usable[0]['name']}" if usable
            else "no authenticated coding CLI (claude / codex) on this machine"
        )
        info["requires_approval"] = True
        return info

    if not shutil.which("hermes"):
        info["detail"] = "hermes CLI not on PATH"
        return info
    if not key_available(mode.get("key_env")):
        info["detail"] = f"{mode.get('key_env')} not resolvable (.env or 1Password map)"
        return info

    info["available"] = True
    info["requires_confirmation"] = bool(mode.get("requires_confirmation"))
    info["detail"] = (
        "gateway default — Jack answers directly"
        if execution == "gateway-default"
        else f"one-shot via: hermes chat -m {mode.get('model')} --provider {mode.get('provider')}"
    )
    return info


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def build_cmd(mode: dict, prompt: str, toolsets: str | None) -> list[str]:
    """Exactly the invocation shape the AgentPhone bridge uses in production."""
    cmd = ["hermes", "chat", "-Q", "--source", SOURCE_TAG]
    if mode.get("model"):
        cmd += ["-m", str(mode["model"])]
    if mode.get("provider"):
        cmd += ["--provider", str(mode["provider"])]
    cmd += ["-q", prompt]
    if toolsets:
        cmd += ["-t", toolsets]
    return cmd


def run_oneshot(mode: dict, prompt: str, toolsets: str | None, timeout: int) -> tuple[int, str, str]:
    cmd = build_cmd(mode, prompt, toolsets)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", "hermes CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_show(cfg: dict, args) -> int:
    name = args.mode or current_mode(cfg)
    info = mode_availability(cfg, name)
    info["selected"] = name == current_mode(cfg)
    info["default_mode"] = cfg.get("default_mode")
    info["routing_file"] = str(ROUTING_FILE)
    if args.json:
        print(json.dumps(info, indent=2))
        return E_OK
    print(f"mode      : {info['mode']}{'' if info['selected'] else '  (not the selected mode)'}")
    print(f"provider  : {info['provider'] or '—'}")
    print(f"model     : {info['model'] or '—'}")
    print(f"execution : {info['execution']}")
    print(f"available : {'yes' if info['available'] else 'NO'}  ({info['detail']})")
    print(f"purpose   : {info['summary']}")
    print(f"why       : {info['why']}")
    return E_OK


def cmd_modes(cfg: dict, args) -> int:
    selected = current_mode(cfg)
    rows = [mode_availability(cfg, m) for m in cfg.get("modes", {})]
    if args.json:
        print(json.dumps({"selected": selected, "modes": rows}, indent=2))
        return E_OK
    print(f"selected mode: {selected}\n")
    for r in rows:
        mark = "*" if r["mode"] == selected else " "
        status = "ok" if r["available"] else "UNAVAILABLE"
        target = f"{r['provider']}/{r['model'] or '<resolved at run time>'}" if r["provider"] else r["execution"]
        print(f" {mark} {r['mode']:<6} {target:<32} {status:<12} {r['detail']}")
    return E_OK


def cmd_set(cfg: dict, args) -> int:
    info = mode_availability(cfg, args.mode)
    path = write_state(cfg, args.mode)
    print(f"mode set to '{args.mode}' ({path})")
    print(f"provider  : {info['provider'] or '—'}")
    print(f"model     : {info['model'] or '—'}")
    if not info["available"]:
        print(f"warning   : this mode is not usable right now — {info['detail']}")
        return E_UNAVAILABLE
    if info.get("requires_confirmation"):
        print("note      : deep mode still requires --confirm on every run")
    if info.get("requires_approval"):
        print("note      : build mode still requires explicit approval before any file is changed")
    return E_OK


def cmd_explain(cfg: dict, args) -> int:
    info = mode_availability(cfg, args.mode)
    print(f"{args.mode}: {info['summary']}\n{info['why']}")
    if info["provider"]:
        print(f"\nroute: provider={info['provider']} model={info['model']}")
    mode = get_mode(cfg, args.mode)
    if mode.get("fallback"):
        print(f"fallback: one retry on '{mode['fallback']}' if this route fails")
    else:
        print("fallback: none — this route never escalates on its own")
    return E_OK


def cmd_run(cfg: dict, args) -> int:
    name = args.mode or current_mode(cfg)
    mode = get_mode(cfg, name)
    info = mode_availability(cfg, name)
    execution = mode.get("execution")

    if execution == "ollama":
        if not info["available"]:
            print(f"local mode is not available: {info['detail']}", file=sys.stderr)
            print("No paid route was used. Fix Ollama first, then re-run.", file=sys.stderr)
            return E_UNAVAILABLE
        if not args.prompt:
            die("a prompt is required: run local -q \"...\"", E_USAGE)
        # Resolve the model the same way availability did, then use the normal
        # one-shot path — local is a route like any other once it is up.
        mode = dict(mode)
        mode["model"] = info["model"]
        rc, out, errs = run_oneshot(mode, args.prompt, args.toolsets, args.timeout)
        if rc == 0:
            if not args.quiet:
                print(f"[route: {name} → {mode.get('provider')}/{mode['model']} (local)]",
                      file=sys.stderr)
            print(out)
            return E_OK
        print(f"local route failed (exit {rc}): {errs or 'no stderr'}", file=sys.stderr)
        print("Local mode does not fall back to a paid route.", file=sys.stderr)
        return E_ERR

    if execution == "specialist-cli":
        # This router never executes a coding agent. It reports the plan; a
        # human approves it in chat and the work runs through the
        # coding-agent-routing skill.
        print(f"build mode: {info['detail']}")
        for spec in info.get("specialists", []):
            print(
                f"  {spec['name']:<12} installed={'yes' if spec['installed'] else 'no':<3} "
                f"authenticated={'yes' if spec['authenticated'] else 'no'}"
            )
        if not info["available"]:
            print("no authenticated specialist — do not attempt an API substitute.", file=sys.stderr)
            return E_UNAVAILABLE
        chosen = next(s for s in info["specialists"] if s["installed"] and s["authenticated"])
        template = next(
            (s.get("invoke") for s in mode.get("specialists", []) if s.get("name") == chosen["name"]),
            "",
        )
        print(f"\nproposed specialist: {chosen['name']}")
        print(f"command template   : {template}")
        print("\nThis router does not run coding agents. Get explicit approval, then run")
        print("the command above yourself — see the coding-agent-routing skill.")
        return E_APPROVAL

    if mode.get("requires_confirmation") and not args.confirm:
        print(
            f"'{name}' is a premium route ({mode.get('provider')}/{mode.get('model')}) "
            "and must be requested explicitly.",
            file=sys.stderr,
        )
        print("Re-run with --confirm once the user has agreed to it.", file=sys.stderr)
        return E_APPROVAL

    if not args.prompt:
        die("a prompt is required: run <mode> -q \"...\"", E_USAGE)

    if not info["available"]:
        print(f"route unavailable: {info['detail']}", file=sys.stderr)
        return E_UNAVAILABLE

    rc, out, errs = run_oneshot(mode, args.prompt, args.toolsets, args.timeout)
    if rc == 0:
        if not args.quiet:
            print(f"[route: {name} → {mode.get('provider')}/{mode.get('model')}]", file=sys.stderr)
        print(out)
        return E_OK

    # One fallback hop, only ever to the mode named in routing.yaml, and only
    # ever downwards in cost. Never silently into a premium route.
    fallback = mode.get("fallback")
    print(f"route '{name}' failed (exit {rc}): {errs or 'no stderr'}", file=sys.stderr)
    if not fallback or args.no_fallback:
        return E_ERR

    fb = get_mode(cfg, fallback)
    if fb.get("requires_confirmation"):
        print(
            f"refusing to fall back into premium mode '{fallback}' automatically.",
            file=sys.stderr,
        )
        return E_ERR
    if not mode_availability(cfg, fallback)["available"]:
        print(f"fallback '{fallback}' is not available either.", file=sys.stderr)
        return E_ERR

    print(f"falling back once: {name} → {fallback}", file=sys.stderr)
    rc2, out2, err2 = run_oneshot(fb, args.prompt, args.toolsets, args.timeout)
    if rc2 == 0:
        print(f"[route: {fallback} (fallback from {name}) → {fb.get('provider')}/{fb.get('model')}]",
              file=sys.stderr)
        print(out2)
        return E_OK
    print(f"fallback '{fallback}' also failed (exit {rc2}): {err2 or 'no stderr'}", file=sys.stderr)
    return E_ERR


def cmd_probe(cfg: dict, args) -> int:
    names = [args.mode] if args.mode else list(cfg.get("modes", {}))
    failures = 0
    for name in names:
        mode = get_mode(cfg, name)
        info = mode_availability(cfg, name)
        if mode.get("execution") == "specialist-cli":
            print(f"{name:<6} skipped      ({info['detail']})")
            continue
        if not info["available"]:
            print(f"{name:<6} UNAVAILABLE  {info['detail']}")
            failures += 1
            continue
        rc, out, errs = run_oneshot(mode, PROBE_PROMPT, None, args.timeout)
        target = f"{mode.get('provider')}/{mode.get('model')}"
        if rc == 0 and "ok" in out.lower():
            print(f"{name:<6} ok           {target}")
        else:
            print(f"{name:<6} FAILED       {target}: {(errs or out or 'no output')[:160]}")
            failures += 1
    return E_OK if failures == 0 else E_ERR


def cmd_preflight(cfg: dict, args) -> int:
    rows = [mode_availability(cfg, m) for m in cfg.get("modes", {})]
    payload = {"selected": current_mode(cfg), "modes": rows}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for r in rows:
            print(f"{r['mode']:<6} {'ok' if r['available'] else 'unavailable':<12} {r['detail']}")
    # Preflight never fails the caller for local/build being unavailable — those
    # are expected states, not errors.
    return E_OK


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="nicks-stack-route",
        description="Nick's Stack provider routing — inspect and use explicit model routes.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="current mode, provider, model and why")
    p.add_argument("mode", nargs="?")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("modes", help="list every mode and its availability")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("set", help="select a mode (persisted)")
    p.add_argument("mode")

    p = sub.add_parser("explain", help="why a mode routes where it does")
    p.add_argument("mode")

    p = sub.add_parser("run", help="run a one-shot in a route")
    p.add_argument("mode", nargs="?")
    p.add_argument("-q", "--prompt")
    p.add_argument("-t", "--toolsets")
    p.add_argument("--confirm", action="store_true", help="required for premium (deep) routes")
    p.add_argument("--no-fallback", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--timeout", type=int, default=900)

    p = sub.add_parser("probe", help="1-token liveness call per route (costs a few tokens)")
    p.add_argument("--mode")
    p.add_argument("--timeout", type=int, default=120)

    p = sub.add_parser("preflight", help="can each mode run right now?")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args()
    cfg = load_routing()
    return {
        "show": cmd_show,
        "modes": cmd_modes,
        "set": cmd_set,
        "explain": cmd_explain,
        "run": cmd_run,
        "probe": cmd_probe,
        "preflight": cmd_preflight,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
