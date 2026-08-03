#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — Composio Sessions bootstrap  (v1.1.2)
# ==========================================================================
# Replaces the legacy static Composio MCP wiring
#
#     url: https://connect.composio.dev/mcp
#     headers: {x-consumer-api-key: ${COMPOSIO_CONSUMER_KEY}}
#
# with the current Sessions API. Every method name below was taken from the
# INSTALLED SDK, not from documentation:
#
#   composio.sessions                       -> ToolRouter
#       (composio.tool_router is a deprecated alias since 0.17.0)
#   sessions.create(*, user_id, toolkits=[...], mcp=True)
#                                           -> ToolRouterSessionWithMcp
#   sessions.use(session_id, *, mcp=True)   -> ToolRouterSessionWithMcp
#   session.session_id                      -> the durable id we persist
#   session.mcp                             -> ToolRouterMCPServerConfig
#                                              (.type 'http'|'sse', .url, .headers)
#   client.toolkits.list(search=..., limit=...)
#                                           -> slug resolution, never invented
#
# ROTATION (SDK source, ToolRouter._create_mcp_server_config):
#   headers = {"x-api-key": self._client.api_key}
#   i.e. session.mcp.headers is built CLIENT-SIDE from COMPOSIO_API_KEY. It is
#   not a server-issued bearer token and does not rotate. Only session.mcp.url
#   comes from the server, so this script re-fetches it on every run and the
#   gateway wrapper runs this script at every start — expiry cannot strand us.
#
#   composio_session.py init            resume-or-create, render the runtime env
#   composio_session.py status [--json] diagnostics (presence only)
#   composio_session.py resolve         resolve capability -> toolkit slugs
#
# SECRET SAFETY: the API key, the MCP URL and every header VALUE are treated as
# secrets. This script never prints them, never logs them, and writes them only
# to a 0600 file. Diagnostics report presence, never content.
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

# Derived tree: regenerated, never user data. update.sh classifies it so.
COMPOSIO_DIR = Path(os.environ.get("NICKS_STACK_COMPOSIO", lib.HERMES_HOME / "composio"))
SESSION_FILE = COMPOSIO_DIR / "session.json"     # durable session id — survives updates
RUNTIME_ENV = COMPOSIO_DIR / "mcp.env"           # derived; sourced by gateway-run.sh
KEY_ENV = "COMPOSIO_API_KEY"


def _spec() -> dict:
    """The composio block from platform.yaml (user_id + capability map)."""
    spec = lib.load_yaml(lib.PLATFORM_FILE).get("composio")
    return spec if isinstance(spec, dict) else {}


def resolve_credential() -> dict:
    """THE canonical Composio credential resolution.  (v1.1.2)

    Session init, activation eligibility, `jack composio status` and verify.sh
    all go through this one function, so they can no longer disagree. It is a
    REAL resolution (env -> ~/.hermes/.env -> `op read`), never
    presence-by-declaration: a mapped op:// reference whose vault field does
    not exist reports unavailable, because that is the truth.

    Deliberately independent of `secrets.onepassword.enabled`: that flag
    governs whether HERMES resolves all 21 references at startup. Composio must
    not require it — the value is fetched here and handed to the gateway
    through the derived runtime env instead.

    Returns {available, source, error, value}. `value` is a secret: callers
    that report must drop it, and status() does.
    """
    value, source = lib.resolve_key_value(KEY_ENV)
    value = (value or "").strip()
    if value:
        return {"available": True, "source": source, "error": "", "value": value}

    # Not available — say precisely why, without printing anything sensitive.
    cfg = lib.load_yaml(lib.CONFIG_FILE)
    op = (cfg.get("secrets") or {}).get("onepassword") or {}
    ref = (op.get("env") or {}).get(KEY_ENV)
    if not ref:
        error = (f"{KEY_ENV} is neither in {lib.HERMES_ENV} nor mapped in "
                 f"config.yaml secrets.onepassword.env")
    elif not lib.op_token():
        error = (f"{KEY_ENV} is mapped to 1Password but no OP_SERVICE_ACCOUNT_TOKEN "
                 f"is reachable ({lib.OP_ENV}) — the vault cannot be read")
    elif not lib.have("op"):
        error = f"{KEY_ENV} is mapped to 1Password but the `op` CLI is not installed"
    else:
        field = ref.rsplit("/", 1)[-1]
        error = (f"`op read` returned nothing for the mapped reference — the field "
                 f"'{field}' is missing from the 1Password item (PROVISIONING)")
    return {"available": False, "source": "unresolved", "error": error, "value": ""}


def _sdk():
    """Import the SDK from wherever bootstrap installed it. Returns (mod, err)."""
    for extra in (_spec().get("sdk_path"), "/opt/nicks-stack/composio-venv/lib"):
        if not extra:
            continue
        for cand in Path(extra).glob("python3*/site-packages"):
            if str(cand) not in sys.path:
                sys.path.append(str(cand))
    try:
        from composio import Composio  # noqa: PLC0415
        return Composio, ""
    except ImportError as exc:
        return None, (f"composio SDK not importable ({exc}). Install it with: "
                      f"sudo bash platform/bootstrap.sh --with-composio")


# config.yaml carries these sentinels with NOTHING between them. The composio
# MCP entry is spliced in only when a session is established, and spliced back
# out on every failure — so Hermes is never handed an empty or stale url.
CONFIG_BEGIN = "  # >>> composio (runtime-managed) >>>"
CONFIG_END = "  # <<< composio <<<"
CONFIG_BLOCK = """  composio:
    url: ${COMPOSIO_MCP_URL}
    enabled: true
    headers:
      x-api-key: ${COMPOSIO_API_KEY}
    connect_timeout: 60.0"""


def _splice_config(block: str | None) -> str:
    """Put `block` between the sentinels in config.yaml, or remove it (None).

    A line-based splice, not a YAML round-trip: config.yaml is heavily
    commented and yaml.safe_dump would strip every comment in the file.
    Returns a short status string for the caller to report."""
    try:
        lines = lib.CONFIG_FILE.read_text().splitlines()
    except OSError as exc:
        return f"config.yaml unreadable ({exc})"
    try:
        i = next(n for n, ln in enumerate(lines) if ln.rstrip() == CONFIG_BEGIN)
        j = next(n for n, ln in enumerate(lines) if ln.rstrip() == CONFIG_END)
    except StopIteration:
        return "config.yaml has no composio sentinels — redeploy with bootstrap.sh"
    if j <= i:
        return "config.yaml composio sentinels are out of order"

    new_inner = block.splitlines() if block else []
    if lines[i + 1:j] == new_inner:
        return "unchanged"
    updated = lines[:i + 1] + new_inner + lines[j:]
    try:
        tmp = lib.CONFIG_FILE.with_suffix(".composio.tmp")
        tmp.write_text("\n".join(updated) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(lib.CONFIG_FILE)
    except OSError as exc:
        return f"could not write config.yaml ({exc})"
    return "injected" if block else "removed"


def teardown(reason: str) -> None:
    """Leave NO Composio runtime behind. Called on every failure path.

    Removes the derived env AND the config entry together, so the gateway can
    neither source a stale url nor start with an empty one. Jack then simply
    has no Composio tools, which is the honest state."""
    removed = []
    try:
        if RUNTIME_ENV.exists():
            RUNTIME_ENV.unlink()
            removed.append(str(RUNTIME_ENV))
    except OSError:
        pass
    status = _splice_config(None)
    if status == "removed":
        removed.append("config.yaml composio entry")
    print(f"composio: unavailable ({reason}) — removed {', '.join(removed) or 'nothing stale'}; "
          f"Hermes will start with no Composio capabilities", file=sys.stderr)


def activate(url: str, transport: str, key: str) -> bool:
    """Write the derived env and splice the config entry in. Both or neither.

    v1.1.2: the env now carries COMPOSIO_API_KEY as well. config.yaml asks the
    gateway to send `x-api-key: ${COMPOSIO_API_KEY}`, and the gateway expands
    that from ITS OWN environment — which previously had no such variable
    unless Hermes-wide 1Password resolution happened to be enabled. The result
    was a valid URL with an empty header and a silent 401. Putting the resolved
    value in this 0600 derived file, which gateway-run.sh sources before exec,
    is what makes the header resolvable without forcing all 21 op:// references
    to resolve at gateway startup.

    Refuses to write anything at all without a non-empty key, so an empty
    header can never be injected."""
    if not key:
        teardown("refusing to activate without a resolved COMPOSIO_API_KEY — "
                 "an empty x-api-key header would 401 silently")
        return False
    try:
        COMPOSIO_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(COMPOSIO_DIR, 0o700)
        tmp = RUNTIME_ENV.with_suffix(".tmp")
        # 0600 before any secret is written into it.
        tmp.touch(mode=0o600, exist_ok=True)
        os.chmod(tmp, 0o600)
        tmp.write_text(
            "# Generated by composio_session.py — DERIVED and SECRET-BEARING.\n"
            "# Do not edit, do not commit, do not print. Mode 0600.\n"
            "# Sourced by hermes-gateway-run.sh. Regenerated on every gateway start.\n"
            f"COMPOSIO_API_KEY={key}\n"
            f"COMPOSIO_MCP_URL={url}\n"
            f"COMPOSIO_MCP_TRANSPORT={transport}\n"
        )
        os.chmod(tmp, 0o600)
        tmp.replace(RUNTIME_ENV)
    except OSError as exc:
        teardown(f"could not write {RUNTIME_ENV}: {exc}")
        return False
    status = _splice_config(CONFIG_BLOCK)
    if status not in ("injected", "unchanged"):
        teardown(f"config.yaml splice failed: {status}")
        return False
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# --------------------------------------------------------------------------
# Toolkit slugs — resolved live, never invented
# --------------------------------------------------------------------------
def resolve_toolkits(client, wanted: dict) -> tuple[list, list]:
    """Map each declared capability to a real toolkit slug via
    client.toolkits.list(search=...). Returns (resolved, unresolved).

    `wanted` is {capability: search-term} from platform.yaml. Nothing here
    hardcodes a slug: if Composio renames a toolkit, this resolves the new
    name and the failure mode is a loud 'unresolved', not a silent wrong slug.
    """
    resolved, unresolved = [], []
    for capability, term in wanted.items():
        try:
            page = client.client.toolkits.list(search=str(term), limit=10)
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not abort the rest
            unresolved.append({"capability": capability, "search": term,
                               "error": lib.scrub(str(exc))[:120]})
            continue
        items = getattr(page, "items", None) or []
        slug = ""
        for item in items:
            candidate = (getattr(item, "slug", "") or "").strip()
            if candidate:
                # Exact slug match on the search term wins; otherwise first hit.
                if candidate.lower() == str(term).lower():
                    slug = candidate
                    break
                slug = slug or candidate
        if slug:
            resolved.append({"capability": capability, "search": term, "slug": slug})
        else:
            unresolved.append({"capability": capability, "search": term,
                               "error": "no toolkit matched"})
    return resolved, unresolved


# --------------------------------------------------------------------------
# init — resume or create, then render the runtime env
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    """Resume or create the session. FAILS CLOSED: every exit path that is not
    a fully-validated session tears the runtime down first, so a stale url, an
    empty url and an unscoped session are all unreachable states."""
    spec = _spec()
    user_id = spec.get("user_id") or "taylor"
    wanted = spec.get("toolkits") or {}
    strict = not args.allow_partial          # strict is the DEFAULT now

    cred = resolve_credential()
    if not cred["available"]:
        teardown(cred["error"])
        return 1
    key = cred["value"]

    Composio, err = _sdk()
    if Composio is None:
        teardown(err)
        return 1

    client = Composio(api_key=key)
    stored = read_session()
    session, mode = None, ""

    # 1. Resume the persisted session first — this is what stops every gateway
    #    restart from minting a new session.
    if stored.get("session_id") and not args.new:
        try:
            session = client.sessions.use(stored["session_id"], mcp=True)
            mode = "resumed"
        except Exception as exc:  # noqa: BLE001 - fall through to create
            print(f"composio: could not resume stored session "
                  f"({lib.scrub(str(exc))[:100]}) — creating a new one", file=sys.stderr)

    # 2. Create only if there is nothing to resume.
    toolkits = stored.get("toolkits") or []
    if session is None:
        toolkits, unresolved = resolve_toolkits(client, wanted)
        slugs = [t["slug"] for t in toolkits]

        # FAIL CLOSED on scope. `toolkits=None` would create an UNSCOPED
        # session with every toolkit in the catalogue — a silent widening of
        # Jack's permissions. Never send it while toolkits are declared.
        if wanted and (unresolved or not slugs):
            names = ", ".join(u["capability"] for u in unresolved) or "all declared toolkits"
            teardown(f"toolkit resolution incomplete ({names}) — refusing to create an "
                     f"unscoped session")
            return 1
        if strict and unresolved:
            teardown("strict mode: some toolkits unresolved")
            return 1

        try:
            session = client.sessions.create(user_id=user_id, toolkits=slugs, mcp=True)
        except Exception as exc:  # noqa: BLE001
            teardown(f"session create failed: {lib.scrub(str(exc))[:160]}")
            return 1
        mode = "created"

    # 3. Validate the endpoint BEFORE anything is written.
    mcp = getattr(session, "mcp", None)
    url = (getattr(mcp, "url", "") or "").strip()
    headers = getattr(mcp, "headers", None) or {}
    if not url:
        teardown("session has no MCP endpoint (created without mcp=True?)")
        return 1
    unexpected = [h for h in headers if h.lower() != "x-api-key"]
    if unexpected:
        teardown(f"session.mcp.headers carries unexpected header name(s): "
                 f"{', '.join(sorted(unexpected))} — values NOT shown")
        return 1

    transport = getattr(getattr(mcp, "type", None), "value", "http")
    # Proves the gateway will have a usable key: activate() refuses without one.
    if not activate(url, transport, key):
        return 1

    _write_json(SESSION_FILE, {
        "session_id": session.session_id,
        "user_id": user_id,
        "created_at": stored.get("created_at") if mode == "resumed"
        else _now(),
        "last_verified": _now(),
        "toolkits": toolkits,
        "header_type": "x-api-key",
        "transport": transport,
    })

    print(f"composio: session {mode} (id persisted), MCP endpoint obtained "
          f"[url {len(url)} chars, {len(headers)} header(s)], "
          f"{len(toolkits)} toolkit(s) scoped")
    return 0


# --------------------------------------------------------------------------
# status — live diagnostics, presence only
# --------------------------------------------------------------------------
def status(live: bool = True) -> dict:
    stored = read_session()
    cred = resolve_credential()          # the SAME resolution init uses
    key = cred["value"]
    Composio, sdk_err = _sdk()
    runtime = lib.parse_env_file(RUNTIME_ENV) if RUNTIME_ENV.is_file() else {}
    url = (runtime.get("COMPOSIO_MCP_URL") or "").strip()
    declared = _spec().get("toolkits") or {}
    resolved = [t.get("slug") for t in (stored.get("toolkits") or []) if t.get("slug")]
    resolved_caps = {t.get("capability") for t in (stored.get("toolkits") or [])}

    # Is the composio entry actually present in Hermes' config right now?
    try:
        cfg_text = lib.CONFIG_FILE.read_text()
    except OSError:
        cfg_text = ""
    inner = ""
    if CONFIG_BEGIN in cfg_text and CONFIG_END in cfg_text:
        inner = cfg_text.split(CONFIG_BEGIN, 1)[1].split(CONFIG_END, 1)[0]
    config_active = "composio:" in inner

    # Live validity: ask Composio whether the stored session still resolves.
    validity, validity_detail = "unknown", "not checked"
    if live and key and Composio is not None and stored.get("session_id"):
        try:
            probe = Composio(api_key=key).sessions.use(stored["session_id"], mcp=True)
            validity = "valid" if getattr(getattr(probe, "mcp", None), "url", "") else "invalid"
            validity_detail = ("session resolves and exposes an MCP endpoint"
                               if validity == "valid" else "session resolves but has no endpoint")
        except Exception as exc:  # noqa: BLE001
            validity, validity_detail = "invalid", lib.scrub(str(exc))[:120]
    elif not stored.get("session_id"):
        validity, validity_detail = "absent", "no session has been created yet"

    # Scope mismatch: what the session holds vs what platform.yaml declares.
    missing = sorted(set(declared) - resolved_caps) if declared else []

    # The gateway reads the key from the derived env, so "will the gateway
    # have it?" is a separate, checkable fact from "can we resolve it?".
    gateway_key_present = bool((runtime.get(KEY_ENV) or "").strip())

    return {
        "api_key_available": cred["available"],
        "api_key_source": cred["source"],
        "api_key_error": cred["error"],
        "gateway_key_present": gateway_key_present,
        "sdk_importable": Composio is not None,
        "sdk_error": sdk_err,
        "session_id": stored.get("session_id", ""),
        "session_id_persisted": bool(stored.get("session_id")),
        "session_validity": validity,
        "session_validity_detail": validity_detail,
        "user_id": stored.get("user_id") or (_spec().get("user_id") or "taylor"),
        "mcp_url_present": bool(url),
        "mcp_headers_obtained": bool(key and url),
        "header_type": stored.get("header_type", "x-api-key"),
        "transport": runtime.get("COMPOSIO_MCP_TRANSPORT", stored.get("transport", "")),
        "config_entry_active": config_active,
        "toolkits_declared": sorted(declared),
        "toolkits_resolved": resolved,
        "toolkits_missing": missing,
        "scope_ok": not missing,
        # Stale = a runtime endpoint exists that the live check says is dead,
        # or an env/config pair that disagree.
        "stale_runtime": bool((url or config_active) and validity == "invalid")
        or bool(url) != config_active
        or (config_active and not gateway_key_present),
        "created_at": stored.get("created_at", ""),
        "last_verified": stored.get("last_verified", ""),
        "session_file": str(SESSION_FILE),
        "runtime_env": str(RUNTIME_ENV),
    }


def _ts(iso: str) -> str:
    if not iso:
        return "never"
    return iso.replace("T", " ").replace("+00:00", " UTC")


def cmd_status(args) -> int:
    data = status(live=not args.offline)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0 if (data["mcp_url_present"] and data["scope_ok"]
                     and data["gateway_key_present"]) else 1

    tick = lambda ok: "\u2713" if ok else "\u2717"  # noqa: E731
    print("Composio")
    print("\u2500" * 20)
    print(f"Project API Key      {tick(data['api_key_available'])} "
          f"{data['api_key_source'] if data['api_key_available'] else ''}")
    if not data["api_key_available"]:
        print(f"  \u2514 {data['api_key_error']}")
    print(f"Gateway key          {tick(data['gateway_key_present'])} "
          f"{'in ' + data['runtime_env'] if data['gateway_key_present'] else 'NOT in the runtime env'}")
    print(f"Session              {tick(data['session_validity'] == 'valid')} "
          f"{data['session_validity']}")
    if data["session_id"]:
        print(f"Session ID           {data['session_id']}")
    print(f"User                 {data['user_id']}")
    print(f"MCP URL              {tick(data['mcp_url_present'])}")
    print(f"Header               {data['header_type']}")
    print(f"Runtime config       {tick(data['config_entry_active'])} "
          f"{'active in config.yaml' if data['config_entry_active'] else 'absent (no Composio tools)'}")
    print("Toolkits")
    if data["toolkits_resolved"]:
        for slug in data["toolkits_resolved"]:
            print(f"  {tick(True)} {slug}")
    else:
        print("  (none scoped)")
    for cap in data["toolkits_missing"]:
        print(f"  {tick(False)} {cap}  (declared but NOT in the session)")
    print()
    print("Last verified")
    print(_ts(data["last_verified"]))
    if data["stale_runtime"]:
        print("\nWARNING: stale runtime detected — run: sudo nicks-stack-composio-session init")
    if data["sdk_error"]:
        print(f"\nSDK: {data['sdk_error']}")
    print("\n(no secret, URL or token value is ever printed)")
    return 0 if (data["mcp_url_present"] and data["scope_ok"]
                 and data["gateway_key_present"]) else 1


def cmd_resolve(args) -> int:
    cred = resolve_credential()
    Composio, err = _sdk()
    if not cred["available"] or Composio is None:
        print(f"composio: cannot resolve ({err or cred['error']})", file=sys.stderr)
        return 1
    client = Composio(api_key=cred["value"])
    resolved, unresolved = resolve_toolkits(client, _spec().get("toolkits") or {})
    if args.json:
        print(json.dumps({"resolved": resolved, "unresolved": unresolved}, indent=2))
        return 0 if not unresolved else 1
    for r in resolved:
        print(f"  {r['capability']:<18} search={r['search']:<18} -> slug={r['slug']}")
    for u in unresolved:
        print(f"  {u['capability']:<18} search={u['search']:<18} -> UNRESOLVED ({u['error']})")
    return 0 if not unresolved else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="composio_session.py",
                                 description="Composio Sessions bootstrap for Hermes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="resume or create the session, render runtime env")
    p.add_argument("--new", action="store_true", help="force a new session")
    p.add_argument("--strict", action="store_true",
                   help="accepted for compatibility; strict is now the default")
    p.add_argument("--allow-partial", action="store_true",
                   help="DANGEROUS: proceed when some declared toolkits are unresolved")
    p = sub.add_parser("status", help="live diagnostics (presence only)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--offline", action="store_true",
                   help="skip the live session-validity call")
    p = sub.add_parser("resolve", help="resolve capability -> toolkit slugs")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return {"init": cmd_init, "status": cmd_status, "resolve": cmd_resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
