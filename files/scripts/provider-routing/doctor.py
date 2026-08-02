#!/usr/bin/env python3
# ==========================================================================
# Nick's Stack — provider doctor  (nicks-stack-provider-doctor)
# ==========================================================================
# Validates every configured provider end to end and prints a PASS/FAIL block
# per provider, followed by the current routing state.
#
#   SUPERSEDED by `jack doctor` (scripts/platform/doctor.py), which reports the
#   whole platform. This remains as the provider-only deep validation that
#   `jack doctor --providers` builds on, and as a compatibility entry point.
#
#   nicks-stack-provider-doctor                 # verify every provider
#   nicks-stack-provider-doctor --provider gemini
#   nicks-stack-provider-doctor --no-inference  # config + catalog only, no spend
#   nicks-stack-provider-doctor --json
#
# For each provider it does three things, in order, and stops at the first
# failure so a later step never reports a misleading PASS:
#
#   1. credential   — is the key resolvable? (env, ~/.hermes/.env, or the
#                     1Password map via `op read`)
#   2. catalog      — does the provider list the model id the routing map
#                     wants? (this is what "verify the configured model IDs"
#                     means: the id is checked against the vendor's own list,
#                     never assumed)
#   3. inference    — one real, minimal completion. Through the Hermes CLI
#                     when it is available, so the runtime path itself is
#                     exercised; otherwise straight to the vendor API.
#
# SECRET SAFETY: key values are held in memory only. They are never printed,
# never logged, and every vendor error string is scrubbed before display.
#
# EXIT CODES
#   0  every checked provider passed
#   1  at least one provider failed
#   2  usage / configuration error
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Shared platform detection — one implementation for every tool.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import lib as platform_lib  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - python3-yaml is an apt dependency
    print("PyYAML missing — apt-get install -y python3-yaml", file=sys.stderr)
    sys.exit(2)

ROUTING_FILE = Path(os.environ.get("NICKS_STACK_ROUTING", "/root/.hermes/routing.yaml"))
CONFIG_FILE = Path(os.environ.get("NICKS_STACK_CONFIG", "/root/.hermes/config.yaml"))
HERMES_ENV = Path(os.environ.get("NICKS_STACK_ENV", "/root/.hermes/.env"))
OP_ENV = Path(os.environ.get("NICKS_STACK_OP_ENV", "/root/.hermes/.op.env"))

PROBE_PROMPT = "Reply with exactly: ok"
DEFAULT_TIMEOUT = 30   # per-check ceiling (v1.0.1 bug 4)

# Anything that looks like a credential is scrubbed before anything is printed.
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"ops_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"key=[A-Za-z0-9._\-]{12,}"),
]


def scrub(text: str) -> str:
    """Remove anything credential-shaped from a string bound for the terminal."""
    out = text or ""
    for pat in SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out.strip()


def short(text: str, limit: int = 180) -> str:
    text = " ".join(scrub(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_yaml(path: Path) -> dict:
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_key(key_env: str) -> tuple[str | None, str]:
    """Delegates to the shared lib. The value is never printed by any caller."""
    return platform_lib.resolve_key_value(key_env)


# --------------------------------------------------------------------------
# HTTP (stdlib only — no third-party dependency on the agent machine)
# --------------------------------------------------------------------------
def http_json(url: str, headers: dict | None = None, payload: dict | None = None,
              timeout: int = 30) -> tuple[int, dict | None, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
            try:
                return resp.status, json.loads(body), ""
            except ValueError:
                return resp.status, None, body[:400]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return exc.code, None, body[:400]
    except Exception as exc:  # noqa: BLE001 - network paths vary widely
        return 0, None, str(exc)


def hermes_available() -> bool:
    return shutil.which("hermes") is not None


def hermes_infer(provider: str, model: str, timeout: int) -> tuple[bool, str]:
    """One real completion through the Hermes runtime path — the same flags the
    AgentPhone bridge uses in production."""
    cmd = [
        "hermes", "chat", "-Q", "--source", "nicks-stack-provider-doctor",
        "-m", model, "--provider", provider, "-q", PROBE_PROMPT,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, f"hermes timed out after {timeout}s"
    except OSError as exc:
        return False, f"hermes could not run: {exc}"
    if proc.returncode == 0 and proc.stdout.strip():
        return True, short(proc.stdout)
    return False, short(proc.stderr or proc.stdout or f"exit {proc.returncode}")


def guarded(fn, res: "Result", timeout: int):
    """Run one provider check under a hard wall-clock guard so a hung provider
    can never stall the rest of the run.  (v1.0.1 bug 4)"""
    import threading
    box: dict = {}

    def work():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - a check must never kill the run
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if thread.is_alive():
        res.step("completed within timeout", False,
                 f"provider check exceeded {timeout + 5}s — moving on")
        return res
    if "error" in box:
        res.step("completed", False, f"check raised: {box['error']}")
        return res
    return box.get("result", res)


# --------------------------------------------------------------------------
# Per-provider checks
# --------------------------------------------------------------------------
class Result:
    def __init__(self, name: str, display: str):
        self.name = name
        self.display = display
        self.steps: list[dict] = []
        self.models: list[str] = []
        self.checked_model = ""
        self.inference_via = ""
        self.reply = ""
        self.skipped = False
        self.service_state = ""    # supervisor program state, when managed
        self.serving = False
        self.required = True       # optional providers never fail the run
        self.fallback_note = ""
        self.resolved_tiers: dict = {}

    def step(self, label: str, ok: bool, detail: str = "",
             status: int | None = None) -> bool:
        entry = {"step": label, "ok": ok, "detail": short(detail)}
        if not ok:
            category, action = platform_lib.classify_failure(detail, status)
            entry["cause"] = category
            entry["action"] = action
        self.steps.append(entry)
        return ok

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(s["ok"] for s in self.steps)

    def as_dict(self) -> dict:
        return {
            "provider": self.name,
            "display": self.display,
            "status": "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL"),
            "model": self.checked_model,
            "models_discovered": len(self.models),
            "inference_via": self.inference_via,
            "reply": self.reply,
            "required": self.required,
            "fallback_note": self.fallback_note,
            "service_state": self.service_state,
            "serving": self.serving,
            "models": self.models,
            "steps": self.steps,
        }


def check_anthropic(spec: dict, args) -> Result:
    res = Result("anthropic", "Anthropic")
    key, source = resolve_key(spec.get("key_env", "ANTHROPIC_API_KEY"))
    if not res.step("credential", key is not None, f"source: {source}"):
        return res

    models, cat_err = platform_lib.anthropic_catalog(key, args.timeout)
    if not res.step("catalog reachable", not cat_err, cat_err or "GET /v1/models 200"):
        return res
    res.models = [m.get("id", "") for m in models]

    # Tiers, not pins: resolve the CURRENT id for each tier the routing map
    # asks for. A stale pin can no longer break a route.  (v1.0.1 bug 6)
    tiers = platform_lib.anthropic_resolve_tiers(key, args.timeout, refresh=args.refresh_models)
    wanted_tiers = spec.get("tiers") or list(platform_lib.ANTHROPIC_TIERS)
    resolved = {t: tiers.get(t) for t in wanted_tiers}
    unresolved = [t for t, v in resolved.items() if not v]
    detail = ", ".join(f"{t}={v or 'UNRESOLVED'}" for t, v in resolved.items())
    if not res.step("model tiers resolved", not unresolved,
                    f"{detail} (from {len(res.models)} live ids)"):
        return res
    res.resolved_tiers = resolved
    res.checked_model = resolved.get("haiku") or next(iter(v for v in resolved.values() if v), "")

    # Report drift so a stale pin in routing.yaml is visible, without failing.
    pinned = [m for m in (spec.get("models") or []) if m not in res.models]
    if pinned:
        res.fallback_note = (
            f"routing.yaml pins ids that no longer exist: {', '.join(pinned)} "
            f"(harmless — tiers resolve live)"
        )

    if args.no_inference:
        res.step("inference", True, "skipped (--no-inference)")
        return res

    model = res.checked_model or (res.models[0] if res.models else "")
    if hermes_available():
        ok, detail = hermes_infer("anthropic", model, args.timeout)
        res.inference_via = "hermes chat --provider anthropic"
    else:
        status, body, err = http_json(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            payload={"model": model, "max_tokens": 16,
                     "messages": [{"role": "user", "content": PROBE_PROMPT}]},
            timeout=args.timeout,
        )
        ok = status == 200
        detail = (
            "".join(b.get("text", "") for b in (body or {}).get("content", []))
            if ok else f"HTTP {status} {short(err, 90)}"
        )
        res.inference_via = "POST https://api.anthropic.com/v1/messages"
    res.reply = short(detail, 80) if ok else ""
    res.step("inference", ok, detail)
    return res


def check_openrouter(spec: dict, args) -> Result:
    res = Result("openrouter", "OpenRouter")
    key, source = resolve_key(spec.get("key_env", "OPENROUTER_API_KEY"))
    if not res.step("credential", key is not None, f"source: {source}"):
        return res

    # OpenRouter's catalog is public; this is what makes a model id verifiable
    # instead of guessed.
    status, body, err = http_json("https://openrouter.ai/api/v1/models", timeout=args.timeout)
    if not res.step("catalog reachable", status == 200, f"HTTP {status} {short(err, 90)}"):
        return res
    res.models = [m.get("id", "") for m in (body or {}).get("data", [])]

    candidates = spec.get("models") or []
    chosen = next((m for m in candidates if m in res.models), "")
    if not res.step(
        "model ids verified",
        bool(chosen),
        f"chose '{chosen}' from {len(candidates)} candidate(s) against {len(res.models)} live ids"
        if chosen else f"none of {candidates} exist in the live catalog",
    ):
        return res
    res.checked_model = chosen

    if args.no_inference:
        res.step("inference", True, "skipped (--no-inference)")
        return res

    if hermes_available():
        ok, detail = hermes_infer(spec.get("provider", "openrouter"), chosen, args.timeout)
        res.inference_via = f"hermes chat --provider {spec.get('provider', 'openrouter')}"
    else:
        status, body, err = http_json(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            payload={"model": chosen, "max_tokens": 16,
                     "messages": [{"role": "user", "content": PROBE_PROMPT}]},
            timeout=args.timeout,
        )
        ok = status == 200
        detail = (
            ((body or {}).get("choices") or [{}])[0].get("message", {}).get("content", "")
            if ok else f"HTTP {status} {short(err, 90)}"
        )
        res.inference_via = "POST https://openrouter.ai/api/v1/chat/completions"
    res.reply = short(detail, 80) if ok else ""
    res.step("inference", ok, detail)
    return res


def check_gemini(spec: dict, args) -> Result:
    res = Result("gemini", "Gemini")
    key, source = resolve_key(spec.get("key_env", "GEMINI_API_KEY"))
    if not res.step("credential", key is not None, f"source: {source}"):
        return res

    # Native Google endpoint for discovery (the OpenAI-compatibility layer does
    # not expose a usable /models listing).
    status, body, err = http_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        timeout=args.timeout,
    )
    if not res.step("catalog reachable", status == 200, f"HTTP {status} {short(err, 90)}"):
        return res
    res.models = [
        (m.get("name", "") or "").removeprefix("models/")
        for m in (body or {}).get("models", [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]

    candidates = spec.get("models") or []
    chosen = next((m for m in candidates if m in res.models), "")
    if not chosen:
        # Nothing pinned matched — fall back to the newest flash-class model the
        # account can actually see, and say so.
        flash = sorted([m for m in res.models if "flash" in m], reverse=True)
        chosen = flash[0] if flash else (res.models[0] if res.models else "")
    if not res.step(
        "model ids verified",
        bool(chosen),
        f"using '{chosen}' (verified against {len(res.models)} live ids)"
        if chosen else "the account exposes no generateContent model",
    ):
        return res
    res.checked_model = chosen

    if args.no_inference:
        res.step("inference", True, "skipped (--no-inference)")
        return res

    provider = spec.get("provider", "custom:gemini")
    if hermes_available():
        ok, detail = hermes_infer(provider, chosen, args.timeout)
        res.inference_via = f"hermes chat --provider {provider}"
        if not ok:
            # Hermes route not usable — prove the key and model still work
            # directly, so the failure is attributed correctly.
            status, body, err = http_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent",
                headers={"x-goog-api-key": key},
                payload={"contents": [{"parts": [{"text": PROBE_PROMPT}]}]},
                timeout=args.timeout,
            )
            if status == 200:
                res.step("direct API cross-check", True,
                         "key and model work; the Hermes provider route is the problem")
    else:
        status, body, err = http_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent",
            headers={"x-goog-api-key": key},
            payload={"contents": [{"parts": [{"text": PROBE_PROMPT}]}]},
            timeout=args.timeout,
        )
        ok = status == 200
        detail = (
            "".join(
                p.get("text", "")
                for p in (((body or {}).get("candidates") or [{}])[0]
                          .get("content", {}).get("parts", []))
            )
            if ok else f"HTTP {status} {short(err, 90)}"
        )
        res.inference_via = "POST https://generativelanguage.googleapis.com/v1beta/…:generateContent"
    res.reply = short(detail, 80) if ok else ""
    res.step("inference", ok, detail)
    return res


# Embedding-model exclusion and supervisor state both live in the shared lib.
is_chat_model = platform_lib.is_chat_model


def supervisor_state(program: str) -> str:
    return platform_lib.supervisor_status().get(program, {}).get("state", "")


def ollama_host(spec: dict) -> str:
    return platform_lib.ollama_host(spec)


def check_ollama(spec: dict, args) -> Result:
    res = Result("ollama", "Ollama")
    host = ollama_host(spec)

    if not (shutil.which("ollama") or True):  # binary is optional; the API is what matters
        res.step("daemon reachable", False, "ollama binary not installed")
        return res

    res.service_state = supervisor_state("ollama")
    if res.service_state:
        res.step("supervised service", res.service_state == "RUNNING",
                 f"supervisorctl status ollama → {res.service_state}")

    status, body, err = http_json(f"{host}/api/tags", timeout=min(args.timeout, 15))
    res.serving = status == 200
    if not res.step("daemon reachable", status == 200, f"{host} → HTTP {status} {short(err, 90)}"):
        return res

    res.models = [m.get("name", "") for m in (body or {}).get("models", [])]
    if not res.step("models installed", bool(res.models),
                    f"{len(res.models)} model(s): {', '.join(res.models[:6])}"
                    if res.models else "no models pulled — run: ollama pull <model>"):
        return res

    chat_models = [m for m in res.models if is_chat_model(m)]
    if not res.step("chat-capable model present", bool(chat_models),
                    "only embedding models are installed — pull a chat model, e.g. ollama pull qwen3:4b"
                    if not chat_models else f"{len(chat_models)} of {len(res.models)} are chat models"):
        return res

    preferred = [m for m in (spec.get("models") or []) if is_chat_model(m)]
    chosen = next((m for m in preferred if m in chat_models), chat_models[0])
    res.checked_model = chosen

    if args.no_inference:
        res.step("inference", True, "skipped (--no-inference)")
        return res

    provider = spec.get("provider", "custom:ollama")
    if hermes_available():
        ok, detail = hermes_infer(provider, chosen, args.timeout)
        res.inference_via = f"hermes chat --provider {provider}"
    else:
        ok = False
        detail = ""
    if not ok:
        # Native Ollama API — no key, local only. Also the attribution path when
        # the Hermes custom provider is not wired up.
        status, body, err = http_json(
            f"{host}/api/generate",
            payload={"model": chosen, "prompt": PROBE_PROMPT, "stream": False},
            timeout=args.timeout,
        )
        native_ok = status == 200
        if native_ok and hermes_available():
            res.step("hermes route", False,
                     "native Ollama works; the Hermes custom:ollama provider did not answer")
            res.inference_via = f"POST {host}/api/generate (native)"
            res.reply = short((body or {}).get("response", ""), 80)
            res.step("inference", True, short((body or {}).get("response", "")))
            return res
        ok = native_ok
        detail = (body or {}).get("response", "") if ok else f"HTTP {status} {short(err, 90)}"
        res.inference_via = f"POST {host}/api/generate (native)"
    res.reply = short(detail, 80) if ok else ""
    res.step("inference", ok, detail)
    return res


CHECKERS = {
    "anthropic": check_anthropic,
    "openrouter": check_openrouter,
    "gemini": check_gemini,
    "ollama": check_ollama,
}


# --------------------------------------------------------------------------
# Routing state
# --------------------------------------------------------------------------
def routing_state(routing: dict) -> dict:
    modes = routing.get("modes") or {}
    state_file = Path(routing.get("state_file") or "/root/.hermes/state/nicks-stack-mode.json")
    mode = routing.get("default_mode") or "smart"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text())
            if isinstance(data, dict) and data.get("mode"):
                mode = data["mode"]
        except (OSError, ValueError):
            pass
    entry = modes.get(mode) or {}
    return {
        "mode": mode,
        "provider": entry.get("provider") or entry.get("execution") or "—",
        "model": entry.get("model") or "—",
    }


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="nicks-stack-provider-doctor",
        description="Validate every configured provider end to end (credential, catalog, inference).",
    )
    ap.add_argument("--provider", action="append",
                    help="check only this provider (repeatable)")
    ap.add_argument("--no-inference", action="store_true",
                    help="credential + catalog only — makes no billable call")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="per-check ceiling in seconds (default 30)")
    ap.add_argument("--refresh-models", action="store_true",
                    help="re-query the Anthropic catalog instead of the 24h cache")
    args = ap.parse_args()

    routing = load_yaml(ROUTING_FILE)
    if not routing:
        print(f"error: routing map not readable: {ROUTING_FILE}", file=sys.stderr)
        return 2

    providers = routing.get("providers") or {}
    if not providers:
        print(f"error: {ROUTING_FILE} has no 'providers' block to validate", file=sys.stderr)
        return 2

    wanted = args.provider or list(providers)
    unknown = [p for p in wanted if p not in providers]
    if unknown:
        print(f"error: unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    results: list[Result] = []
    for name in wanted:
        spec = providers.get(name) or {}
        checker = CHECKERS.get(name)
        res = Result(name, spec.get("display") or name.title())
        if checker is None:
            res.skipped = True
            res.step("supported", False, "no doctor implementation for this provider")
        elif not spec.get("enabled", True):
            res.skipped = True
            res.steps = []
            res.step("enabled", True, "declared but disabled in routing.yaml")
        else:
            # Each provider runs under a wall-clock guard so one hung endpoint
            # cannot stall the rest of the validation.  (v1.0.1 bug 4)
            res = guarded(lambda sp=spec: checker(sp, args), res, args.timeout)
            res.display = spec.get("display") or res.display
            res.required = bool(spec.get("required", True))
            if not res.required and not res.passed:
                fb = spec.get("fallback_provider")
                res.fallback_note = res.fallback_note or (
                    f"optional provider — models remain available via {fb}" if fb
                    else "optional provider — does not block platform validation")
        results.append(res)

    state = routing_state(routing)
    available = [r.display for r in results if r.passed and not r.skipped]
    unavailable = [r.display for r in results
                   if not r.passed and not r.skipped and r.required]
    optional_failed = [r.display for r in results
                       if not r.passed and not r.skipped and not r.required]
    ollama_models = next((r.models for r in results if r.name == "ollama"), [])

    if args.json:
        print(json.dumps({
            "routing": state,
            "available_providers": available,
            "unavailable_providers": unavailable,
            "optional_failed": optional_failed,
            "ollama_models": ollama_models,
            "providers": [r.as_dict() for r in results],
        }, indent=2))
        return 0 if not unavailable else 1

    for res in results:
        print("Provider:")
        print(res.display)
        if res.skipped:
            print("SKIP")
        elif res.passed:
            print("PASS")
        elif not res.required:
            print("FAIL (optional — does not block platform validation)")
        else:
            print("FAIL")
        for st in res.steps:
            mark = "✓" if st["ok"] else "✗"
            detail = f" — {st['detail']}" if st["detail"] else ""
            print(f"  {mark} {st['step']}{detail}")
            if not st["ok"] and st.get("cause"):
                print(f"      cause : {st['cause']}")
                print(f"      fix   : {st['action']}")
        if res.checked_model:
            print(f"  model: {res.checked_model}")
        if res.inference_via:
            print(f"  via  : {res.inference_via}")
        if res.resolved_tiers:
            print("  tiers: " + ", ".join(f"{t}={m}" for t, m in res.resolved_tiers.items() if m))
        if res.fallback_note:
            print(f"  note : {res.fallback_note}")
        if res.reply:
            print(f"  reply: {res.reply}")

        # Ollama is a platform service, not just an API endpoint — report the
        # service state and the installed model set explicitly.
        if res.name == "ollama":
            print()
            print("Status:")
            if res.serving:
                svc = f" (supervisor: {res.service_state})" if res.service_state else ""
                ollama_status = f"Running{svc}"
            elif res.service_state:
                ollama_status = f"Not serving (supervisor: {res.service_state})"
            else:
                ollama_status = "Not running"
            print(ollama_status)
            print()
            print("Models:")
            if res.models:
                for m in res.models:
                    print(m)
            else:
                print("none installed")
        print()

    print("Current routing mode : " + state["mode"])
    print("Current provider     : " + str(state["provider"]))
    print("Current model        : " + str(state["model"]))
    print("Available providers  : " + (", ".join(available) or "none"))
    print("Unavailable providers: " + (", ".join(unavailable) or "none"))
    if optional_failed:
        print("Optional (non-blocking): " + ", ".join(optional_failed))
    print("Installed Ollama models: " + (", ".join(ollama_models) or "none"))

    # Optional providers (native Gemini) never fail the run.  (v1.0.1 bug 5)
    return 0 if not unavailable else 1


if __name__ == "__main__":
    sys.exit(main())
