#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — Composio Sessions bootstrap  (v1.1.7)
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
#   sessions.create(..., auth_configs={slug: "ac_..."})
#                                           -> t.Dict[str, str], "mapping of
#                                              toolkit slug to auth config ID"
#                                              (SDK docstring, tool_router.py:731)
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
#   composio_session.py deps            prove deps come from the venv, not
#                                      Ubuntu's dist-packages
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


def auth_config_map(spec: dict | None = None) -> dict:
    """platform.yaml composio.auth_configs, normalised to lowercase keys.

    Most Composio toolkits (gmail, googlecalendar, googledrive, notion) have a
    Composio-MANAGED OAuth app, so naming them in a session is enough — Composio
    auto-creates the auth config. A minority ship no managed app (googlecontacts
    is one) and the session create then returns:

        400 The following toolkits require auth configs but none exist and
            cannot be auto-created: googlecontacts

    This map is how the platform states, declaratively, what to do about that:

        auth_configs:
          googlecontacts: ""          -> DEFER: omit from the session
          googlecontacts: ac_XXXX     -> INCLUDE, passing that auth config id

    A key may be a toolkit slug or the capability name; both are accepted so the
    declaration reads naturally either way.
    """
    raw = (spec if spec is not None else _spec()).get("auth_configs")
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v or "").strip() for k, v in raw.items()}


def partition_toolkits(resolved: list, auth_map: dict) -> tuple[list, list, dict]:
    """Split resolved toolkits into (included, deferred, auth_configs).

    DEFERRED is a NARROWING that platform.yaml declared on purpose — not a
    resolution failure and not a silent drop. The strict-scoping rule is intact:
    a toolkit the platform asked for and could not resolve still fails the whole
    init; a toolkit the platform explicitly parked is reported as pending.
    """
    included, deferred, auth_configs = [], [], {}
    for entry in resolved:
        slug = entry.get("slug", "")
        cap = entry.get("capability", "")
        keys = [k for k in (slug.lower(), str(cap).lower()) if k in auth_map]
        if not keys:
            included.append(entry)                    # managed auth — nothing to do
            continue
        value = next((auth_map[k] for k in keys if auth_map[k]), "")
        if value:
            auth_configs[slug] = value
            included.append(dict(entry, auth_config="declared"))
        else:
            deferred.append(dict(entry, reason=(
                "no Composio-managed OAuth app and no auth config id declared in "
                "platform.yaml composio.auth_configs")))
    return included, deferred, auth_configs


def scope_key(spec: dict | None = None) -> str:
    """A fingerprint of the DECLARED scope: toolkits + auth-config decisions.

    Stored with the session so a later platform.yaml change — e.g. filling in
    composio.auth_configs.googlecontacts once the auth config exists — forces a
    fresh session instead of silently resuming the old, narrower one. It is a
    pure local comparison, so it costs the gateway nothing at start.
    """
    spec = spec if spec is not None else _spec()
    wanted = spec.get("toolkits") or {}
    return json.dumps({
        "toolkits": {str(k): str(v) for k, v in sorted(wanted.items())},
        "auth_configs": dict(sorted(auth_config_map(spec).items())),
    }, sort_keys=True)


def resolve_credential() -> dict:
    """Composio's view of the ONE canonical resolver.  (v1.1.2, unified v1.1.8)

    v1.1.8: this used to be a second, Composio-specific implementation of
    "resolve a secret". It is now a thin call into lib.resolve_runtime_secret,
    which is the single resolver the gateway, provider doctor, `jack secrets
    status` and verify.sh all use. Composio therefore cannot disagree with the
    rest of the platform about whether the key is available.

    Returns {available, source, error, value}. `value` is a secret: callers
    that report must drop it, and status() does."""
    res = lib.resolve_runtime_secret(KEY_ENV)
    return {"available": res["available"], "source": res["source"],
            "error": (f"{KEY_ENV} {res['error']}" if res["error"] else ""),
            "value": res["value"]}


def sdk_site_packages() -> list[str]:
    """The venv site-packages directories, in glob order."""
    out: list[str] = []
    for extra in (_spec().get("sdk_path"), "/opt/nicks-stack/composio-venv/lib"):
        if not extra:
            continue
        for cand in sorted(Path(extra).glob("python3*/site-packages")):
            if str(cand) not in out:
                out.append(str(cand))
    return out


def _sdk():
    """Import the SDK under the venv's dependency set. Returns (mod, err).

    v1.1.6: these paths are PREPENDED, not appended. Appending put them at the
    END of sys.path, behind Debian's /usr/lib/python3/dist-packages, so a
    system copy of a shared dependency won the import. Observed on the VM:

        cannot import name 'Sentinel' from 'typing_extensions'
        (/usr/lib/python3/dist-packages/typing_extensions.py)

    composio needs the newer typing_extensions that pip put in the venv; the
    Ubuntu one shadowed it. Inside a real venv, dist-packages is not on
    sys.path at all, so prepending is what reproduces the venv's resolution
    order — the dependency set bootstrap actually installed.

    Modules already imported from outside the venv cannot be re-resolved by a
    path change, so anything the venv supplies and that is already in
    sys.modules from a system location is dropped first. Nothing this script
    has imported by now (json, os, sys, pathlib, yaml via lib) is in that set,
    but a future import here would otherwise fail silently and confusingly.
    """
    paths = sdk_site_packages()
    for path in reversed(paths):           # reversed -> first glob ends up first
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)

    if paths:
        # Drop system-resolved copies of anything the venv provides, so the
        # prepended path is actually consulted.
        for name in list(sys.modules):
            mod = sys.modules.get(name)
            origin = getattr(mod, "__file__", "") or ""
            if origin and "dist-packages" in origin:
                base = name.split(".")[0]
                if any(Path(p, base).exists() or Path(p, base + ".py").exists()
                       for p in paths):
                    del sys.modules[name]

    try:
        from composio import Composio  # noqa: PLC0415
        return Composio, ""
    except ImportError as exc:
        return None, (f"composio SDK not importable ({exc}) — the venv is missing, "
                      f"was built by a different python3, or a system package is "
                      f"shadowing a dependency. Rebuild with: "
                      f"sudo rm -rf /opt/nicks-stack/composio-venv && "
                      f"sudo bash platform/bootstrap.sh")


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

    v1.1.2 put COMPOSIO_API_KEY in this file because config.yaml asks the
    gateway to send `x-api-key: ${COMPOSIO_API_KEY}` and the gateway expands
    that from ITS OWN environment, which had no such variable — a valid URL
    with an empty header and a silent 401.

    v1.1.8 keeps that guarantee but stops storing the key twice: it is a
    declared runtime secret, so secrets.env carries it and this file carries
    only the URL and transport. The guarantee is preserved by CHECKING, not by
    duplicating — activation refuses unless the key is genuinely present in the
    rendered runtime env the gateway sources, so the header can still never
    expand to nothing.

    Refuses to write anything at all without a usable key."""
    if not key:
        teardown("refusing to activate without a resolved COMPOSIO_API_KEY — "
                 "an empty x-api-key header would 401 silently")
        return False
    runtime_secrets = lib.runtime_secrets_path()
    if not (lib.parse_env_file(runtime_secrets).get(KEY_ENV) or "").strip():
        teardown(f"{KEY_ENV} resolves, but is not present in {runtime_secrets} — the "
                 f"gateway expands ${{{KEY_ENV}}} from its own environment and would "
                 f"send an EMPTY x-api-key header. Run: sudo nicks-stack-secrets render")
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
            "# COMPOSIO_API_KEY is NOT here — it is a declared runtime secret and\n"
            "# lives only in runtime/secrets.env (v1.1.8, no double storage).\n"
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
    auth_map = auth_config_map(spec)
    want_key = scope_key(spec)
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
    #    restart from minting a new session. A DECLARED-SCOPE CHANGE is the one
    #    thing that must not be resumed through: if platform.yaml now scopes a
    #    different toolkit set, or an auth config id has appeared for a toolkit
    #    that was previously deferred, the stored session no longer represents
    #    the declaration and a new one is minted.
    # A session with no recorded scope_key predates v1.1.7, so its scope cannot
    # be compared and is re-minted once, on the same reasoning.
    drift = bool(stored.get("session_id")) and stored.get("scope_key", "") != want_key
    if drift:
        print("composio: declared toolkit scope changed since the stored session "
              "was created — creating a new session", file=sys.stderr)
    if stored.get("session_id") and not args.new and not drift:
        try:
            session = client.sessions.use(stored["session_id"], mcp=True)
            mode = "resumed"
        except Exception as exc:  # noqa: BLE001 - fall through to create
            print(f"composio: could not resume stored session "
                  f"({lib.scrub(str(exc))[:100]}) — creating a new one", file=sys.stderr)

    # 2. Create only if there is nothing to resume.
    toolkits = stored.get("toolkits") or []
    deferred = stored.get("deferred") or []
    if session is None:
        resolved, unresolved = resolve_toolkits(client, wanted)

        # FAIL CLOSED on RESOLUTION. `toolkits=None` would create an UNSCOPED
        # session with every toolkit in the catalogue — a silent widening of
        # Jack's permissions. Never send it while toolkits are declared.
        if wanted and unresolved:
            names = ", ".join(u["capability"] for u in unresolved)
            teardown(f"toolkit resolution incomplete ({names}) — refusing to create an "
                     f"unscoped session")
            return 1
        if strict and unresolved:
            teardown("strict mode: some toolkits unresolved")
            return 1

        # DECLARED NARROWING. Toolkits with no Composio-managed OAuth app and no
        # auth config id are omitted rather than sent — sending one returns a
        # 400 that takes the whole session down with it, so a single unprovisioned
        # capability would block Gmail, Calendar, Drive and Notion too.
        toolkits, deferred, auth_configs = partition_toolkits(resolved, auth_map)
        slugs = [t["slug"] for t in toolkits]
        if wanted and not slugs:
            teardown("every declared toolkit is deferred for want of an auth config — "
                     "refusing to create an unscoped session")
            return 1
        if deferred:
            print("composio: deferring " + ", ".join(
                f"{d['capability']} ({d['slug']})" for d in deferred) +
                " — no auth config id in platform.yaml composio.auth_configs; "
                "the remaining toolkits are unaffected", file=sys.stderr)

        try:
            session = client.sessions.create(
                user_id=user_id, toolkits=slugs, mcp=True,
                **({"auth_configs": auth_configs} if auth_configs else {}))
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
        "deferred": deferred,
        "scope_key": want_key,
        "header_type": "x-api-key",
        "transport": transport,
    })

    print(f"composio: session {mode} (id persisted), MCP endpoint obtained "
          f"[url {len(url)} chars, {len(headers)} header(s)], "
          f"{len(toolkits)} toolkit(s) scoped"
          + (f", {len(deferred)} deferred (pending auth config)" if deferred else ""))
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
    spec = _spec()
    declared = spec.get("toolkits") or {}
    auth_map = auth_config_map(spec)
    resolved = [t.get("slug") for t in (stored.get("toolkits") or []) if t.get("slug")]
    resolved_caps = {t.get("capability") for t in (stored.get("toolkits") or [])}

    # Deferred: declared, but parked for want of an auth config id. Read from
    # the session when one exists; otherwise derive from platform.yaml so the
    # pending state is visible before any session has been created.
    if stored.get("deferred") is not None:
        deferred = [{"capability": d.get("capability", ""), "slug": d.get("slug", ""),
                     "reason": d.get("reason", "")} for d in (stored.get("deferred") or [])]
    else:
        pending = partition_toolkits(
            [{"capability": c, "slug": str(t)} for c, t in declared.items()], auth_map)[1]
        deferred = [{"capability": d["capability"], "slug": d["slug"],
                     "reason": "pending auth config"} for d in pending]
    deferred_caps = {d["capability"] for d in deferred}

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
    # A DEFERRED capability is not missing — the platform declared that it stays
    # out until an auth config exists, so it is reported, not failed.
    missing = sorted(set(declared) - resolved_caps - deferred_caps) if declared else []

    # Declared scope changed since the session was minted (e.g. an auth config
    # id was filled in). init() re-creates rather than resuming when this is set.
    drift = bool(stored.get("session_id")) and stored.get("scope_key", "") != scope_key(spec)

    # The gateway reads the key from the unified runtime secrets file (v1.1.8 —
    # it used to be duplicated into mcp.env), so "will the gateway have it?" is
    # a separate, checkable fact from "can we resolve it?".
    gateway_key_present = bool(
        (lib.parse_env_file(lib.runtime_secrets_path()).get(KEY_ENV) or "").strip())

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
        "toolkits_deferred": [d["capability"] for d in deferred],
        "deferred_detail": deferred,
        "scope_ok": not missing,
        "scope_drift": drift,
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
    for d in data["deferred_detail"]:
        print(f"  ○ {d['slug']}  ({d['capability']}: deferred, pending auth config)")
    if data["toolkits_deferred"]:
        print("  └ Composio has no managed OAuth app for the above. Create an auth")
        print("    config at platform.composio.dev with your own OAuth client, then put")
        print("    its id in platform.yaml composio.auth_configs and re-run init.")
    print()
    print("Last verified")
    print(_ts(data["last_verified"]))
    if data["stale_runtime"]:
        print("\nWARNING: stale runtime detected — run: sudo nicks-stack-composio-session init")
    if data["scope_drift"]:
        print("\nNOTE: platform.yaml declares a different toolkit scope than this session"
              "\n      carries — the next init will mint a new session automatically.")
    if data["sdk_error"]:
        print(f"\nSDK: {data['sdk_error']}")
    print("\n(no secret, URL or token value is ever printed)")
    return 0 if (data["mcp_url_present"] and data["scope_ok"]
                 and data["gateway_key_present"]) else 1


def cmd_deps(args) -> int:
    """Prove the SDK and its critical dependencies resolve from the VENV, not
    from Ubuntu's /usr/lib/python3/dist-packages.  (v1.1.6)

    This is the acceptance test for the import-order fix: typing_extensions in
    particular must come from the venv, because the system copy lacks the
    Sentinel symbol composio needs."""
    Composio, err = _sdk()               # applies the path ordering
    paths = sdk_site_packages()
    checks, ok_all = [], True

    for name in ("typing_extensions", "pydantic", "pydantic_core", "composio"):
        try:
            mod = __import__(name)
        except Exception as exc:  # noqa: BLE001
            checks.append({"module": name, "ok": False, "origin": "",
                           "detail": f"import failed: {lib.scrub(str(exc))[:100]}"})
            ok_all = False
            continue
        origin = getattr(mod, "__file__", "") or ""
        from_venv = any(origin.startswith(p) for p in paths)
        detail = ""
        if name == "typing_extensions":
            # The exact symbol whose absence broke the launcher.
            detail = f"Sentinel present: {hasattr(mod, 'Sentinel')}"
            if not hasattr(mod, "Sentinel"):
                from_venv = False
        checks.append({"module": name, "ok": from_venv, "origin": origin,
                       "detail": detail})
        ok_all = ok_all and from_venv

    if args.json:
        print(json.dumps({"site_packages": paths, "sdk_importable": Composio is not None,
                          "sdk_error": err, "checks": checks, "ok": ok_all}, indent=2))
        return 0 if ok_all and Composio is not None else 1

    print("Composio dependency resolution\n")
    print(f"  venv site-packages: {', '.join(paths) or 'none found'}\n")
    for c in checks:
        mark = "\u2713" if c["ok"] else "\u2717"
        where = "venv" if c["ok"] else "SYSTEM/dist-packages or missing"
        print(f"  {mark} {c['module']:<20} {where}")
        if c["origin"]:
            print(f"      {c['origin']}")
        if c["detail"]:
            print(f"      {c['detail']}")
    if err:
        print(f"\n  SDK: {err}")
    print(f"\n  verdict: {'all dependencies resolve from the venv' if ok_all else 'A DEPENDENCY IS BEING SHADOWED BY A SYSTEM PACKAGE'}")
    return 0 if ok_all and Composio is not None else 1


def cmd_resolve(args) -> int:
    cred = resolve_credential()
    Composio, err = _sdk()
    if not cred["available"] or Composio is None:
        print(f"composio: cannot resolve ({err or cred['error']})", file=sys.stderr)
        return 1
    spec = _spec()
    client = Composio(api_key=cred["value"])
    resolved, unresolved = resolve_toolkits(client, spec.get("toolkits") or {})
    included, deferred, auth_configs = partition_toolkits(resolved, auth_config_map(spec))
    if args.json:
        print(json.dumps({"resolved": resolved, "unresolved": unresolved,
                          "included": included, "deferred": deferred,
                          # ids, not secrets: an auth config id is a public
                          # handle, but only the names are printed here.
                          "auth_configs_declared": sorted(auth_configs)}, indent=2))
        return 0 if not unresolved else 1
    for r in included:
        extra = "  [auth config declared]" if r.get("auth_config") else ""
        print(f"  {r['capability']:<18} search={r['search']:<18} -> slug={r['slug']}{extra}")
    for d in deferred:
        print(f"  {d['capability']:<18} search={d['search']:<18} -> slug={d['slug']}  "
              f"DEFERRED ({d['reason']})")
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
    p = sub.add_parser("deps", help="prove dependencies resolve from the venv, not the system")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return {"init": cmd_init, "status": cmd_status, "resolve": cmd_resolve,
            "deps": cmd_deps}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
