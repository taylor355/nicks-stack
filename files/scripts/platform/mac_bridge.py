#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — Mac mini bridge  (v1.1.34)
# ==========================================================================
# Taylor has an always-on Mac mini. Three things can only live there, and all
# three are things this VM cannot do:
#
#   1. OLLAMA that actually answers. The `local` route has been dead since
#      v1.1.11 — not misconfigured, starved: 92% CPU steal on this VM, a 1.7B
#      model never finished a warmup in 20 minutes. Apple Silicon fixes that by
#      having real cores. Free inference, nothing leaves his house.
#
#   2. CLAUDE CODE on his own subscription. A consumer subscription is a
#      browser login on a machine he owns; it is NOT an API key and must never
#      be driven as one (routing.yaml `build`, coding-agent-routing skill).
#      So build work runs where the login lives, and the flat-rate plan absorbs
#      it instead of the metered API. Codex is the same shape and is the
#      backup lane when Claude limits are tapped.
#
#   3. IMESSAGE. iMessage is macOS-only, full stop. BlueBubbles is a macOS
#      server that exposes the Messages app over HTTP; there is no cloud
#      substitute and no way to do this from Linux.
#
# The VM reaches the Mac over TAILSCALE — a private mesh between two machines
# Taylor owns. Nothing here is published to the internet, no port is forwarded
# on his home router, and the Mac needs no static IP.
#
# WHAT THIS SCRIPT IS NOT: it is not a policy engine. WHEN to hand off a build
# and WHAT is allowed to become a text message live in the mac-bridge skill,
# where Jack reads them. This file enforces only the mechanical guards — is the
# Mac up, is the specialist really authenticated, has the daily text cap been
# hit — and it fails closed and loudly on every one of them.
#
# PROOF, NOT STATUS. Taylor's standing rule is that a status command is not
# proof. So `status` does not ask Claude Code whether it is logged in; it sends
# a real one-turn prompt and requires real text back. Same for Ollama (a real
# /api/tags) and BlueBubbles (a real /server/info). A green line here means
# something answered, not that a config file looked right.
#
# SECRET SAFETY: BLUEBUBBLES_PASSWORD is resolved through the runtime secrets
# plane and travels in a query string (BlueBubbles' own API shape) over the
# Tailscale link only. It is never logged, never echoed, never included in an
# error message — every outbound URL passes through lib.scrub() before it can
# reach stdout.
#
# USAGE
#   python3 mac_bridge.py join             # one time, joins the tailnet
#   python3 mac_bridge.py status [--json]
#   python3 mac_bridge.py text "<message>" [--to <handle>] [--force]
#   python3 mac_bridge.py build "<task>" --confirm [--specialist claude-code|codex]
#   python3 mac_bridge.py fetch <remote-path> [--dest <dir>]
#   python3 mac_bridge.py models            # what Ollama on the Mac can run
# ==========================================================================
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

STATE = lib.HERMES_HOME.joinpath("runtime", "mac_bridge.json")

BASE_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "LogLevel=ERROR",
]

# Minted by platform/bootstrap.sh, no passphrase, because this has to work
# unattended from a cron wake. Its PUBLIC half is what Taylor pastes into the
# Mac's authorized_keys; the private half never leaves this VM.
DEFAULT_IDENTITY = "/root/.ssh/jack_mac_ed25519"


# USERSPACE NETWORKING, NOT A CHOICE (v1.1.34).
# This VM's container has no /dev/net/tun and cannot modprobe it, so tailscaled
# runs with --tun=userspace-networking. That mode has no tailscale0 interface
# and no kernel route: the VM cannot simply "connect to 100.86.x.x". Everything
# outbound goes through the two local listeners tailscaled provides instead —
# a SOCKS5 proxy for ssh (reached with `tailscale nc`, which needs no extra
# tools) and an HTTP proxy for Ollama and BlueBubbles.
#
# This is a supported Tailscale mode, not a workaround, and it is exactly what
# Tailscale documents for containers. What it costs: the VM is reachable-FROM
# nothing (it can dial the Mac, the Mac cannot dial it), which is the direction
# this bridge needs anyway.
DEFAULT_PROXY_COMMAND = "tailscale nc %h %p"
DEFAULT_HTTP_PROXY = "http://127.0.0.1:1056"


def ssh_opts(cfg: dict) -> list[str]:
    identity = cfg.get("identity_file") or DEFAULT_IDENTITY
    opts = list(BASE_SSH_OPTS)
    if Path(identity).is_file():
        opts += ["-i", identity, "-o", "IdentitiesOnly=yes"]
    proxy = cfg.get("ssh_proxy_command")
    if proxy:
        opts += ["-o", f"ProxyCommand={proxy}"]
    return opts


def proxied_json(cfg: dict, url: str, payload: dict | None = None,
                 timeout: int = 15) -> tuple[int, dict | None, str]:
    """lib.http_json with the tailnet HTTP proxy applied to THIS call only.

    Deliberately not done with HTTP_PROXY environment variables: this VM
    already has an outbound agent proxy configured that way, and hijacking it
    globally would break every other network call the platform makes."""
    proxy = cfg.get("http_proxy") or ""
    if not proxy:
        return lib.http_json(url, payload=payload, timeout=timeout)

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
            try:
                return resp.status, json.loads(body), ""
            except ValueError:
                return resp.status, None, body[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode(errors="replace")[:400]
    except Exception as exc:  # noqa: BLE001 - every transport failure is "down"
        return 0, None, str(exc)


DEFAULTS = {
    "host": None,
    "ssh_user": None,
    "identity_file": DEFAULT_IDENTITY,
    "ssh_proxy_command": DEFAULT_PROXY_COMMAND,
    "http_proxy": DEFAULT_HTTP_PROXY,
    "workspace": "~/JackBuilds",
    "ollama_port": 11434,
    "ollama_url": "http://127.0.0.1:11435",
    "build_mode": "queue",
    "queue_dir": "$HOME/JackBuilds/queue",
    "runs_dir": "$HOME/JackBuilds",
    "claude_binary": "claude",
    "codex_binary": "codex",
    "max_turns": 40,
    "build_timeout": 1800,
    "bluebubbles_port": 1234,
    "bluebubbles_scheme": "http",
    "recipient": None,
    "daily_text_cap": 6,
    "max_text_chars": 1200,
}


# --------------------------------------------------------------------------
# Configuration — platform.yaml is the only source, nothing is inferred
# --------------------------------------------------------------------------
def shell_path(path: str) -> str:
    """`~/JackBuilds` is useless inside the double quotes the remote script
    uses — bash expands a tilde only when it is unquoted, so mkdir would
    cheerfully create a directory literally named '~'. Hand bash something it
    will expand where it actually sits."""
    text = str(path or "").strip()
    if text == "~":
        return "$HOME"
    if text.startswith("~/"):
        return "$HOME/" + text[2:]
    return text


def config() -> dict:
    raw = lib.load_yaml(lib.PLATFORM_FILE).get("mac_bridge") or {}
    bb = raw.get("bluebubbles") or {}
    ol = raw.get("ollama") or {}
    cc = raw.get("claude_code") or {}
    return {
        "enabled": bool(raw.get("enabled")),
        "host": raw.get("host") or DEFAULTS["host"],
        "ssh_user": raw.get("ssh_user") or DEFAULTS["ssh_user"],
        "identity_file": raw.get("identity_file") or DEFAULTS["identity_file"],
        "ssh_proxy_command": (DEFAULTS["ssh_proxy_command"]
                              if raw.get("ssh_proxy_command") is None
                              else raw.get("ssh_proxy_command")),
        "http_proxy": (DEFAULTS["http_proxy"] if raw.get("http_proxy") is None
                       else raw.get("http_proxy")),
        "workspace": shell_path(raw.get("workspace") or DEFAULTS["workspace"]),
        "ollama_port": int(ol.get("port") or DEFAULTS["ollama_port"]),
        "ollama_url": ol.get("url") or DEFAULTS["ollama_url"],
        "build_mode": raw.get("build_mode") or DEFAULTS["build_mode"],
        "queue_dir": shell_path(raw.get("queue_dir") or DEFAULTS["queue_dir"]),
        "runs_dir": shell_path(raw.get("runs_dir") or DEFAULTS["runs_dir"]),
        "claude_binary": cc.get("binary") or DEFAULTS["claude_binary"],
        "codex_binary": (raw.get("codex") or {}).get("binary") or DEFAULTS["codex_binary"],
        "max_turns": int(cc.get("max_turns") or DEFAULTS["max_turns"]),
        "build_timeout": int(raw.get("build_timeout") or DEFAULTS["build_timeout"]),
        "bluebubbles_port": int(bb.get("port") or DEFAULTS["bluebubbles_port"]),
        "bluebubbles_scheme": bb.get("scheme") or DEFAULTS["bluebubbles_scheme"],
        "recipient": bb.get("recipient") or DEFAULTS["recipient"],
        "daily_text_cap": int(bb.get("daily_cap") or DEFAULTS["daily_text_cap"]),
        "max_text_chars": int(bb.get("max_chars") or DEFAULTS["max_text_chars"]),
    }


def not_configured(cfg: dict) -> str | None:
    """One sentence Jack can say verbatim, or None when the bridge is usable."""
    if not cfg["enabled"]:
        return ("The Mac mini bridge is switched off (platform.yaml -> "
                "mac_bridge.enabled). Nothing on the Mac has been set up yet.")
    if not cfg["host"]:
        return "The Mac mini bridge has no host set (platform.yaml -> mac_bridge.host)."
    return None


# --------------------------------------------------------------------------
# State — the daily text cap, and nothing else
# --------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with STATE.open() as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    tmp.replace(STATE)


def today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


# --------------------------------------------------------------------------
# Transport — SSH over Tailscale
# --------------------------------------------------------------------------
def ssh_target(cfg: dict) -> str:
    return f"{cfg['ssh_user']}@{cfg['host']}" if cfg["ssh_user"] else cfg["host"]


def ssh(cfg: dict, remote_cmd: str, timeout: int = 45) -> tuple[int, str, str]:
    """Run a command on the Mac. Returns (rc, stdout, stderr), both scrubbed."""
    cmd = ["ssh", *ssh_opts(cfg), ssh_target(cfg), remote_cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "ssh is not installed on this VM"
    return proc.returncode, lib.scrub(proc.stdout), lib.scrub(proc.stderr)


def remote_script(body: str) -> str:
    """Ship a shell body base64-encoded so quoting can never bite.

    The script is written to a temp file and run with stdin CLOSED, rather
    than piped into `bash -s`. That is not fussiness: with `| bash -s`, the
    shell's stdin IS the pipe carrying the rest of the script, so the first
    child process that reads stdin swallows it. Codex does exactly that — its
    first live run printed "Reading additional input from stdin..." and ate
    the artifact-listing half of its own wrapper, which then came back as
    shell fragments in the artifact list. `claude -p` reads stdin too. Closing
    it is the fix for both."""
    blob = base64.b64encode(body.encode()).decode()
    return (f'f=$(mktemp); echo {blob} | base64 -d > "$f"; '
            f'bash "$f" < /dev/null; rc=$?; rm -f "$f"; exit $rc')



# --------------------------------------------------------------------------
# The queue — how work reaches the GUI login session
# --------------------------------------------------------------------------
# ssh CANNOT run Claude Code. Not "is awkward at": macOS refuses Keychain
# access to an ssh session outright —
#
#   security: SecKeychainCopySettings login.keychain-db:
#             User interaction is not allowed.
#
# so `claude -p` over ssh cannot read its own subscription token and reports
# "Not logged in" while the user is, in fact, logged in. That is macOS policy.
# No ssh flag, no PATH fix and no amount of retrying changes it.
#
# A LaunchAgent does change it. jack-build-runner.sh is bootstrapped into the
# Aqua (GUI) domain, where the Keychain is unlocked and every tool behaves as
# it does in Terminal. So work is ENQUEUED as a file over ssh and executed by
# that agent. The queue is a directory: nothing listens on a port, and the
# only way to enqueue is to already hold ssh access to the account.
#
# Codex does not need this — it keeps its auth in a plain file and runs fine
# over ssh — but it goes through the same path anyway, because one code path
# that always works beats two that work under different conditions.
def enqueue(cfg: dict, specialist: str, task: str, run_id: str) -> tuple[bool, str]:
    """Drop a task file into the Mac's queue. First line is the specialist,
    everything after is the brief verbatim — deliberately not JSON, so a brief
    full of quotes and newlines cannot be mis-parsed on the way in."""
    payload = base64.b64encode(f"{specialist}\n{task}".encode()).decode()
    body = (f'mkdir -p "{cfg["queue_dir"]}"\n'
            f'echo {payload} | base64 -d > "{cfg["queue_dir"]}/{run_id}.task.tmp"\n'
            f'mv "{cfg["queue_dir"]}/{run_id}.task.tmp" '
            f'"{cfg["queue_dir"]}/{run_id}.task"\n'
            f'echo queued\n')
    rc, out, err = ssh(cfg, remote_script(body), timeout=45)
    if rc != 0 or "queued" not in out:
        return False, (err or out or f"ssh exited {rc}").strip()[:200]
    return True, ""


def await_run(cfg: dict, run_id: str, timeout: int) -> dict:
    """Poll for the runner's DONE marker. The runner writes it atomically and
    LAST, so its presence means the artifacts it lists are complete on disk."""
    run_dir = f'{cfg["runs_dir"]}/{run_id}'
    deadline = time.time() + timeout
    poll = (f'if [ -f "{run_dir}/DONE" ]; then cat "{run_dir}/DONE"; '
            f'echo "---LOG---"; tail -40 "{run_dir}/RESULT.log" 2>/dev/null; '
            f'else echo PENDING; fi')
    while time.time() < deadline:
        rc, out, _ = ssh(cfg, remote_script(poll), timeout=45)
        if rc == 0 and "PENDING" not in out:
            done, _, log = out.partition("---LOG---")
            exit_code, artifacts = 1, []
            head, _, art_block = done.partition("---ARTIFACTS---")
            for line in head.splitlines():
                if line.strip().startswith("exit="):
                    exit_code = int(line.strip().split("=", 1)[1] or 1)
            artifacts = [a.strip() for a in art_block.splitlines() if a.strip()]
            return {"pending": False, "exit_code": exit_code,
                    "artifacts": artifacts, "log": log.strip(), "run_dir": run_dir}
        time.sleep(5)
    return {"pending": True, "exit_code": 124, "artifacts": [],
            "log": f"no result after {timeout}s", "run_dir": run_dir}


# --------------------------------------------------------------------------
# Health checks — each one is a real call, never a status readout
# --------------------------------------------------------------------------
def check_reachable(cfg: dict) -> dict:
    rc, out, err = ssh(cfg, "echo mac-ok && sw_vers -productVersion 2>/dev/null", timeout=20)
    if rc == 0 and "mac-ok" in out:
        version = ""
        for line in out.splitlines():
            line = line.strip()
            if line and line != "mac-ok":
                version = line
                break
        return {"ok": True, "detail": f"macOS {version}" if version else "reachable"}
    hint = "run `tailscale status` on both machines" if rc in (255, 127, 124) else ""
    return {"ok": False, "detail": (err or out or f"ssh exited {rc}").splitlines()[0][:160],
            "hint": hint}


def check_ollama(cfg: dict) -> dict:
    # Through the SUPERVISED SSH TUNNEL, not the tailnet address. Ollama on the
    # Mac listens on 127.0.0.1 only, and that was left alone on purpose: a
    # forwarded port is strictly more private than binding the daemon to the
    # network, and it needs nothing clicked on the Mac. supervisor keeps the
    # tunnel up; if it drops, this check fails honestly instead of hanging.
    url = cfg["ollama_url"].rstrip("/") + "/api/tags"
    status, body, err = lib.http_json(url, timeout=10)
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "detail": lib.scrub(err or f"HTTP {status}")[:160],
                "hint": "check the tunnel: supervisorctl status mac-ollama-tunnel"}
    models = [m.get("name", "") for m in body.get("models") or []]
    chat = [m for m in models if lib.is_chat_model(m)]
    if not chat:
        return {"ok": False, "detail": "daemon answers but no chat model is pulled",
                "hint": "on the Mac: ollama pull qwen3:8b"}
    return {"ok": True, "detail": f"{len(chat)} chat model(s): " + ", ".join(sorted(chat)[:4]),
            "models": sorted(chat)}


def check_specialist(cfg: dict, which: str) -> dict:
    """A real one-turn prompt. A version string proves installation, not login."""
    if cfg.get("build_mode") == "queue":
        return check_specialist_queued(cfg, which)
    binary = cfg["claude_binary"] if which == "claude-code" else cfg["codex_binary"]
    if which == "claude-code":
        probe = (f'{binary} -p "Reply with exactly: BRIDGE-OK" '
                 f'--max-turns 1 --output-format text 2>&1 | tail -5')
    else:
        # --skip-git-repo-check: the build workspace is a plain directory, not
        # a repo, and Codex refuses to run outside a "trusted directory"
        # without this. Found by the first live probe, which failed on the
        # trust check before it ever reached the auth question.
        probe = (f'{binary} exec --skip-git-repo-check "Reply with exactly: BRIDGE-OK" '
                 f'2>&1 | tail -5')
    rc, out, err = ssh(cfg, remote_script(
        f'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
        f'command -v {binary} >/dev/null 2>&1 || {{ echo "NOT-INSTALLED"; exit 0; }}\n'
        f'{probe}\n'), timeout=120)
    blob = f"{out}\n{err}"
    if "NOT-INSTALLED" in blob:
        return {"ok": False, "detail": f"{binary} is not installed on the Mac"}
    if "BRIDGE-OK" in blob:
        return {"ok": True, "detail": "authenticated (answered a live prompt)"}
    lowered = blob.lower()
    if "login" in lowered or "auth" in lowered or "unauthor" in lowered:
        return {"ok": False, "detail": "installed but not logged in",
                "hint": f"on the Mac: run `{binary}` and sign in"}
    if "limit" in lowered or "quota" in lowered or "rate" in lowered:
        return {"ok": False, "detail": "installed and logged in, but the plan limit is hit",
                "hint": "use the backup specialist until it resets"}
    return {"ok": False, "detail": (blob.strip().splitlines() or ["no response"])[-1][:160]}


def check_specialist_queued(cfg: dict, which: str) -> dict:
    """Same live prompt, routed through the GUI-session runner — the only place
    Claude Code can read its Keychain credential."""
    run_id = f"probe-{which}-{int(time.time())}"
    ok, err = enqueue(cfg, which, "Reply with exactly: BRIDGE-OK", run_id)
    if not ok:
        return {"ok": False, "detail": f"could not reach the queue: {err}",
                "hint": "is the Mac awake and is Remote Login still on?"}
    res = await_run(cfg, run_id, timeout=150)
    blob = res["log"]
    if res["pending"]:
        return {"ok": False, "detail": "the build runner did not pick the task up",
                "hint": "on the Mac: launchctl print gui/$(id -u)/com.jack.buildrunner"}
    if "BRIDGE-OK" in blob:
        return {"ok": True, "detail": "authenticated (answered a live prompt)"}
    lowered = blob.lower()
    if "expired" in lowered:
        return {"ok": False, "detail": "signed in, but the session expired",
                "hint": "on the Mac: run `claude`, then /login"}
    if "not logged in" in lowered or "/login" in lowered:
        return {"ok": False, "detail": "installed but not signed in",
                "hint": "on the Mac: run `claude`, then /login"}
    if "limit" in lowered or "quota" in lowered:
        return {"ok": False, "detail": "signed in, but the plan limit is hit",
                "hint": "use the backup specialist until it resets"}
    return {"ok": False, "detail": (blob.splitlines() or ["no response"])[-1][:160]}


def bb_base(cfg: dict) -> str:
    return f"{cfg['bluebubbles_scheme']}://{cfg['host']}:{cfg['bluebubbles_port']}"


def bb_url(cfg: dict, path: str, password: str, extra: dict | None = None) -> str:
    params = {"password": password}
    params.update(extra or {})
    return f"{bb_base(cfg)}{path}?{urllib.parse.urlencode(params)}"


def check_bluebubbles(cfg: dict) -> dict:
    resolved = lib.resolve_runtime_secret("BLUEBUBBLES_PASSWORD")
    password = (resolved or {}).get("value") or ""
    if not password:
        return {"ok": False, "detail": "BLUEBUBBLES_PASSWORD is not set",
                "hint": "add it to the Hermes vault item, then restart the gateway"}
    if not cfg["recipient"]:
        return {"ok": False, "detail": "no recipient configured",
                "hint": "set platform.yaml -> mac_bridge.bluebubbles.recipient"}
    status, body, err = proxied_json(cfg, bb_url(cfg, "/api/v1/server/info", password),
                                     timeout=10)
    if status != 200:
        return {"ok": False, "detail": lib.scrub(err or f"HTTP {status}")[:160],
                "hint": "is the BlueBubbles server running on the Mac?"}
    data = (body or {}).get("data") or {}
    ver = data.get("os_version") or data.get("server_version") or "running"
    return {"ok": True, "detail": f"BlueBubbles {ver}, sending to {cfg['recipient']}"}


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        report = {"configured": False, "reason": blocked}
        print(json.dumps(report, indent=2) if args.json else blocked)
        return 1

    checks = {"reachable": check_reachable(cfg)}
    if checks["reachable"]["ok"]:
        checks["ollama"] = check_ollama(cfg)
        checks["claude-code"] = check_specialist(cfg, "claude-code")
        checks["codex"] = check_specialist(cfg, "codex")
        checks["imessage"] = check_bluebubbles(cfg)

    if args.json:
        print(json.dumps({"configured": True, "host": cfg["host"], "checks": checks}, indent=2))
    else:
        print(f"Mac mini bridge  ({cfg['host']})")
        for name, res in checks.items():
            mark = "ok  " if res["ok"] else "FAIL"
            print(f"  [{mark}] {name:<12} {res['detail']}")
            if not res["ok"] and res.get("hint"):
                print(f"          -> {res['hint']}")
    # Reachability is the only hard failure; a missing backup lane is not.
    return 0 if checks["reachable"]["ok"] else 2


def cmd_models(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        print(blocked)
        return 1
    res = check_ollama(cfg)
    print(json.dumps(res, indent=2) if args.json else res["detail"])
    return 0 if res["ok"] else 2


# --------------------------------------------------------------------------
# text — iMessage, delivery only, hard-capped
# --------------------------------------------------------------------------
def cmd_text(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        print(blocked, file=sys.stderr)
        return 1

    message = (args.message or "").strip()
    if not message:
        print("nothing to send", file=sys.stderr)
        return 1
    if len(message) > cfg["max_text_chars"]:
        print(f"message is {len(message)} chars, cap is {cfg['max_text_chars']}. "
              "Send the short version by text and the full version on Telegram.",
              file=sys.stderr)
        return 1

    to = args.to or cfg["recipient"]
    if not to:
        print("no recipient configured (platform.yaml -> mac_bridge.bluebubbles.recipient)",
              file=sys.stderr)
        return 1

    state = load_state()
    counts = state.get("text_counts") or {}
    day = today_key()
    sent_today = int(counts.get(day) or 0)
    if sent_today >= cfg["daily_text_cap"] and not args.force:
        print(f"daily text cap reached ({sent_today}/{cfg['daily_text_cap']}). "
              "Send this on Telegram instead.", file=sys.stderr)
        return 3

    resolved = lib.resolve_runtime_secret("BLUEBUBBLES_PASSWORD")
    password = (resolved or {}).get("value") or ""
    if not password:
        print("BLUEBUBBLES_PASSWORD is not set", file=sys.stderr)
        return 1

    guid = to if ";-;" in to else f"iMessage;-;{to}"
    payload = {
        "chatGuid": guid,
        "message": message,
        "method": "apple-script",
        "tempGuid": f"jack-{int(time.time() * 1000)}",
    }
    status, body, err = proxied_json(
        cfg, bb_url(cfg, "/api/v1/message/text", password), payload=payload, timeout=45)
    if status not in (200, 201):
        print(f"iMessage send failed: {lib.scrub(err or f'HTTP {status}')[:200]}", file=sys.stderr)
        return 2

    counts[day] = sent_today + 1
    for stale in [k for k in counts if k < day]:
        counts.pop(stale, None)
    state["text_counts"] = counts
    state["last_text_at"] = int(time.time())
    save_state(state)
    print(f"sent by iMessage to {to} ({counts[day]}/{cfg['daily_text_cap']} today)")
    return 0


# --------------------------------------------------------------------------
# build — hand the work to the specialist that owns the subscription
# --------------------------------------------------------------------------
BUILD_WRAPPER = """\
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
RUN_DIR="{workspace}/{run_id}"
mkdir -p "$RUN_DIR"
cd "$RUN_DIR"
echo {task_b64} | base64 -d > TASK.md
{invoke}
echo "---ARTIFACTS---"
find . -type f ! -name 'TASK.md' ! -name 'RESULT.log' -newer TASK.md -print 2>/dev/null | sed 's|^\\./||'
echo "---RUNDIR---"
echo "$RUN_DIR"
"""

INVOKE = {
    "claude-code": ('{binary} -p "$(cat TASK.md)" --max-turns {max_turns} '
                    '--permission-mode acceptEdits --output-format text '
                    '> RESULT.log 2>&1 || true; tail -40 RESULT.log'),
    # --sandbox workspace-write: Codex defaults to a READ-ONLY sandbox, so its
    # first live build politely reported that it could not create the file it
    # had been asked for and exited 0. workspace-write confines it to the run
    # directory, which is the whole point of giving each build its own.
    "codex": ('{binary} exec --skip-git-repo-check --sandbox workspace-write '
              '"$(cat TASK.md)" > RESULT.log 2>&1 || true; tail -40 RESULT.log'),
}


def cmd_build(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        print(blocked, file=sys.stderr)
        return 1

    task = (args.task or "").strip()
    if not task:
        print("no task given", file=sys.stderr)
        return 1

    specialist = args.specialist
    if specialist not in INVOKE:
        print(f"unknown specialist {specialist}", file=sys.stderr)
        return 1

    run_id = f"run-{time.strftime('%Y%m%d-%H%M%S', time.localtime())}"
    binary = cfg["claude_binary"] if specialist == "claude-code" else cfg["codex_binary"]
    body = BUILD_WRAPPER.format(
        workspace=cfg["workspace"],
        run_id=run_id,
        task_b64=base64.b64encode(task.encode()).decode(),
        invoke=INVOKE[specialist].format(binary=binary, max_turns=cfg["max_turns"]),
    )

    if args.dry_run:
        print(body)
        return 0
    if not args.confirm:
        print("build never runs unapproved. Show Taylor the task, get a yes, "
              "then re-run with --confirm.", file=sys.stderr)
        return 3

    # Never claim a specialist ran without proving it can.
    health = check_specialist(cfg, specialist)
    if not health["ok"]:
        print(f"{specialist} is not usable: {health['detail']}", file=sys.stderr)
        if health.get("hint"):
            print(f"-> {health['hint']}", file=sys.stderr)
        return 2

    if cfg.get("build_mode") == "queue":
        ok, err = enqueue(cfg, specialist, task, run_id)
        if not ok:
            print(f"could not enqueue the build: {err}", file=sys.stderr)
            return 2
        res = await_run(cfg, run_id, timeout=cfg["build_timeout"])
        rc, out, err = res["exit_code"], "", ""
        artifacts, run_dir, log = res["artifacts"], res["run_dir"], res["log"]
        result = {
            "specialist": specialist, "run_id": run_id, "run_dir": run_dir,
            "artifacts": artifacts, "exit_code": rc,
            "log_tail": log[-2000:], "stderr": "",
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"{specialist} finished in {run_dir} (exit {rc})")
            if artifacts:
                print("Artifacts:")
                for a in artifacts:
                    print(f"  {a}")
            else:
                print("No files were produced.")
            if log:
                print("\n--- last output ---")
                print(log[-1500:])
        return 0 if rc == 0 else 2

    rc, out, err = ssh(cfg, remote_script(body), timeout=cfg["build_timeout"])
    artifacts, run_dir, log = [], "", out
    if "---ARTIFACTS---" in out:
        log, _, rest = out.partition("---ARTIFACTS---")
        art_block, _, dir_block = rest.partition("---RUNDIR---")
        artifacts = [a.strip() for a in art_block.splitlines() if a.strip()]
        run_dir = dir_block.strip()

    result = {
        "specialist": specialist,
        "run_id": run_id,
        "run_dir": run_dir,
        "artifacts": artifacts,
        "exit_code": rc,
        "log_tail": log.strip()[-2000:],
        "stderr": err.strip()[-500:],
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{specialist} finished in {run_dir or run_id} (exit {rc})")
        if artifacts:
            print("Artifacts:")
            for a in artifacts:
                print(f"  {a}")
        else:
            print("No files were produced.")
        if log.strip():
            print("\n--- last output ---")
            print(log.strip()[-1500:])
    return 0 if rc == 0 else 2


# --------------------------------------------------------------------------
# fetch — pull an artifact back so Jack can attach it on Telegram
# --------------------------------------------------------------------------
def cmd_fetch(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        print(blocked, file=sys.stderr)
        return 1

    dest = Path(args.dest) if args.dest else lib.HERMES_HOME.joinpath("runtime", "mac_artifacts")
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["scp", *ssh_opts(cfg), "-p", f"{ssh_target(cfg)}:{args.remote_path}", str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("scp timed out", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print(lib.scrub(proc.stderr).strip()[:300] or "scp failed", file=sys.stderr)
        return 2
    landed = dest.joinpath(Path(args.remote_path).name)
    size = landed.stat().st_size if landed.exists() else 0
    print(f"{landed} ({size} bytes)")
    return 0


# --------------------------------------------------------------------------
# watch — the difference between "works today" and "keeps working"
# --------------------------------------------------------------------------
# Taylor's ask was that iMessage be bulletproof, because he intends to redirect
# real notifications onto it. The failure that matters is not a loud one. It is
# the Mac going to sleep, BlueBubbles quitting after an update, Messages
# signing itself out, or the Tailscale key expiring — after which Jack keeps
# "sending" texts that never arrive, and Taylor finds out by missing something.
#
# So this runs on a schedule and compares each lane against the last run.
# It speaks ONLY on a state change:
#
#   working -> broken   say so, once, with the specific reason
#   broken  -> working  say so, once, so silence is never ambiguous
#   unchanged           say nothing at all
#
# The last line of stdout is the Hermes cron wake gate. {"wakeAgent": false}
# skips the LLM run entirely, so a healthy day costs nothing: no tokens, no
# Telegram message, no noise. That is what makes a frequent check affordable.
#
# LIFECYCLE-GUARD NOTE (same trap as tkfamily_scan.py): the cron lifecycle
# guard tokenises this job's command and fails closed on any path-like token
# that is not a regular file. Keep bare "/" and directory literals out of the
# scheduled command string.
def cmd_watch(args) -> int:
    cfg = config()
    blocked = not_configured(cfg)
    if blocked:
        print(json.dumps({"wakeAgent": False}))
        return 0

    checks = {"reachable": check_reachable(cfg)}
    if checks["reachable"]["ok"]:
        checks["imessage"] = check_bluebubbles(cfg)
        if not args.imessage_only:
            checks["ollama"] = check_ollama(cfg)
            checks["claude-code"] = check_specialist(cfg, "claude-code")

    state = load_state()
    previous = state.get("watch") or {}
    changes = []
    now = {}
    for name, res in checks.items():
        now[name] = bool(res["ok"])
        was = previous.get(name)
        if was is None:
            continue                      # first sighting is not a change
        if was and not res["ok"]:
            changes.append(f"{name} stopped working: {res['detail']}")
        elif not was and res["ok"]:
            changes.append(f"{name} is working again")
    state["watch"] = now
    state["watch_checked_at"] = int(time.time())
    save_state(state)

    if not changes:
        # Nothing to say. The wake gate keeps this free.
        print(json.dumps({"wakeAgent": False}))
        return 0

    print("MAC BRIDGE STATE CHANGE")
    for line in changes:
        print(f"- {line}")
    print(json.dumps({
        "wakeAgent": True,
        "reason": "mac-bridge lane changed state",
        "changes": changes,
    }))
    return 0


# --------------------------------------------------------------------------
# join — bring this VM onto Taylor's tailnet without anyone handling the key
# --------------------------------------------------------------------------
def cmd_join(args) -> int:
    """The auth key lives in the Hermes vault and is never printed, never
    logged, and never passed on a visible command line — it goes to tailscale
    through the environment. Idempotent: an already-joined VM says so and
    exits 0 rather than re-authenticating."""
    if not lib.have("tailscale"):
        print("tailscale is not installed on this VM", file=sys.stderr)
        return 1

    probe = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=20)
    if probe.returncode == 0:
        try:
            state = json.loads(probe.stdout)
        except ValueError:
            state = {}
        if state.get("BackendState") == "Running":
            name = (state.get("Self") or {}).get("DNSName", "").rstrip(".")
            print(f"already on the tailnet as {name or 'this machine'}")
            return 0

    resolved = lib.resolve_runtime_secret("TAILSCALE_AUTHKEY")
    key = (resolved or {}).get("value") or ""
    if not key:
        print("TAILSCALE_AUTHKEY is not set. Generate one at "
              "login.tailscale.com/admin/settings/keys and put it in the Hermes "
              "vault item as TAILSCALE_AUTHKEY.", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["TS_AUTHKEY"] = key
    proc = subprocess.run(
        ["tailscale", "up", "--auth-key=env:TS_AUTHKEY",
         f"--hostname={args.hostname}", "--accept-routes=false", "--ssh=false"],
        capture_output=True, text=True, timeout=120, env=env)
    if proc.returncode != 0:
        print(lib.scrub(proc.stderr or proc.stdout).strip()[:300] or "tailscale up failed",
              file=sys.stderr)
        return 2
    print(f"joined the tailnet as {args.hostname}")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Bridge from this VM to Taylor's Mac mini")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("watch", help="scheduled health check; speaks only on a state change")
    p.add_argument("--imessage-only", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("join", help="bring this VM onto Taylor's tailnet (one time)")
    p.add_argument("--hostname", default="jack-55-vm")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("status", help="prove every lane end to end")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", help="chat models Ollama on the Mac can run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("text", help="send one iMessage (delivery only, capped)")
    p.add_argument("message")
    p.add_argument("--to")
    p.add_argument("--force", action="store_true", help="override the daily cap")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("build", help="hand a build task to Claude Code or Codex")
    p.add_argument("task")
    p.add_argument("--specialist", default="claude-code", choices=sorted(INVOKE))
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("fetch", help="copy an artifact back to this VM")
    p.add_argument("remote_path")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
