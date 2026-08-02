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


def key_presence(key_env: str) -> dict:
    """Presence only — never the value. This is what every report uses."""
    if not key_env:
        return {"key": key_env, "present": True, "source": "not required"}
    if os.environ.get(key_env, "").strip():
        return {"key": key_env, "present": True, "source": "environment"}
    if len(_env_file_value(key_env)) > 1:
        return {"key": key_env, "present": True, "source": ".env"}
    cfg = load_yaml(CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    if key_env in (op.get("env") or {}):
        if op.get("enabled"):
            return {"key": key_env, "present": True, "source": "1Password map"}
        return {"key": key_env, "present": False, "source": "1Password map (disabled)"}
    return {"key": key_env, "present": False, "source": "unresolved"}


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
def ollama_host(spec: dict | None = None) -> str:
    host = (os.environ.get("OLLAMA_HOST", "").strip()
            or _env_file_value("OLLAMA_HOST")
            or (spec or {}).get("host")
            or DEFAULT_OLLAMA_HOST)
    if not host.startswith("http"):
        host = f"http://{host}"
    return host.rstrip("/")


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
    ("Composio",     "mcp",       lambda: key_presence("COMPOSIO_CONSUMER_KEY")["present"]),
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


def git_commit(repo: Path | str | None = None) -> dict:
    repo = Path(repo or os.environ.get("NICKS_STACK_REPO", ".")).resolve()
    if not have("git"):
        return {"commit": "unknown", "branch": "unknown", "dirty": False}
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
        "companies": companies_detect(),
        "ollama": ollama_detect(),
        "git": git_commit(repo),
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
    }
    if what not in table:
        print(f"unknown query '{what}' — one of: {', '.join(table)}", file=sys.stderr)
        return 2
    print(json.dumps(table[what](), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
