#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — Composio Sessions bootstrap  (v1.1.0)
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


def _api_key() -> str:
    """COMPOSIO_API_KEY from the environment, ~/.hermes/.env, or 1Password."""
    value, _source = lib.resolve_key_value(KEY_ENV)
    return (value or "").strip()


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
    spec = _spec()
    user_id = spec.get("user_id") or "taylor"
    wanted = spec.get("toolkits") or {}

    key = _api_key()
    if not key:
        print(f"composio: {KEY_ENV} not resolvable — cannot create a session", file=sys.stderr)
        print("          add it to ~/.hermes/.env or the 1Password map, then re-run",
              file=sys.stderr)
        return 1

    Composio, err = _sdk()
    if Composio is None:
        print(f"composio: {err}", file=sys.stderr)
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
    if session is None:
        toolkits, unresolved = resolve_toolkits(client, wanted)
        if unresolved and args.strict:
            for u in unresolved:
                print(f"composio: unresolved toolkit for {u['capability']}: {u['error']}",
                      file=sys.stderr)
            return 1
        slugs = [t["slug"] for t in toolkits]
        try:
            session = client.sessions.create(user_id=user_id, toolkits=slugs or None,
                                             mcp=True)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the gateway
            print(f"composio: session create failed: {lib.scrub(str(exc))[:200]}",
                  file=sys.stderr)
            return 1
        mode = "created"
        _write_json(SESSION_FILE, {
            "session_id": session.session_id,
            "user_id": user_id,
            "toolkits": toolkits,
            "unresolved": unresolved,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    # 3. Render the runtime env. Only the URL is derived: the header value is
    #    COMPOSIO_API_KEY, which the gateway already has from ~/.hermes/.env.
    mcp = getattr(session, "mcp", None)
    url = getattr(mcp, "url", "") or ""
    headers = getattr(mcp, "headers", None) or {}
    if not url:
        print("composio: session has no MCP endpoint — was it created with mcp=True?",
              file=sys.stderr)
        return 1

    unexpected = [h for h in headers if h.lower() != "x-api-key"]
    if unexpected:
        # Fail loudly rather than render a config that silently drops auth.
        print(f"composio: session.mcp.headers carries unexpected header name(s): "
              f"{', '.join(sorted(unexpected))} — config.yaml only wires x-api-key. "
              f"Values are NOT shown.", file=sys.stderr)
        return 1

    COMPOSIO_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(COMPOSIO_DIR, 0o700)
    tmp = RUNTIME_ENV.with_suffix(".tmp")
    tmp.write_text(
        "# Generated by composio_session.py — DERIVED, do not edit, do not commit.\n"
        "# Sourced by hermes-gateway-run.sh. Regenerated on every gateway start.\n"
        f"COMPOSIO_MCP_URL={url}\n"
        f"COMPOSIO_MCP_TRANSPORT={getattr(getattr(mcp, 'type', None), 'value', 'http')}\n"
    )
    os.chmod(tmp, 0o600)
    tmp.replace(RUNTIME_ENV)

    # Presence only. Never the url, never the key.
    print(f"composio: session {mode} (id persisted), MCP endpoint obtained "
          f"[url {len(url)} chars, {len(headers)} header(s)] -> {RUNTIME_ENV}")
    return 0


# --------------------------------------------------------------------------
# status — diagnostics, presence only
# --------------------------------------------------------------------------
def status() -> dict:
    stored = read_session()
    key = _api_key()
    Composio, sdk_err = _sdk()
    runtime = lib.parse_env_file(RUNTIME_ENV) if RUNTIME_ENV.is_file() else {}
    url = runtime.get("COMPOSIO_MCP_URL", "")
    return {
        "api_key_available": bool(key),
        "sdk_importable": Composio is not None,
        "sdk_error": sdk_err,
        "session_id_persisted": bool(stored.get("session_id")),
        "session_file": str(SESSION_FILE),
        "user_id": stored.get("user_id", (_spec().get("user_id") or "taylor")),
        "toolkits_resolved": [t.get("slug") for t in (stored.get("toolkits") or [])],
        "toolkits_unresolved": [u.get("capability") for u in (stored.get("unresolved") or [])],
        "mcp_url_obtained": bool(url),
        "mcp_transport": runtime.get("COMPOSIO_MCP_TRANSPORT", ""),
        "runtime_env": str(RUNTIME_ENV),
        # Headers are constructed client-side by the SDK as {"x-api-key": key},
        # so "obtained" is equivalent to the key being available.
        "mcp_headers_obtained": bool(key and url),
        "created_at": stored.get("created_at", ""),
    }


def cmd_status(args) -> int:
    data = status()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0 if data["mcp_url_obtained"] else 1
    rows = [
        ("COMPOSIO_API_KEY available", data["api_key_available"]),
        ("composio SDK importable", data["sdk_importable"]),
        ("session id persisted", data["session_id_persisted"]),
        ("MCP url obtained", data["mcp_url_obtained"]),
        ("MCP headers obtained", data["mcp_headers_obtained"]),
    ]
    print("Composio Sessions\n")
    for label, ok in rows:
        print(f"  {'ok  ' if ok else 'MISS'}  {label}")
    print(f"\n  user_id            : {data['user_id']}")
    print(f"  toolkits resolved  : {', '.join(data['toolkits_resolved']) or 'none yet'}")
    if data["toolkits_unresolved"]:
        print(f"  UNRESOLVED         : {', '.join(data['toolkits_unresolved'])}")
    print(f"  transport          : {data['mcp_transport'] or 'unknown'}")
    if data["sdk_error"]:
        print(f"  sdk                : {data['sdk_error']}")
    print("\n  (values are never printed — presence only)")
    return 0 if data["mcp_url_obtained"] else 1


def cmd_resolve(args) -> int:
    key = _api_key()
    Composio, err = _sdk()
    if not key or Composio is None:
        print(f"composio: cannot resolve ({err or KEY_ENV + ' missing'})", file=sys.stderr)
        return 1
    client = Composio(api_key=key)
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
                   help="fail if any declared toolkit cannot be resolved")
    p = sub.add_parser("status", help="diagnostics (presence only)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("resolve", help="resolve capability -> toolkit slugs")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return {"init": cmd_init, "status": cmd_status, "resolve": cmd_resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
