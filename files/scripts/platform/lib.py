#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — shared detection library
# ==========================================================================
# ONE implementation of every "is X installed / running / configured" question
# the platform asks. bootstrap.sh, update.sh, verify.sh, jack doctor and the
# routing CLI all read from here, so a detection rule is fixed in one place and
# cannot drift between tools.
#
# Python callers:   from lib import detect_all, ollama_detect, supervisor_status
# Shell callers:    python3 lib.py detect --json      (the whole picture)
#                   python3 lib.py ollama --json
#                   python3 lib.py versions --json
#                   python3 lib.py runtime             (gateway-vs-one-shot parity)
#                   python3 lib.py profiles            (runtime profiles, v1.0.3)
#
# SECRET SAFETY: credential *presence* is detected and reported; a value is
# only ever returned by resolve_key_value(), which no reporting path calls.
# ==========================================================================
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - python3-yaml is an apt dependency
    print("PyYAML missing — apt-get install -y python3-yaml", file=sys.stderr)
    sys.exit(2)

# --------------------------------------------------------------------------
# Paths — every tool inherits these, overridable for testing
# --------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("NICKS_STACK_HERMES_HOME", "/root/.hermes"))
CONFIG_FILE = Path(os.environ.get("NICKS_STACK_CONFIG", HERMES_HOME / "config.yaml"))
ROUTING_FILE = Path(os.environ.get("NICKS_STACK_ROUTING", HERMES_HOME / "routing.yaml"))
PLATFORM_FILE = Path(os.environ.get("NICKS_STACK_PLATFORM", HERMES_HOME / "platform.yaml"))
HERMES_ENV = Path(os.environ.get("NICKS_STACK_ENV", HERMES_HOME / ".env"))
OP_ENV = Path(os.environ.get("NICKS_STACK_OP_ENV", HERMES_HOME / ".op.env"))
SOUL_FILE = HERMES_HOME / "SOUL.md"
VAULT_DIR = Path(os.environ.get("NICKS_STACK_VAULT", "/root/Documents/HermesVault"))
BRIDGE_DIR = Path("/root/.hermes_agentphone_bridge")
STACK_ROOT = Path(os.environ.get("NICKS_STACK_ROOT", "/opt/nicks-stack"))
MANIFEST_FILE = Path(os.environ.get("NICKS_STACK_MANIFEST", STACK_ROOT / "platform-manifest.json"))

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

# Embedding models cannot answer a prompt — never offer one as a chat route.
EMBED_HINTS = ("embed", "embedding", "bge-", "gte-", "minilm")

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"ops_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"key=[A-Za-z0-9._\-]{12,}"),
]


def scrub(text: str) -> str:
    out = text or ""
    for pat in SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out.strip()


def load_yaml(path: Path | str) -> dict:
    try:
        with Path(path).open() as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def is_chat_model(name: str) -> bool:
    lowered = (name or "").lower()
    return bool(name) and not any(hint in lowered for hint in EMBED_HINTS)


def http_json(url: str, headers: dict | None = None, payload: dict | None = None,
              timeout: int = 15) -> tuple[int, dict | None, str]:
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
        return exc.code, None, exc.read().decode(errors="replace")[:400]
    except Exception as exc:  # noqa: BLE001 - every transport failure is "down"
        return 0, None, str(exc)


# --------------------------------------------------------------------------
# Credentials — presence for reporting, value only on explicit request
# --------------------------------------------------------------------------
def _env_file_value(key_env: str) -> str:
    try:
        with HERMES_ENV.open() as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line.startswith(f"{key_env}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def op_token() -> str:
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in OP_ENV.read_text().splitlines():
            if line.startswith("OP_SERVICE_ACCOUNT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def resolve_key_value(key_env: str) -> tuple[str | None, str]:
    """Return (value, source). Only the doctor's inference path calls this."""
    val = os.environ.get(key_env, "").strip()
    if val:
        return val, "environment"
    val = _env_file_value(key_env)
    if val:
        return val, ".env"

    cfg = load_yaml(CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    ref = (op.get("env") or {}).get(key_env)
    token = op_token()
    if ref and token and have("op"):
        env = dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN=token)
        try:
            proc = subprocess.run(["op", "read", ref], capture_output=True, text=True,
                                  timeout=30, env=env, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip(), "1Password"
        except (OSError, subprocess.SubprocessError):
            pass
    return None, "unresolved"


# --------------------------------------------------------------------------
# THE canonical runtime-secret resolver  (v1.1.8)
# --------------------------------------------------------------------------
# Before v1.1.8 each component decided for itself what "the key is available"
# meant: composio_session had a real resolver, the provider doctor used
# key_presence() (which says "present" merely because an op:// reference is
# MAPPED), verify.sh had a third opinion, and the gateway had none at all.
# They could and did disagree.
#
# There is now one function. It performs a REAL resolution — env, then
# ~/.hermes/.env, then `op read` — and returns one shape. Nothing else in the
# platform is allowed to invent a fourth answer.
def resolve_runtime_secret(key_env: str) -> dict:
    """Resolve one secret for the runtime. Returns
    {key, available, source, error, value}.

    `value` is a SECRET. Only the renderer that writes the 0600 runtime env
    may keep it; every reporting caller must drop it (runtime_secrets_report
    does)."""
    value, source = resolve_key_value(key_env)
    value = (value or "").strip()
    if value:
        return {"key": key_env, "available": True, "source": source,
                "error": "", "value": value}

    cfg = load_yaml(CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    ref = (op.get("env") or {}).get(key_env)
    if not ref:
        error = (f"neither set in the environment nor in {HERMES_ENV}, and not "
                 f"mapped in config.yaml secrets.onepassword.env")
    elif not op_token():
        error = (f"mapped to 1Password but no OP_SERVICE_ACCOUNT_TOKEN is "
                 f"reachable ({OP_ENV}) — the vault cannot be read")
    elif not have("op"):
        error = "mapped to 1Password but the `op` CLI is not installed"
    else:
        field = str(ref).rsplit("/", 1)[-1]
        error = (f"`op read` returned nothing — the field '{field}' is missing "
                 f"from the 1Password item (PROVISIONING)")
    return {"key": key_env, "available": False, "source": "unresolved",
            "error": error, "value": ""}


def runtime_secrets_spec() -> dict:
    """platform.yaml runtime_secrets, normalised. Never raises."""
    spec = load_yaml(PLATFORM_FILE).get("runtime_secrets")
    spec = spec if isinstance(spec, dict) else {}
    keys = []
    for entry in spec.get("keys") or []:
        if isinstance(entry, str):
            entry = {"env": entry}
        if not isinstance(entry, dict) or not entry.get("env"):
            continue
        keys.append({"env": str(entry["env"]),
                     "required": bool(entry.get("required", False)),
                     "consumer": str(entry.get("consumer", ""))})
    return {
        "dir": Path(spec.get("dir") or (HERMES_HOME / "runtime")),
        "file": str(spec.get("file") or "secrets.env"),
        "keys": keys,
        "purge": [Path(p) for p in (spec.get("purge_plaintext_caches") or [])],
    }


def runtime_secrets_path() -> Path:
    spec = runtime_secrets_spec()
    return Path(spec["dir"]) / spec["file"]


def runtime_secrets_report() -> dict:
    """Presence-only report over every declared runtime secret.

    This is what `jack secrets status`, `jack doctor` and verify.sh consume.
    The resolved VALUES are dropped here and never leave this function."""
    spec = runtime_secrets_spec()
    path = Path(spec["dir"]) / spec["file"]
    rendered = parse_env_file(path) if path.is_file() else {}
    results, missing_required = [], []
    for entry in spec["keys"]:
        res = resolve_runtime_secret(entry["env"])
        in_runtime = bool((rendered.get(entry["env"]) or "").strip())
        results.append({
            "key": entry["env"],
            "required": entry["required"],
            "consumer": entry["consumer"],
            "available": res["available"],
            "source": res["source"],
            "error": res["error"],
            "in_runtime_env": in_runtime,      # will the GATEWAY see it?
        })
        if entry["required"] and not res["available"]:
            missing_required.append(entry["env"])
    try:
        mode = oct(path.stat().st_mode)[-3:]
    except OSError:
        mode = ""
    return {
        "path": str(path),
        "exists": path.is_file(),
        "mode": mode,
        "mode_ok": mode == "600" if mode else False,
        "keys": results,
        "missing_required": missing_required,
        "ok": not missing_required,
        "gateway_ready": bool(results) and all(
            r["in_runtime_env"] for r in results if r["required"]),
    }


def key_presence(key_env: str) -> dict:
    """Presence only — never the value. This is what every report uses."""
    if not key_env:
        return {"key": key_env, "present": True, "source": "not required"}
    if os.environ.get(key_env, "").strip():
        return {"key": key_env, "present": True, "source": "environment"}
    if len(_env_file_value(key_env)) > 1:
        return {"key": key_env, "present": True, "source": ".env"}
    # The rendered runtime env (v1.1.9). gateway-run.sh sources this 0600 file
    # before it execs the gateway, so a key present here genuinely IS available
    # to the runtime — even though it is absent from .env and
    # secrets.onepassword.enabled is deliberately false. Without this, the
    # manifest and the bootstrap summary reported
    #   provider anthropic unavailable (credential: 1Password map (disabled))
    # about a provider the gateway was authenticating with. Presence only.
    try:
        if (parse_env_file(runtime_secrets_path()).get(key_env) or "").strip():
            return {"key": key_env, "present": True, "source": "runtime env"}
    except OSError:
        pass
    cfg = load_yaml(CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    if key_env in (op.get("env") or {}):
        if op.get("enabled"):
            return {"key": key_env, "present": True, "source": "1Password map"}
        return {"key": key_env, "present": False, "source": "1Password map (disabled)"}
    return {"key": key_env, "present": False, "source": "unresolved"}


# ==========================================================================
# Hermes runtime  (v1.0.2)
# ==========================================================================
# Every tool that spawns `hermes` must hand it the SAME runtime initialisation
# the supervised gateway gets. Before v1.0.2 the routing CLI and the provider
# doctor spawned `hermes chat` with a bare inherited environment and none of
# the gateway's launch flags, so they exercised a different runtime than the
# one production actually uses — and hung.
#
# The two references, both already in this repo:
#
#   gateway-run.sh          export HOME/HERMES_HOME/PATH, then
#                           `set -a; . /root/.env; . ~/.hermes/.env; set +a`,
#                           then `hermes gateway run --replace --accept-hooks`
#   agentphone_bridge.py    env = os.environ | <parsed ~/.hermes/.env>,
#                           HERMES_YOLO_MODE=1,
#                           `hermes chat -Q --source … --max-turns N -q …`
#
# hermes_child_env() reproduces the first; hermes_chat_cmd() reproduces the
# second. Nothing here reads or prints a secret value: the .env files are
# parsed into the child's environment and never inspected further.
# ==========================================================================
ROOT_ENV = Path(os.environ.get("NICKS_STACK_ROOT_ENV", "/root/.env"))

# Identical to the PATH gateway-run.sh exports, so `hermes` and its helpers
# resolve the same way under supervisor, ssh and cron.
HERMES_PATH = "/usr/local/bin:/root/.local/bin:/root/.hermes/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def parse_env_file(path: Path | str) -> dict:
    """KEY=VALUE pairs from a shell env file, WITHOUT executing it.

    Equivalent to `set -a; . file; set +a` for the plain assignments these
    files contain, minus the arbitrary-code-execution that sourcing implies.
    Values are returned for the caller to put in a child environment; no
    caller logs them."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def hermes_child_env(extra: dict | None = None, yolo: bool = False,
                     profile: str | None = None) -> dict:
    """The environment `hermes` must be given, built exactly as the supervised
    gateway builds its own.

    Layering order matches gateway-run.sh literally: HOME/HERMES_HOME/PATH
    first, then /root/.env, then ~/.hermes/.env (a sourced file wins over the
    inherited value, which is what `set -a; . file` does).

    OP_SERVICE_ACCOUNT_TOKEN is added from ~/.hermes/.op.env when present.
    Without it, an enabled 1Password map makes `op read` prompt on /dev/tty —
    which is the documented ~30s-per-reference stall (config.yaml, secrets
    block) and, with 21 mapped references, a hang no 30s timeout survives.

    yolo=True sets HERMES_YOLO_MODE=1, the bridge's non-interactive setting.
    Only the validation paths (probes) pass it; a user-driven `route run`
    keeps the normal approval behaviour.

    profile selects a runtime profile (v1.0.3). It only ever repoints
    HERMES_HOME at a smaller config tree under ~/.hermes/profiles/<name>/ —
    the same `hermes` startup, less to load. A full profile, an unknown name
    or a profile that could not be built all fall back to the real
    HERMES_HOME, so a profile can never break a call."""
    env = dict(os.environ)
    env["HOME"] = str(HERMES_HOME.parent)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PATH"] = HERMES_PATH + ":" + os.environ.get("PATH", "")
    env.update(parse_env_file(ROOT_ENV))
    env.update(parse_env_file(HERMES_ENV))
    token = op_token()
    if token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    if yolo:
        env.setdefault("HERMES_YOLO_MODE", "1")
    if profile:
        # v1.0.4: apply exactly ONE selection layout, and only one that has
        # been MEASURED to isolate on this machine. v1.0.3 set HERMES_HOME and
        # HERMES_PROFILE together, which is self-defeating: if Hermes resolves
        # a named profile as $HERMES_HOME/profiles/$HERMES_PROFILE/, pointing
        # HERMES_HOME into the profile makes it look for
        # profiles/validation/profiles/validation/, find nothing, and fall
        # back to the default config — the full 21-reference secret map.
        env.update(profile_env_overrides(profile))
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ==========================================================================
# Runtime profiles  (v1.0.3)
# ==========================================================================
# A profile is a smaller config tree that the SAME `hermes` startup is pointed
# at. It is declared in routing.yaml (profiles:) and, for a derived profile,
# generated from the machine's real config.yaml — never hand-written — so it
# cannot drift from the runtime it is a subset of.
#
# Layout follows Hermes' own convention: ~/.hermes/profiles/<name>/, with the
# default profile living directly in ~/.hermes/
# (skills/autonomous-ai-agents/hermes-gateway-onboarding/SKILL.md:40-42).
#
# Nothing here patches, wraps or reimplements Hermes. The only lever used is
# which directory HERMES_HOME points at.
# ==========================================================================
PROFILES_DIR = HERMES_HOME / "profiles"
PROFILE_STAMP = ".derived-from.sha256"

# Blocks that would start something long-lived or stateful. Never carried into
# a derived profile even if a future config nests them somewhere new.
_NEVER_IN_DERIVED = ("mcp_servers", "plugins", "secrets", "platform_toolsets",
                     "known_plugin_toolsets", "memory", "browser", "skills",
                     "delegation", "image_gen", "stt", "code_execution",
                     "hooks_auto_accept", "session_reset")


PROBE_PROMPT = "Reply with exactly: ok"


def api_probe(vendor: str, *, model: str, key: str | None = None,
              base_url: str | None = None, max_tokens: int = 16, timeout: int = 30,
              prompt: str = PROBE_PROMPT) -> tuple[bool, str, str]:
    """One capped completion straight at a vendor API.  (v1.0.4)

    ONE implementation, used by both the provider doctor and `jack doctor
    --providers`, so a validation call cannot be capped in one tool and
    uncapped in the other.

    Every vendor here takes an explicit output cap and it is always set:
    Anthropic/OpenAI-compatible `max_tokens`, Gemini
    `generationConfig.maxOutputTokens`, Ollama `options.num_predict`. This is
    what keeps a credential check off OpenRouter's HTTP 402 — that account is
    charged against a request's MAXIMUM possible cost, so an uncapped probe
    advertising the model's full completion budget is rejected even when the
    probe would only ever use a handful of tokens.

    Returns (ok, detail, label). No secret is printed: `key` is used as a
    header and every error string is scrubbed."""
    if vendor == "anthropic":
        url = f"{(base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages"
        status, body, err = http_json(
            url, headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"},
            payload={"model": model, "max_tokens": max_tokens,
                     "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout)
        if status == 200:
            return True, "".join(b.get("text", "") for b in (body or {}).get("content", [])), url
        return False, f"HTTP {status} {scrub(err)[:90]}", url

    if vendor == "openai-compat":
        url = f"{(base_url or 'https://openrouter.ai/api/v1').rstrip('/')}/chat/completions"
        status, body, err = http_json(
            url, headers={"Authorization": f"Bearer {key or ''}"},
            payload={"model": model, "max_tokens": max_tokens,
                     "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout)
        if status == 200:
            choice = ((body or {}).get("choices") or [{}])[0]
            return True, (choice.get("message") or {}).get("content", ""), url
        return False, f"HTTP {status} {scrub(err)[:90]}", url

    if vendor == "gemini":
        base = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        url = f"{base}/models/{model}:generateContent"
        status, body, err = http_json(
            url, headers={"x-goog-api-key": key or ""},
            payload={"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"maxOutputTokens": max_tokens}},
            timeout=timeout)
        if status == 200:
            parts = (((body or {}).get("candidates") or [{}])[0]
                     .get("content", {}).get("parts", []))
            return True, "".join(p.get("text", "") for p in parts), url
        return False, f"HTTP {status} {scrub(err)[:90]}", url

    if vendor == "ollama":
        url = f"{(base_url or DEFAULT_OLLAMA_HOST).rstrip('/')}/api/generate"
        status, body, err = http_json(
            url, payload={"model": model, "prompt": prompt, "stream": False,
                          "options": {"num_predict": max_tokens}},
            timeout=timeout)
        if status == 200:
            return True, (body or {}).get("response", ""), url
        return False, f"HTTP {status} {scrub(err)[:90]}", url

    return False, f"no capped probe implemented for vendor '{vendor}'", ""


def vendor_for(provider: str) -> str:
    """Map a Hermes provider name onto the vendor request shape it speaks."""
    bare = (provider or "").replace("custom:", "").lower()
    if "anthropic" in bare:
        return "anthropic"
    if "gemini" in bare or "google" in bare:
        return "gemini"
    if "ollama" in bare:
        return "ollama"
    # OpenRouter and every declared custom provider use transport
    # chat_completions (config.yaml providers.*), i.e. the OpenAI shape.
    return "openai-compat"


def validation_spec(routing: dict | None = None) -> dict:
    """The validation cost policy from routing.yaml (v1.0.4).

    max_tokens caps every probe that goes to a vendor API. `inference` selects
    which leg validation uses: the capped API call (cheap, the default), the
    Hermes runtime leg (uncapped — no verified way exists to bound it), or
    both. Normal routing never reads this."""
    routing = routing if routing is not None else load_yaml(ROUTING_FILE)
    spec = routing.get("validation")
    spec = spec if isinstance(spec, dict) else {}
    try:
        max_tokens = int(spec.get("max_tokens", 16))
    except (TypeError, ValueError):
        max_tokens = 16
    mode = str(spec.get("inference", "api")).lower()
    return {
        # Clamped: below ~8 a model cannot answer at all; above 64 a probe is
        # no longer "the smallest practical completion".
        "max_tokens": max(8, min(max_tokens, 64)),
        "inference": mode if mode in ("api", "hermes", "both") else "api",
    }


def profiles_spec(routing: dict | None = None) -> dict:
    """The profiles: block from routing.yaml, or {} when none is declared."""
    routing = routing if routing is not None else load_yaml(ROUTING_FILE)
    spec = routing.get("profiles")
    return spec if isinstance(spec, dict) else {}


def profile_for(kind: str, routing: dict | None = None) -> str | None:
    """Which profile a kind of work runs in ('validation', 'routing_run',
    'gateway'). Returns None when profiles are not declared, which means
    'use the full runtime' — the pre-v1.0.3 behaviour."""
    spec = profiles_spec(routing)
    name = (spec.get("use") or {}).get(kind)
    if not name or name not in spec:
        return None
    if (spec[name] or {}).get("kind") == "full":
        return None          # 'full' is the real HERMES_HOME; nothing to build
    return name


def _routing_key_envs(cfg: dict, routing: dict) -> set:
    """Every credential name this routing map actually addresses. Trimming the
    1Password map to these is the single biggest cold-start saving: 21 op://
    references become the handful inference needs."""
    keys = set()
    for block in ("providers", "modes"):
        for spec in (routing.get(block) or {}).values():
            if (spec or {}).get("key_env"):
                keys.add(spec["key_env"])
    for spec in (routing.get("providers") or {}).values():
        # A custom provider declared in config.yaml carries its own key_env.
        name = ((spec or {}).get("provider") or "").replace("custom:", "")
        cfg_provider = (cfg.get("providers") or {}).get(name) or {}
        if cfg_provider.get("key_env"):
            keys.add(cfg_provider["key_env"])
    return keys


def _provider_plugins(cfg: dict, routing: dict) -> list:
    """Provider plugins only. Computed, not hardcoded: every enabled plugin
    whose name ends in `-provider`, plus any enabled plugin named exactly like
    a provider this routing map addresses. Tool plugins (browser, web, spotify,
    orgo-desktop-local, telemetry) are dropped."""
    enabled = (cfg.get("plugins") or {}).get("enabled") or []
    wanted = set()
    for spec in (routing.get("providers") or {}).values():
        p = ((spec or {}).get("provider") or "")
        if p and not p.startswith("custom:"):
            wanted.add(p)
    for spec in (routing.get("modes") or {}).values():
        p = ((spec or {}).get("provider") or "")
        if p and not p.startswith("custom:"):
            wanted.add(p)
    return [p for p in enabled if p.endswith("-provider") or p in wanted]


def derive_profile_config(cfg: dict, routing: dict, spec: dict) -> dict:
    """Build the derived config dict for a profile. Pure function of the real
    config + the routing map, so `jack profiles --diff` can show exactly what
    was dropped and why."""
    include = (spec or {}).get("include") or {}
    out: dict = {}

    for block in include.get("blocks") or []:
        if block in _NEVER_IN_DERIVED:
            continue                      # declaration cannot override safety
        if block in cfg:
            out[block] = cfg[block]

    if include.get("plugins") == "providers-only":
        keep = _provider_plugins(cfg, routing)
        out["plugins"] = {"enabled": keep,
                          "disabled": (cfg.get("plugins") or {}).get("disabled") or []}
    if include.get("mcp_servers") is False:
        out["mcp_servers"] = {}

    toolsets = include.get("toolsets")
    if isinstance(toolsets, list):
        # Every surface gets the same (empty) toolset list: a probe calls no
        # tools, so none need to be constructed at start.
        out["platform_toolsets"] = {s: list(toolsets)
                                    for s in (cfg.get("platform_toolsets") or {"cli": []})}

    if include.get("secrets") == "routing-keys":
        op = ((cfg.get("secrets") or {}).get("onepassword") or {})
        wanted = _routing_key_envs(cfg, routing)
        trimmed = {k: v for k, v in (op.get("env") or {}).items() if k in wanted}
        keep = {k: v for k, v in op.items() if k != "env"}
        keep["env"] = trimmed
        out["secrets"] = {"onepassword": keep}

    # DECLARED OVERRIDES (v1.1.34). Everything above SUBSETS the real config;
    # this is the one place a profile may state a different VALUE. Added for
    # `local`, which needs agent.reasoning_effort off because Ollama returns
    # HTTP 400 "does not support thinking" for models that lack it — and
    # turning it off globally would degrade the Anthropic tiers instead.
    #
    # Deep-merged one level so a profile can change a single key without
    # restating the whole block, and applied AFTER the includes so it always
    # wins. _NEVER_IN_DERIVED still applies: a declaration cannot override
    # safety.
    for block, value in ((spec or {}).get("overrides") or {}).items():
        if block in _NEVER_IN_DERIVED:
            continue
        if isinstance(value, dict) and isinstance(out.get(block), dict):
            merged = dict(out[block]); merged.update(value); out[block] = merged
        elif isinstance(value, dict) and isinstance(cfg.get(block), dict):
            merged = dict(cfg[block]); merged.update(value); out[block] = merged
        else:
            out[block] = value

    if include.get("ephemeral"):
        # No memory writes, no session reset machinery, no onboarding prompts.
        out["memory"] = {"memory_enabled": False, "user_profile_enabled": False}
        out["onboarding"] = (cfg.get("onboarding") or {})
        out["streaming"] = {"enabled": False}

    return out


def profile_paths(name: str, spec: dict | None = None) -> Path | None:
    """Where a profile's config tree lives. `dir` is declared in routing.yaml
    and is always relative to HERMES_HOME; a value that would resolve outside
    HERMES_HOME is rejected rather than followed, so a typo cannot make the
    platform write a config tree somewhere unexpected."""
    spec = spec if spec is not None else profiles_spec().get(name) or {}
    rel = spec.get("dir") or f"profiles/{name}"
    if os.path.isabs(rel):
        return None
    path = (HERMES_HOME / rel).resolve()
    home = HERMES_HOME.resolve()
    if path == home or home not in path.parents:
        return None
    return path


def profile_ensure(name: str, force: bool = False) -> dict:
    """Create or refresh a derived profile, and return what happened.

    Rebuilds whenever the source config.yaml changes (a sha256 of the derived
    content is stamped beside it), so the profile can never describe a runtime
    the machine no longer has. Returns {'path': None, ...} for anything that
    should run on the full runtime — the caller then simply uses HERMES_HOME."""
    import hashlib

    result = {"profile": name, "path": None, "built": False, "reason": ""}
    routing = load_yaml(ROUTING_FILE)
    spec = profiles_spec(routing).get(name) or {}
    if not spec:
        result["reason"] = f"no profile '{name}' declared in {ROUTING_FILE.name}"
        return result
    if spec.get("kind") == "full":
        result["reason"] = "full runtime — nothing to derive"
        return result

    cfg = load_yaml(CONFIG_FILE)
    if not cfg:
        result["reason"] = f"{CONFIG_FILE} unreadable — falling back to the full runtime"
        return result

    derived = derive_profile_config(cfg, routing, spec)
    body = yaml.safe_dump(derived, default_flow_style=False, sort_keys=False,
                          allow_unicode=True, width=1000)
    digest = hashlib.sha256(body.encode()).hexdigest()

    path = profile_paths(name, spec)
    if path is None:
        result["reason"] = (f"profile '{name}' declares dir='{spec.get('dir')}', which is not "
                            f"inside {HERMES_HOME} — refusing to build it")
        return result
    stamp = path / PROFILE_STAMP
    try:
        if not force and stamp.is_file() and stamp.read_text().strip() == digest \
                and (path / "config.yaml").is_file():
            result.update(path=str(path), reason="up to date")
            _profile_link(path, spec)      # links are cheap; keep them correct
            return result

        path.mkdir(parents=True, exist_ok=True)
        # The tree sits inside HERMES_HOME (0700) and links to secret files;
        # keep it just as closed as the directory it derives from.
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o700)
        tmp = path / "config.yaml.tmp"
        tmp.write_text(body)
        os.chmod(tmp, 0o600)
        tmp.replace(path / "config.yaml")
        _profile_link(path, spec)
        stamp.write_text(digest + "\n")
        os.chmod(stamp, 0o600)
        result.update(path=str(path), built=True, reason="rebuilt from config.yaml")
        return result
    except OSError as exc:
        # A profile is an optimisation. If it cannot be written, say so and
        # let the caller run on the full runtime.
        result["reason"] = f"could not build profile: {exc}"
        result["path"] = None
        return result


def _profile_link(path: Path, spec: dict) -> None:
    """Symlink credentials and identity in from the real profile. Linked, not
    copied — no secret is duplicated on disk, and permissions stay the
    originals'."""
    # `.hermes` -> the profile directory itself, so a resolver that derives the
    # config dir from $HOME ($HOME/.hermes/config.yaml) lands on the same
    # derived config as one that reads HERMES_HOME. Costs one symlink and lets
    # the layout detector test that possibility.  (v1.0.4)
    selflink = path / ".hermes"
    try:
        if not selflink.exists() and not selflink.is_symlink():
            selflink.symlink_to(path)
    except OSError:
        pass

    for rel in spec.get("link") or []:
        src, dst = HERMES_HOME / rel, path / rel
        try:
            if not src.exists():
                if dst.is_symlink() and not dst.exists():
                    dst.unlink()          # source went away; drop the dangling link
                continue
            if dst.is_symlink():
                if os.readlink(dst) == str(src):
                    continue
                dst.unlink()
            elif dst.exists():
                continue                  # a real file here was put there deliberately
            dst.symlink_to(src)
        except OSError:
            continue                      # a missing link is not fatal


# --------------------------------------------------------------------------
# Profile selection: DECLARED, and read from YAML only  (v1.0.5)
# --------------------------------------------------------------------------
# v1.0.4 measured the selection layout by running
# `hermes config get secrets.onepassword.env` once per candidate. That was the
# wrong tool for the job: on this Hermes a `config get` is not a cheap file
# read — the CLI performs its full startup (resolve every op:// reference,
# load plugins, connect MCP servers, start adapters) BEFORE printing the value.
# So a configuration read cost a complete runtime boot, and a candidate that
# did not isolate booted the DEFAULT runtime: 21 secret resolutions, MCP OAuth
# attempts against Composio / Agent Cards / Linear / X / Vidiq / Latitude, and
# a Telegram adapter start. That is upstream CLI behaviour and is not something
# to work around — the fix is to stop asking Hermes questions that a YAML file
# already answers.
#
# The layout is therefore DECLARED in routing.yaml (profiles.selection) and
# resolved here by parsing files, never by starting Hermes. The default,
# `hermes-home`, is not a guess: it is what the real VM reported before this
# call was removed. Nothing in this module starts Hermes to read configuration.
PROFILE_LAYOUTS = ("profile-var", "hermes-home", "home-root")
LAYOUT_STAMP = ".layout.json"


def profile_layout_env(name: str, layout: str, path: Path) -> dict:
    """The environment overrides for one selection layout."""
    if layout == "profile-var":
        # Native convention: HERMES_HOME stays the real config root and the
        # profile is named. Hermes looks in HERMES_HOME/profiles/<name>/.
        return {"HERMES_PROFILE": name}
    if layout == "hermes-home":
        # Config-root repoint: the profile directory IS the config root, so a
        # profile name would be meaningless and must not be set. Setting both
        # is what broke v1.0.3.
        return {"HERMES_HOME": str(path), "HERMES_PROFILE": "default"}
    if layout == "home-root":
        # For a resolver that derives the config dir from $HOME. The profile
        # carries a `.hermes` self-link so $HOME/.hermes/config.yaml lands on
        # the same derived file.
        return {"HOME": str(path), "HERMES_HOME": str(path / ".hermes"),
                "HERMES_PROFILE": "default"}
    return {}


def _base_child_env() -> dict:
    """The v1.0.2 bridged environment, with no profile selection applied."""
    env = dict(os.environ)
    env["HOME"] = str(HERMES_HOME.parent)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PATH"] = HERMES_PATH + ":" + os.environ.get("PATH", "")
    env.update(parse_env_file(ROOT_ENV))
    env.update(parse_env_file(HERMES_ENV))
    token = op_token()
    if token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    return env


def profile_expected_refs(name: str) -> int:
    """How many op:// references the derived profile declares. YAML only."""
    routing = load_yaml(ROUTING_FILE)
    spec = profiles_spec(routing).get(name) or {}
    derived = derive_profile_config(load_yaml(CONFIG_FILE), routing, spec)
    return len(((derived.get("secrets") or {}).get("onepassword") or {}).get("env") or {})


def profile_detect_layout(name: str, force: bool = False) -> dict:
    """Which selection layout this machine uses, from routing.yaml.

    Reads files only — it never starts Hermes. `force` is accepted so callers
    written against v1.0.4 keep working; there is nothing to re-measure."""
    built = profile_ensure(name)
    path = built.get("path")
    if not path:
        return {"layout": None, "isolated": False, "source": "declared",
                "reason": built.get("reason", ""), "attempts": []}

    layout = str(profiles_spec().get("selection") or "hermes-home")
    if layout not in PROFILE_LAYOUTS:
        return {"layout": None, "isolated": False, "source": "declared",
                "reason": (f"routing.yaml profiles.selection='{layout}' is not one of "
                           f"{', '.join(PROFILE_LAYOUTS)} — probes use the full runtime"),
                "attempts": []}

    # The only check worth making, and it needs no subprocess: does the config
    # this layout points Hermes at actually exist?
    target = (Path(path) / ".hermes" / "config.yaml") if layout == "home-root" \
        else (Path(path) / "config.yaml")
    if not target.is_file():
        return {"layout": layout, "isolated": False, "source": "declared",
                "reason": f"layout '{layout}' expects {target}, which is not there",
                "attempts": []}

    return {
        "layout": layout,
        "isolated": True,
        "source": "declared in routing.yaml (profiles.selection)",
        "expected_refs": profile_expected_refs(name),
        "reason": f"layout '{layout}' declared; {target} present",
        "attempts": [],
    }


def profile_env_overrides(name: str) -> dict:
    """Environment overrides for a profile, or {} to use the full runtime."""
    built = profile_ensure(name)
    if not built.get("path"):
        return {}
    detected = profile_detect_layout(name)
    if not detected.get("isolated"):
        return {}
    return profile_layout_env(name, detected["layout"], Path(built["path"]))


def profile_report(name: str | None = None) -> dict:
    """What each declared profile is and what the derived one drops. Names and
    counts only — no secret value is read or emitted."""
    routing = load_yaml(ROUTING_FILE)
    cfg = load_yaml(CONFIG_FILE)
    spec_all = profiles_spec(routing)
    full_mcp = len(cfg.get("mcp_servers") or {})
    full_plugins = len((cfg.get("plugins") or {}).get("enabled") or [])
    full_refs = len(((cfg.get("secrets") or {}).get("onepassword") or {}).get("env") or {})

    out = {
        "declared": sorted(k for k in spec_all if k != "use"),
        "use": spec_all.get("use") or {},
        "full_runtime": {"mcp_servers": full_mcp, "plugins": full_plugins,
                         "op_references": full_refs},
        "profiles": {},
    }
    for pname, spec in spec_all.items():
        if pname == "use" or not isinstance(spec, dict):
            continue
        entry = {"kind": spec.get("kind"), "display": spec.get("display", pname)}
        if spec.get("kind") == "full":
            entry.update(mcp_servers=full_mcp, plugins=full_plugins,
                         op_references=full_refs, path=str(HERMES_HOME))
        else:
            derived = derive_profile_config(cfg, routing, spec)
            path = profile_paths(pname, spec)
            entry.update(
                mcp_servers=len(derived.get("mcp_servers") or {}),
                plugins=len((derived.get("plugins") or {}).get("enabled") or []),
                plugin_names=(derived.get("plugins") or {}).get("enabled") or [],
                op_references=len(((derived.get("secrets") or {})
                                   .get("onepassword") or {}).get("env") or {}),
                op_reference_names=sorted(((derived.get("secrets") or {})
                                           .get("onepassword") or {}).get("env") or {}),
                path=str(path) if path else "(rejected: outside HERMES_HOME)",
            )
            # Layout comes from routing.yaml and is resolved by reading
            # files. No Hermes process is started to answer this.  (v1.0.5)
            # This also ensures the profile, so `built` is read afterwards.
            sel = profile_detect_layout(pname) if path else {}
            entry.update(
                built=bool(path and (path / "config.yaml").is_file()),
                isolated=sel.get("isolated"),
                selection_layout=sel.get("layout"),
                selection_source=sel.get("source", "declared"),
                isolation_detail=sel.get("reason", ""),
                isolation_attempts=[],
            )
        out["profiles"][pname] = entry
    if name:
        return out["profiles"].get(name, {})
    return out


def hermes_chat_cmd(prompt: str, *, source: str, model: str | None = None,
                    provider: str | None = None, toolsets: str | None = None,
                    max_turns: int | None = None,
                    accept_hooks: bool = True) -> list[str]:
    """The one-shot invocation shape this platform actually runs in production.

    `--accept-hooks` is not optional for an unattended caller: without it a
    plugin/hook approval prompt blocks on a TTY that a supervised or piped
    process does not have. gateway-run.sh passes it for the same reason
    ("plugins/hooks never prompt"), and the documented working one-shot
    (skills/observability/latitude-agent-review) passes it too.

    `--max-turns` bounds an agentic turn; the bridge always sets it."""
    cmd = ["hermes", "chat", "-Q", "--source", source]
    if accept_hooks:
        cmd.append("--accept-hooks")
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if model:
        cmd += ["-m", str(model)]
    if provider:
        cmd += ["--provider", str(provider)]
    cmd += ["-q", prompt]
    if toolsets:
        cmd += ["-t", toolsets]
    return cmd


def hermes_run(cmd: list[str], timeout: int, env: dict | None = None):
    """Run a hermes child in its own process group and reap the WHOLE group.

    v1.0.5: subprocess.run(timeout=…) kills only the direct child. Hermes
    spawns its MCP servers as its own children, so killing `hermes` left those
    running — reparented to init and still authenticating. That is why MCP
    warnings (Composio, Agent Cards, Linear, X, Vidiq, Latitude) kept appearing
    after the provider doctor had already exited.

    start_new_session=True puts the child in a new process group; on timeout the
    group is signalled, so nothing survives the call. Raises TimeoutExpired like
    subprocess.run, so callers are unchanged."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL, start_new_session=True,
        env=env if env is not None else hermes_child_env())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_group(proc) -> None:
    """TERM then KILL the child's whole process group, then the child itself."""
    import signal
    import time
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        if sig is signal.SIGTERM:
            time.sleep(2)
            if proc.poll() is not None:
                return
    try:
        proc.kill()
    except OSError:
        pass


# Startup/config failures — the signatures that mean "the profile is the
# problem", as opposed to a genuine provider or credential failure. Only these
# trigger the one retry on the full runtime.
_PROFILE_BAILOUT = ("config", "no such file", "not found", "unknown provider",
                    "no provider", "missing", "cannot open", "traceback",
                    "unsupported", "invalid configuration")


def hermes_probe(prompt: str, *, source: str, timeout: int,
                 model: str | None = None, provider: str | None = None,
                 profile: str | None = None, key_env: str | None = None,
                 max_turns: int | None = 1) -> dict:
    """One unattended completion, in a runtime profile when one is declared.

    This is the single entry point every validation path uses, so the profile
    policy, the fallback and the diagnosis live in exactly one place.

    A profile is an optimisation and is treated as one: if the probe fails for
    a startup/config reason, it is retried once on the full runtime and the
    result says the profile was bypassed. A profile can make validation
    faster; it can never make it fail.

    Returns {ok, detail, profile, fell_back, timed_out}."""
    cmd = hermes_chat_cmd(prompt, source=source, model=model,
                          provider=provider, max_turns=max_turns)

    def attempt(prof: str | None) -> dict:
        try:
            proc = hermes_run(cmd, timeout, env=hermes_child_env(yolo=True, profile=prof))
        except subprocess.TimeoutExpired:
            findings = hermes_runtime_diagnose(key_env)
            hint = f" — {findings[0]}" if findings else ""
            return {"ok": False, "detail": f"hermes timed out after {timeout}s{hint}",
                    "timed_out": True}
        except OSError as exc:
            return {"ok": False, "detail": f"hermes could not run: {exc}", "timed_out": False}
        if proc.returncode == 0 and proc.stdout.strip():
            return {"ok": True, "detail": scrub(proc.stdout), "timed_out": False}
        return {"ok": False, "timed_out": False,
                "detail": scrub(proc.stderr or proc.stdout or f"exit {proc.returncode}")}

    first = attempt(profile)
    first.update(profile=profile or "gateway", fell_back=False)
    if first["ok"] or not profile:
        return first

    blob = (first["detail"] or "").lower()
    if first.get("timed_out") or any(w in blob for w in _PROFILE_BAILOUT):
        second = attempt(None)
        if second["ok"]:
            second.update(profile="gateway", fell_back=True,
                          detail=f"{second['detail']}  [profile '{profile}' bypassed: "
                                 f"{first['detail'][:80]}]")
            return second
        # Both failed — report the full-runtime failure, which is the real one.
        second.update(profile="gateway", fell_back=True)
        return second
    return first


def hermes_runtime_diagnose(key_env: str | None = None) -> list[str]:
    """Why a fresh `hermes chat` may stall where the long-running gateway does
    not. Returns human-readable findings, most-likely cause first. Presence
    only — no secret value is read, resolved or printed."""
    findings: list[str] = []
    cfg = load_yaml(CONFIG_FILE)
    env = hermes_child_env()

    if not have("hermes"):
        findings.append("hermes is not on PATH for this process")
        return findings

    # 1. 1Password: an enabled map with no token prompts on /dev/tty, once per
    #    reference. This is the single largest cold-start stall.
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    refs = len(op.get("env") or {})
    if op.get("enabled"):
        if not env.get("OP_SERVICE_ACCOUNT_TOKEN"):
            findings.append(
                f"1Password map is ENABLED with {refs} references but no "
                f"OP_SERVICE_ACCOUNT_TOKEN is reachable ({OP_ENV} missing or empty) — "
                f"`op read` then prompts on /dev/tty and every hermes start stalls"
            )
        elif not have("op"):
            findings.append(
                f"1Password map is ENABLED with {refs} references but the `op` binary "
                f"is not installed — every reference fails slowly at start"
            )
        else:
            # A validation profile trims the map to the credentials inference
            # actually needs, so say what a probe really pays.  (v1.0.3)
            prof = profile_for("validation")
            report = profile_report(prof) if prof else {}
            # Only claim the saving when the profile is MEASURED to be in
            # effect — a built-but-ignored profile saves nothing.  (v1.0.4)
            lean = report.get("op_references") if report.get("isolated") else None
            if lean is not None and lean < refs:
                findings.append(
                    f"1Password map enabled, token and `op` present — the full runtime "
                    f"resolves {refs} references, validation profile '{prof}' resolves {lean}"
                )
            else:
                findings.append(
                    f"1Password map enabled, {refs} references, token and `op` present — "
                    f"a cold start still pays one `op read` per unresolved reference"
                )

    # 2. Credentials: a key that only exists in the map costs a cold start an
    #    op round-trip; one already in .env costs nothing.
    if key_env:
        if env.get(key_env):
            findings.append(f"{key_env} is present in the child environment (from .env)")
        else:
            findings.append(
                f"{key_env} is NOT in the child environment — hermes must resolve it "
                f"through the 1Password map at start"
            )

    # 3. Cold start cost: a fresh `hermes chat` boots the whole MCP/plugin
    #    stack; the gateway paid that once and keeps it warm. A validation
    #    profile is what removes this, so report whether one is in use.
    mcp = cfg.get("mcp_servers") or cfg.get("mcpServers") or {}
    enabled = [n for n, s in mcp.items() if isinstance(s, dict) and s.get("enabled", True)]
    timeouts = [float(s.get("connect_timeout", 0) or 0)
                for s in mcp.values() if isinstance(s, dict)]
    if enabled:
        prof = profile_for("validation")
        if prof:
            built = profile_ensure(prof)
            isolated = built.get("path") and profile_detect_layout(prof).get("isolated")
            if isolated:
                findings.append(
                    f"the full runtime enables {len(enabled)} MCP server(s) (slowest "
                    f"connect_timeout={max(timeouts or [0]):.0f}s), but validation runs in "
                    f"profile '{prof}' with none of them — this is not the stall"
                )
            elif built.get("path"):
                findings.append(
                    f"validation profile '{prof}' is built but hermes does NOT honour it, so "
                    f"probes still pay the full runtime: {len(enabled)} MCP server(s), "
                    f"slowest connect_timeout={max(timeouts or [0]):.0f}s"
                )
            else:
                findings.append(
                    f"validation profile '{prof}' is NOT usable ({built.get('reason')}), so "
                    f"probes fall back to the full runtime: {len(enabled)} MCP server(s), "
                    f"slowest connect_timeout={max(timeouts or [0]):.0f}s"
                )
        else:
            findings.append(
                f"{len(enabled)} MCP server(s) are enabled and no validation profile is "
                f"declared; the slowest declares connect_timeout={max(timeouts or [0]):.0f}s — "
                f"a cold `hermes chat` pays that startup, the running gateway does not"
            )

    # 4. The gateway itself: if it is up, the runtime is fine and the
    #    difference really is the cold start, not the configuration.
    gw = supervisor_status().get("hermes-gateway") or {}
    if gw.get("state") == "RUNNING":
        findings.append(
            "hermes-gateway is RUNNING — the same config.yaml works in a warm "
            "process, so the failure is specific to cold one-shot startup"
        )
    elif gw:
        findings.append(f"hermes-gateway is {gw.get('state')} — fix the service first")

    if not HERMES_ENV.exists():
        findings.append(f"{HERMES_ENV} does not exist — no keys can be bridged into hermes")
    if not (cfg.get("model") or {}).get("default"):
        findings.append(f"{CONFIG_FILE} declares no model.default")
    return findings


# --------------------------------------------------------------------------
# Services (Supervisor is this platform's init for services, not systemd)
# --------------------------------------------------------------------------
def init_system() -> str:
    try:
        return Path("/proc/1/comm").read_text().strip()
    except OSError:
        return "unknown"


def supervisor_status() -> dict:
    """{program: {state, detail}} for every program supervisord knows."""
    if not have("supervisorctl"):
        return {}
    try:
        proc = subprocess.run(["supervisorctl", "status"], capture_output=True,
                              text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = {"state": parts[1], "detail": " ".join(parts[2:])}
    return out


def services_detect() -> dict:
    status = supervisor_status()
    expected = ["hermes-gateway", "agentphone-bridge", "ollama"]
    services = {}
    for name in expected:
        if name in status:
            services[name] = {"configured": True, **status[name]}
        else:
            services[name] = {"configured": False, "state": "NOT CONFIGURED", "detail": ""}
    return {
        "init_system": init_system(),
        "supervisor_available": have("supervisorctl"),
        "supervisor_responding": bool(status),
        "programs": services,
    }


# --------------------------------------------------------------------------
# Ollama — the single implementation every tool uses
# --------------------------------------------------------------------------
def _routing_ollama_host() -> str:
    """routing.yaml modes.local host/port, or "" when not configured.

    ADDED v1.1.34 because the reporting lied. The `local` mode moved to the
    Mac mini (reached on a forwarded port), routing.yaml said so, and
    `nicks-stack-route show local` still printed the models on THIS VM —
    qwen3:1.7b and qwen3:4b, neither of which local mode would ever use. The
    route map and the availability line have to read the same address or the
    report is worse than none."""
    mode = ((load_yaml(ROUTING_FILE).get("modes") or {}).get("local") or {})
    host = str(mode.get("host") or "").strip()
    if not host:
        return ""
    if not host.startswith("http"):
        host = f"http://{host}"
    port = mode.get("port")
    if port and ":" not in host.split("//", 1)[-1]:
        host = f"{host}:{int(port)}"
    return host.rstrip("/")


def ollama_host(spec: dict | None = None) -> str:
    # PRECEDENCE, and it matters. routing.yaml is the declared map of where
    # `local` runs, so it outranks ~/.hermes/.env — which carried a stale
    # 127.0.0.1:11434 from when Ollama ran on this VM and silently won,
    # producing a route that named llama3.1:8b while reporting the VM's toy
    # models as available. An explicitly exported OLLAMA_HOST still wins, for
    # one-off testing.
    #
    # THE PORT IS SEPARATE FROM THE HOST in routing.yaml, and callers pass the
    # mode dict straight through as `spec`. So a spec host of "127.0.0.1"
    # arrives with its port sitting in a sibling key, and joining them here is
    # the whole reason this function exists — the first version dropped the
    # port and reported "no daemon at http://127.0.0.1".
    spec = spec or {}
    port = spec.get("port")
    host = (os.environ.get("OLLAMA_HOST", "").strip()
            or str(spec.get("host") or "").strip()
            or _routing_ollama_host()
            or _env_file_value("OLLAMA_HOST")
            or DEFAULT_OLLAMA_HOST)
    if not host.startswith("http"):
        host = f"http://{host}"
    if port and ":" not in host.split("//", 1)[-1]:
        host = f"{host}:{int(port)}"
    return host.rstrip("/")


def cpu_steal_percent(sample_seconds: float = 3.0) -> float:
    """Percentage of this VM's CPU time taken by the hypervisor, or -1.0.

    WHY A PLATFORM CARES (v1.1.11). Local inference on this box failed as
    "Ollama inference: HTTP 0 timed out", which reads like a broken daemon. It
    was not: `ollama ps` showed the model resident, llama.cpp had allocated its
    KV cache and was mid-warmup, and it simply never got scheduled. /proc/stat
    told the real story — 92% steal, 0.3% idle. The VM wants CPU essentially all
    the time and receives about 7% of one core.

    No amount of configuration fixes that, so the platform should name it
    instead of blaming Ollama. A timeout with high steal is a capacity fact
    about the host; a timeout with low steal is a real local fault.

    Returns -1.0 where /proc/stat is unavailable or unparseable (non-Linux, or a
    kernel without the steal column) so callers can skip the check rather than
    report a misleading 0.
    """
    def _snapshot() -> list[int] | None:
        try:
            with open("/proc/stat") as fh:
                fields = fh.readline().split()
        except OSError:
            return None
        if len(fields) < 9 or fields[0] != "cpu":
            return None
        try:
            return [int(v) for v in fields[1:9]]
        except ValueError:
            return None

    first = _snapshot()
    if first is None:
        return -1.0
    time.sleep(max(0.2, sample_seconds))
    second = _snapshot()
    if second is None:
        return -1.0
    delta = [b - a for a, b in zip(first, second)]
    total = sum(delta)
    if total <= 0:
        return -1.0
    return round(100.0 * delta[7] / total, 1)


# Above this, the host is not giving this VM enough CPU to run a local model.
# Chosen from the measured failure: 92% steal could not complete a 1.7B warmup
# in 20 minutes, while normal shared hosting sits in the low single digits.
CPU_STEAL_CRITICAL = 40.0


def ollama_detect(spec: dict | None = None, timeout: int = 8) -> dict:
    host = ollama_host(spec)
    info = {
        "host": host,
        "installed": have("ollama"),
        "serving": False,
        "models": [],
        "chat_models": [],
        "embedding_models": [],
        "supervisor_state": supervisor_status().get("ollama", {}).get("state", ""),
        "detail": "",
    }
    status, body, err = http_json(f"{host}/api/tags", timeout=timeout)
    if status != 200:
        info["detail"] = f"{host} did not answer (HTTP {status})" if status else f"no daemon at {host}"
        return info
    info["serving"] = True
    info["models"] = [m.get("name", "") for m in (body or {}).get("models", []) if m.get("name")]
    info["chat_models"] = [m for m in info["models"] if is_chat_model(m)]
    info["embedding_models"] = [m for m in info["models"] if not is_chat_model(m)]
    if not info["models"]:
        info["detail"] = "running, but no model is pulled"
    elif not info["chat_models"]:
        info["detail"] = "only embedding models installed — pull a chat model (ollama pull qwen3:4b)"
    else:
        info["detail"] = f"{len(info['chat_models'])} chat model(s), {len(info['embedding_models'])} embedding"
    return info


def ollama_pick_model(spec: dict | None = None, detected: dict | None = None) -> str:
    """Preferred chat model: routing.yaml pin first, else first chat-capable."""
    info = detected if detected is not None else ollama_detect(spec)
    preferred = [m for m in ((spec or {}).get("models") or []) if is_chat_model(m)]
    for want in preferred:
        if want in info["chat_models"]:
            return want
    return info["chat_models"][0] if info["chat_models"] else ""


# --------------------------------------------------------------------------
# Anthropic model resolution  (v1.0.1 bug 6)
# --------------------------------------------------------------------------
# Model aliases go stale. Rather than pinning ids in routing.yaml, each
# Anthropic-backed mode declares a TIER (haiku / sonnet / opus) and the current
# id is resolved from the vendor's own catalog, newest first. The pinned
# `model:` value stays as the offline fallback.
ANTHROPIC_TIERS = ("haiku", "sonnet", "opus")
ANTHROPIC_CACHE = HERMES_HOME / "state" / "anthropic-models.json"
ANTHROPIC_CACHE_TTL = 86400  # seconds


def anthropic_catalog(key: str, timeout: int = 30) -> tuple[list[dict], str]:
    status, body, err = http_json(
        "https://api.anthropic.com/v1/models?limit=100",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=timeout,
    )
    if status != 200:
        return [], f"HTTP {status} {scrub(err)[:120]}"
    return (body or {}).get("data") or [], ""


def _tier_of(model_id: str) -> str:
    lowered = (model_id or "").lower()
    for tier in ANTHROPIC_TIERS:
        if tier in lowered:
            return tier
    return ""


def anthropic_resolve_tiers(key: str | None = None, timeout: int = 30,
                            refresh: bool = False) -> dict:
    """{'haiku': id, 'sonnet': id, 'opus': id, 'source': ..., 'error': ...}

    Cached for 24h under state/ so the routing CLI stays fast and offline-safe.
    """
    now = int(__import__("time").time())
    if not refresh and ANTHROPIC_CACHE.is_file():
        try:
            cached = json.loads(ANTHROPIC_CACHE.read_text())
            if now - int(cached.get("fetched_at", 0)) < ANTHROPIC_CACHE_TTL:
                cached["source"] = "cache"
                return cached
        except (OSError, ValueError):
            pass

    if key is None:
        key, _ = resolve_key_value("ANTHROPIC_API_KEY")
    if not key:
        return {"source": "unresolved", "error": "ANTHROPIC_API_KEY not resolvable"}

    models, err = anthropic_catalog(key, timeout)
    if err:
        return {"source": "error", "error": err}

    # Newest first. The catalog is ordered newest-first already; created_at is
    # used when present so the choice is deterministic either way.
    ordered = sorted(models, key=lambda m: str(m.get("created_at") or ""), reverse=True)
    resolved: dict = {"fetched_at": now, "source": "live", "error": ""}
    for entry in ordered:
        mid = entry.get("id") or ""
        if "fast" in mid.lower():      # speed variants are not distinct models here
            continue
        tier = _tier_of(mid)
        if tier and tier not in resolved:
            resolved[tier] = mid
    resolved["available"] = [m.get("id") for m in ordered if m.get("id")]
    try:
        ANTHROPIC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ANTHROPIC_CACHE.write_text(json.dumps(resolved, indent=2) + "\n")
    except OSError:
        pass
    return resolved


def resolve_model(spec: dict, tiers: dict | None = None) -> tuple[str, str]:
    """(model_id, how). Honours an explicit tier for Anthropic routes and falls
    back to the pinned id when the catalog is unavailable."""
    pinned = spec.get("model") or ""
    tier = spec.get("tier") or ""
    provider = spec.get("provider") or ""
    if not tier or "anthropic" not in provider:
        return pinned, "pinned"
    tiers = tiers if tiers is not None else anthropic_resolve_tiers()
    live = tiers.get(tier)
    if live:
        return live, f"resolved live ({tier})"
    if pinned:
        return pinned, f"pinned fallback ({tiers.get('error') or 'catalog unavailable'})"
    return "", f"unresolved ({tiers.get('error') or 'no catalog'})"


# --------------------------------------------------------------------------
# Failure classification  (v1.0.1 bug 7)
# --------------------------------------------------------------------------
def classify_failure(text: str, status: int | None = None) -> tuple[str, str]:
    """(category, suggested action) for a provider failure. Categories:
    credential | network | model-id | hermes-config | provider-error."""
    blob = (text or "").lower()
    if status in (401, 403) or any(w in blob for w in
                                   ("unauthorized", "invalid api key", "authentication",
                                    "permission", "not found. user", "user not found",
                                    "unresolved", "not resolvable", "map (disabled)",
                                    "no key", "missing key", "credential")):
        return ("credential",
                "key is missing, wrong or lacks access — check the 1Password field and `op read`")
    if status in (402,) or any(w in blob for w in
                               ("insufficient credit", "insufficient_quota", "more credits",
                                "payment required", "quota", "billing", "afford")):
        return ("quota",
                "the account cannot cover the request's MAXIMUM cost — providers price by "
                "max_tokens, not usage. Validation caps this (routing.yaml "
                "validation.max_tokens); an uncapped `--via hermes` leg cannot be capped")
    if status in (404,) or any(w in blob for w in
                               ("model not found", "unknown model", "does not exist",
                                "invalid model", "no such model")):
        return ("model-id",
                "the model id is stale — run `nicks-stack-provider-doctor --refresh-models`")
    if status == 0 or any(w in blob for w in
                          ("connection refused", "timed out", "timeout", "temporary failure",
                           "name resolution", "unreachable", "ssl", "certificate")):
        return ("network", "the endpoint was unreachable — check egress/DNS/proxy from this VM")
    if any(w in blob for w in ("unknown provider", "provider not", "no such provider",
                               "custom:", "not configured", "unsupported provider")):
        return ("hermes-config",
                "Hermes does not accept this provider — check config.yaml providers/plugins")
    if status in (429,):
        return ("provider-error", "rate limited — retry later")
    if status and status >= 500:
        return ("provider-error", "vendor-side error — retry later")
    return ("provider-error", "see the message above")


# --------------------------------------------------------------------------
# Providers / integrations / identity
# --------------------------------------------------------------------------
def providers_detect() -> dict:
    routing = load_yaml(ROUTING_FILE)
    cfg = load_yaml(CONFIG_FILE)
    plugins = (cfg.get("plugins") or {}).get("enabled") or []
    custom = set((cfg.get("providers") or {}).keys())
    out = {}
    for name, raw in (routing.get("providers") or {}).items():
        spec = raw or {}
        provider = spec.get("provider") or ""
        bare = provider.replace("custom:", "")
        wired = provider in plugins or f"{provider}-provider" in plugins or bare in custom
        entry = {
            "display": spec.get("display") or name.title(),
            "provider": provider,
            "enabled": bool(spec.get("enabled", True)),
            "wired": wired,
            "candidates": spec.get("models") or [],
            "key": spec.get("key_env"),
        }
        if name == "ollama":
            info = ollama_detect(spec)
            entry.update({
                "credential": {"key": None, "present": True, "source": "local (none required)"},
                "available": info["serving"] and bool(info["chat_models"]),
                "models_installed": info["models"],
                "detail": info["detail"],
            })
        else:
            cred = key_presence(spec.get("key_env") or "")
            entry.update({
                "credential": cred,
                "available": wired and cred["present"] and entry["enabled"],
                "detail": "" if cred["present"] else f"{cred['key']} {cred['source']}",
            })
        out[name] = entry
    return out


INTEGRATIONS = [
    # name,          kind,        probe
    ("Telegram",     "chat",      lambda: key_presence("TELEGRAM_BOT_TOKEN")["present"]),
    ("1Password",    "secrets",   lambda: bool(op_token()) and have("op")),
    ("Composio",     "mcp",       lambda: key_presence("COMPOSIO_API_KEY")["present"]),
    ("AgentMail",    "mcp",       lambda: key_presence("AGENTMAIL_API_KEY")["present"]),
    ("AgentPhone",   "mcp",       lambda: key_presence("AGENTPHONE_API_KEY")["present"]),
    ("Latitude",     "telemetry", lambda: key_presence("LATITUDE_API_KEY")["present"]),
    ("Orgo",         "mcp",       lambda: key_presence("ORGO_API_KEY")["present"]),
    ("Obsidian",     "notes",     lambda: VAULT_DIR.is_dir()),
    ("Claude Code",  "build-cli", lambda: have("claude")),
    ("Codex",        "build-cli", lambda: have("codex")),
]


def integrations_detect() -> dict:
    out = {}
    for name, kind, probe in INTEGRATIONS:
        try:
            present = bool(probe())
        except Exception:  # noqa: BLE001 - a probe must never break the report
            present = False
        out[name] = {"kind": kind, "configured": present}
    return out


def capabilities_detect() -> dict:
    """Declared capability routing vs what is actually on the machine.

    The declaration lives in platform.yaml (capabilities / forbidden_
    implementations). This checks the machine against it: a forbidden MCP
    server or plugin means a Hermes-native Google path has reappeared, which is
    the thing the decision forbids. Reads YAML only — nothing is started."""
    spec = load_yaml(PLATFORM_FILE)
    declared = spec.get("capabilities") or {}
    forbidden = spec.get("forbidden_implementations") or {}
    cfg = load_yaml(CONFIG_FILE)

    mcp = {n.lower() for n in (cfg.get("mcp_servers") or {})}
    plugins = {p.lower() for p in ((cfg.get("plugins") or {}).get("enabled") or [])}

    violations = []
    for name in forbidden.get("mcp_servers") or []:
        if name.lower() in mcp:
            violations.append(f"mcp_servers.{name} is configured — forbidden for this agent")
    for name in forbidden.get("plugins") or []:
        if name.lower() in plugins:
            violations.append(f"plugins.enabled contains '{name}' — forbidden for this agent")
    for name in forbidden.get("binaries") or []:
        if have(name):
            violations.append(f"binary '{name}' is on PATH — forbidden for this agent")

    # Composio is what every declared capability routes through, so its
    # absence makes the whole matrix unusable rather than merely degraded.
    composio_wired = "composio" in mcp
    composio_keyed = key_presence("COMPOSIO_API_KEY")["present"]

    return {
        "declared": declared,
        "composio_wired": composio_wired,
        "composio_credential": composio_keyed,
        "violations": violations,
        "compliant": not violations and composio_wired,
    }


def identity_detect() -> dict:
    spec = load_yaml(PLATFORM_FILE)
    identity = (spec.get("identity") or {})
    soul_present = SOUL_FILE.is_file() and SOUL_FILE.stat().st_size > 0
    return {
        "name": identity.get("name") or "unnamed",
        "role": identity.get("role") or "",
        "status": identity.get("status") or "unknown",
        "soul_file": str(SOUL_FILE),
        "soul_present": soul_present,
        "soul_bytes": SOUL_FILE.stat().st_size if soul_present else 0,
    }


def companies_detect() -> list[dict]:
    spec = load_yaml(PLATFORM_FILE)
    out = []
    for entry in (spec.get("companies") or []):
        item = dict(entry or {})
        item.setdefault("status", "declared")
        out.append(item)
    return out


def versions_detect() -> dict:
    spec = load_yaml(PLATFORM_FILE)
    versions = dict(spec.get("versions") or {})
    versions.setdefault("platform", spec.get("version") or "unknown")
    # Runtime versions of the things the platform depends on.
    for label, cmd in (("hermes", ["hermes", "--version"]),
                       ("ollama", ["ollama", "--version"]),
                       ("op", ["op", "--version"])):
        if have(cmd[0]):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                versions[label] = (proc.stdout or proc.stderr or "").strip().splitlines()[0][:60]
            except (OSError, subprocess.SubprocessError, IndexError):
                versions[label] = "unknown"
        else:
            versions[label] = "not installed"
    return versions


# Candidate checkout locations, in priority order. A deployment may live in
# any of these; nothing may assume one.  (v1.0.1 bug 2)
REPO_CANDIDATES = (
    "/opt/nicks-stack",
    "/root/nicks-stack",
    str(Path.home() / "nicks-stack"),
    "/home/user/nicks-stack",
)


def find_repo_root(start: Path | str | None = None) -> Path | None:
    """Locate the nicks-stack git checkout without assuming a path.

    Order: explicit argument -> $NICKS_STACK_REPO -> the path recorded in the
    manifest -> the known candidates -> walking up from this file. Returns the
    git top-level, or None when no checkout can be found.  (v1.0.1 bugs 1+2)
    """
    def toplevel(path: Path | str) -> Path | None:
        path = Path(path)
        if not path.is_dir() or not have("git"):
            return None
        try:
            proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        root = Path(proc.stdout.strip())
        return root if root.is_dir() else None

    tried: list[Path | str] = []
    if start:
        tried.append(start)
    env_repo = os.environ.get("NICKS_STACK_REPO", "").strip()
    if env_repo:
        tried.append(env_repo)
    # A previous deploy records where it ran from.
    try:
        stored = json.loads(MANIFEST_FILE.read_text()).get("repo_path")
        if stored:
            tried.append(stored)
    except (OSError, ValueError, AttributeError):
        pass
    tried.extend(REPO_CANDIDATES)
    # Finally: walk up from this file (works when running out of the checkout).
    tried.append(Path(__file__).resolve().parent)

    for candidate in tried:
        root = toplevel(candidate)
        if root and (root / "platform" / "bootstrap.sh").is_file():
            return root
    # Accept any git root as a last resort, even without the platform dir.
    for candidate in tried:
        root = toplevel(candidate)
        if root:
            return root
    return None


def git_commit(repo: Path | str | None = None) -> dict:
    """Branch/commit for the deployment checkout. Discovers the repo rather
    than assuming the caller's cwd is it.  (v1.0.1 bug 1)"""
    unknown = {"commit": "unknown", "short": "unknown", "branch": "unknown",
               "dirty": False, "repo_path": "", "detail": ""}
    if not have("git"):
        return {**unknown, "detail": "git not installed"}
    root = find_repo_root(repo)
    if root is None:
        return {**unknown, "detail": "no nicks-stack checkout found"}
    repo = root
    def run(args):
        try:
            proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, timeout=15, check=False)
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    return {
        "commit": run(["rev-parse", "HEAD"]) or "unknown",
        "short": run(["rev-parse", "--short", "HEAD"]) or "unknown",
        "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown",
        "dirty": bool(run(["status", "--porcelain"])),
        "repo_path": str(repo),
        "detail": "",
    }


def detect_all(repo: Path | str | None = None) -> dict:
    """The whole platform picture. Every tool reports from this one call."""
    spec = load_yaml(PLATFORM_FILE)
    return {
        "platform": {
            "name": spec.get("name") or "Taylor AI Platform",
            "version": spec.get("version") or "unknown",
            "hostname": os.uname().nodename,
            "os": _os_pretty(),
            "init_system": init_system(),
        },
        "versions": versions_detect(),
        "services": services_detect(),
        "providers": providers_detect(),
        "integrations": integrations_detect(),
        "identity": identity_detect(),
        "capabilities": capabilities_detect(),
        "companies": companies_detect(),
        "ollama": ollama_detect(),
        "git": git_commit(repo),
    }


def runtime_report() -> dict:
    """Gateway-vs-one-shot runtime parity, for shell callers (verify.sh).
    Key NAMES only — no value is read, resolved or emitted.  (v1.0.2)"""
    env = hermes_child_env()
    bridged = sorted(parse_env_file(HERMES_ENV))
    cfg = load_yaml(CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    validation = profile_for("validation")
    built = profile_ensure(validation) if validation else {"reason": "none declared"}
    isolation = profile_detect_layout(validation) if validation and built.get("path") else {}
    return {
        "hermes_on_path": have("hermes"),
        "root_env_present": ROOT_ENV.is_file(),
        "hermes_env_present": HERMES_ENV.is_file(),
        "op_env_present": OP_ENV.is_file(),
        "op_enabled": bool(op.get("enabled")),
        "op_references": len(op.get("env") or {}),
        "op_token_reachable": bool(env.get("OP_SERVICE_ACCOUNT_TOKEN")),
        "op_binary": have("op"),
        "bridged_keys": bridged,
        "bridged_key_count": len(bridged),
        "accept_hooks": True,
        "validation_profile": validation,
        "validation_profile_usable": bool(built.get("path")),
        "validation_profile_reason": built.get("reason", ""),
        "validation_profile_isolated": bool(isolation.get("isolated")),
        "validation_profile_layout": isolation.get("layout"),
        "validation_profile_isolation_detail": isolation.get("reason", ""),
        "findings": hermes_runtime_diagnose(),
    }


def _os_pretty() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


# --------------------------------------------------------------------------
# CLI — so shell callers use the same detection instead of re-implementing it
# --------------------------------------------------------------------------
def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__ or "lib.py detect|ollama|services|providers|versions [--json]")
        return 0
    what = args[0]
    table = {
        "detect": detect_all,
        "ollama": ollama_detect,
        "services": services_detect,
        "providers": providers_detect,
        "integrations": integrations_detect,
        "identity": identity_detect,
        "versions": versions_detect,
        "git": git_commit,
        "runtime": runtime_report,
        "profiles": profile_report,
        "capabilities": capabilities_detect,
    }
    if what not in table:
        print(f"unknown query '{what}' — one of: {', '.join(table)}", file=sys.stderr)
        return 2
    # ensure_ascii=False: shell callers print these strings verbatim, and
    # \uXXXX escapes would leak into verify.sh's output.
    print(json.dumps(table[what](), indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
