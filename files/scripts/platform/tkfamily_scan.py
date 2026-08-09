#!/usr/bin/env python3
"""TK Family document scan — the cheap half of the every-minute loop.

Taylor's spec says to check for new documents every 60 seconds. Doing that with
a model in the loop would mean 1,440 LLM turns a day, nearly all of them to
discover that nothing happened. So the minute-by-minute work is done here, in
plain HTTP with no model at all, and the agent is woken only when there is
something for it to do.

The mechanism is Hermes' cron wake gate: the scheduler parses the LAST non-empty
stdout line of a job's pre-run script as JSON, and `{"wakeAgent": false}` skips
the LLM run and the delivery entirely. So an idle minute costs three HTTP calls.
A minute with work prints a manifest of exactly what turned up, which the
scheduler injects into the agent's prompt as context.

What it watches:

  1. TK Family `list_pending_documents`  — captured in the app, not yet read
  2. `0 Inbox/Scanner Pro - Auto Upload Here`   } the three Drive drop zones,
  3. `0 Inbox/Receipts to File`                 } listed through Composio with
  4. `0 Inbox/Everything Else`                  } the OWNER account

Two things stop this from becoming a wake-up storm.

  * Re-wake backoff. A document Jack failed to clear stays in the drop zone, and
    without backoff it would wake him again sixty seconds later, forever. An
    item is re-raised after RETRY_MINUTES, and after MAX_WAKES it is marked
    STUCK and re-raised only every STUCK_MINUTES.
  * Failure backoff. If TK Family or Composio is down, the failure is counted,
    not announced. The agent is woken at the thresholds in FAIL_WAKE_AT so a
    real outage is reported once, not sixty times an hour.

Usage:
    tkfamily_scan.py            scan, update state, emit the wake gate (cron)
    tkfamily_scan.py --status   human-readable, never touches state or the gate
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

STATE = Path(os.environ.get("NICKS_STACK_HERMES_HOME", "/root/.hermes")) / "runtime" / "tkfamily_scan.json"

RETRY_MINUTES = 30      # re-raise an item still sitting there after this long
MAX_WAKES = 5           # after this many wakes for one item, call it stuck
STUCK_MINUTES = 360     # and re-raise it only every six hours
FAIL_WAKE_AT = (15, 60, 240)   # consecutive-failure counts that earn a wake
HTTP_TIMEOUT = 45


# ---------------------------------------------------------------------------
# config + secrets
# ---------------------------------------------------------------------------
def spec() -> dict:
    cfg = lib.load_yaml(lib.PLATFORM_FILE).get("tk_family")
    return cfg if isinstance(cfg, dict) else {}


def token() -> str:
    """TK_FAMILY_TOKEN from the runtime env, the rendered secrets file, or
    ~/.hermes/.env. Never resolved through `op` here: this runs every minute and
    must not shell out to 1Password sixty times an hour."""
    key = spec().get("token_env") or "TK_FAMILY_TOKEN"
    val = os.environ.get(key, "").strip()
    if val:
        return val
    for path in (lib.runtime_secrets_path(), lib.HERMES_ENV):
        try:
            val = lib.parse_env_file(path).get(key, "").strip()
        except Exception:
            val = ""
        if val:
            return val
    return ""


def composio_creds() -> tuple[str, str]:
    env = {}
    for path in (lib.HERMES_HOME / "composio" / "mcp.env", lib.runtime_secrets_path(),
                 lib.HERMES_ENV):
        try:
            env.update({k: v for k, v in lib.parse_env_file(path).items() if v})
        except Exception:
            pass
    url = os.environ.get("COMPOSIO_MCP_URL") or env.get("COMPOSIO_MCP_URL", "")
    key = os.environ.get("COMPOSIO_API_KEY") or env.get("COMPOSIO_API_KEY", "")
    return url.strip(), key.strip()


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
def _rpc(url: str, headers: dict, method: str, params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode("utf-8")
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    # Streamable-HTTP transports answer with SSE framing; take the data line.
    if raw.lstrip().startswith("event:") or "\ndata: " in raw:
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break
    return json.loads(raw)


def _tool_text(payload: dict) -> str:
    parts = (payload.get("result") or {}).get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
    if text:
        return text
    if payload.get("error"):
        raise RuntimeError("rpc error: %s" % json.dumps(payload["error"])[:300])
    raise RuntimeError("no text content in response")


def tk_call(name: str, args: dict) -> dict:
    cfg = spec()
    tok = token()
    if not tok:
        raise RuntimeError("TK_FAMILY_TOKEN is not resolvable (env, %s, %s)"
                           % (lib.runtime_secrets_path(), lib.HERMES_ENV))
    payload = _rpc(cfg["mcp_url"], {"x-mcp-token": tok}, "tools/call",
                   {"name": name, "arguments": args})
    return json.loads(_tool_text(payload))


def drive_list(folder_ids: dict) -> dict:
    """Map folder path -> list of files, in ONE Composio round trip."""
    url, key = composio_creds()
    if not url or not key:
        raise RuntimeError("no live Composio session (COMPOSIO_MCP_URL / "
                           "COMPOSIO_API_KEY unresolved) — cannot read the "
                           "Drive drop zones")
    account = spec().get("drive_account") or None
    paths = list(folder_ids)
    tools = []
    for path in paths:
        item = {"tool_slug": "GOOGLEDRIVE_FIND_FILE",
                "arguments": {"folder_id": folder_ids[path], "trashed": False,
                              "pageSize": 50,
                              "fields": "files(id,name,mimeType,createdTime)"}}
        if account:
            # `account` belongs on the ITEM, not at the top level. Put it at the
            # top level and Composio silently uses the default account, which is
            # NOT the owner of Covey Files.
            item["account"] = account
        tools.append(item)

    payload = _rpc(url, {"x-api-key": key}, "tools/call",
                   {"name": "COMPOSIO_MULTI_EXECUTE_TOOL",
                    "arguments": {"tools": tools,
                                  "sync_response_to_workbench": False}})
    body = json.loads(_tool_text(payload))
    if not body.get("successful", True):
        raise RuntimeError("composio multi-execute failed: %s"
                           % json.dumps(body.get("error"))[:300])

    out = {path: [] for path in paths}
    for result in (body.get("data") or {}).get("results") or []:
        idx = result.get("index")
        if not isinstance(idx, int) or idx >= len(paths):
            continue
        response = result.get("response") or {}
        if not response.get("successful"):
            raise RuntimeError("listing %s failed: %s"
                               % (paths[idx], json.dumps(response)[:200]))
        for f in (response.get("data") or {}).get("files") or []:
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                continue      # a drop zone holds documents, not subfolders
            out[paths[idx]].append(f)
    return out


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(STATE)
    except OSError as exc:
        print("state write failed: %s" % exc, file=sys.stderr)


def gate(wake: bool) -> None:
    print(json.dumps({"wakeAgent": bool(wake)}))


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def collect() -> tuple[list, dict]:
    """(pending app documents, {folder path: [drive files]}). Raises on outage."""
    cfg = spec()
    docs = tk_call("list_pending_documents", {"limit": 25}).get("documents") or []
    drops = drive_list(cfg.get("inboxes") or {})
    return docs, drops


def item_keys(docs: list, drops: dict) -> dict:
    """Stable key -> one-line label, for every unit of work found."""
    items = {}
    for d in docs:
        did = d.get("id") or d.get("document_id") or ""
        if did:
            items["doc:%s" % did] = d
    for path, files in drops.items():
        for f in files:
            items["file:%s" % f.get("id")] = dict(f, _path=path)
    return items


def main() -> int:
    status_only = "--status" in sys.argv[1:]
    cfg = spec()
    if not cfg or not cfg.get("enabled", True):
        if status_only:
            print("tk_family is not enabled in platform.yaml")
            return 0
        gate(False)
        return 0

    state = load_state()
    seen = state.get("items") or {}
    now = int(time.time())

    try:
        docs, drops = collect()
    except Exception as exc:                       # network, auth, schema drift
        streak = int(state.get("fail_streak") or 0) + 1
        state["fail_streak"] = streak
        state["last_error"] = str(exc)[:400]
        if status_only:
            print("scan FAILED (%d consecutive): %s" % (streak, exc))
            return 1
        save_state(state)
        wake = streak in FAIL_WAKE_AT or (streak > FAIL_WAKE_AT[-1]
                                          and streak % FAIL_WAKE_AT[-1] == 0)
        if wake:
            print("TK FAMILY SCAN IS FAILING")
            print("")
            print("The every-minute document scan has failed %d times in a row."
                  % streak)
            print("Last error: %s" % str(exc)[:400])
            print("")
            print("Documents captured in the app are NOT being read while this "
                  "is broken. Tell Taylor plainly what is down and what you "
                  "need. Do not attempt to process documents this turn.")
        gate(wake)
        return 0

    state["fail_streak"] = 0
    state.pop("last_error", None)

    items = item_keys(docs, drops)

    # Anything that has left the queue or the drop zone is finished work.
    seen = {k: v for k, v in seen.items() if k in items}

    fresh, retry, stuck = [], [], []
    for key, item in items.items():
        rec = seen.get(key)
        if rec is None:
            seen[key] = {"first_seen": now, "last_woken": now, "wakes": 1}
            fresh.append((key, item))
            continue
        wakes = int(rec.get("wakes") or 1)
        age_min = (now - int(rec.get("last_woken") or now)) / 60.0
        limit = STUCK_MINUTES if wakes >= MAX_WAKES else RETRY_MINUTES
        if age_min >= limit:
            rec["last_woken"] = now
            rec["wakes"] = wakes + 1
            (stuck if wakes >= MAX_WAKES else retry).append((key, item))

    state["items"] = seen
    state["last_scan"] = now

    wake = bool(fresh or retry or stuck)

    if status_only:
        print("TK Family scan  (%s)" % time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        print("  app queue      : %d pending" % len(docs))
        for path, files in drops.items():
            print("  %-38s: %d file(s)" % (path, len(files)))
        print("  tracked items  : %d" % len(seen))
        print("  would wake Jack: %s" % ("yes" if wake else "no"))
        if stuck:
            print("  STUCK          : %d item(s) past %d wakes"
                  % (len(stuck), MAX_WAKES))
        for key, item in items.items():
            rec = seen.get(key, {})
            print("    %-46s wakes=%s" % (key[:46], rec.get("wakes")))
        return 0

    save_state(state)

    if not wake:
        gate(False)
        return 0

    print("TK FAMILY: %d document(s) waiting." % len(items))
    print("")

    def line(key, item):
        if key.startswith("doc:"):
            note = (item.get("capture_note") or "").strip()
            who = item.get("captured_by_name") or item.get("captured_by") or "?"
            return ("  app document %s  captured by %s%s"
                    % (item.get("id"), who,
                       ('  note: "%s"' % note[:160]) if note else "  (no capture note)"))
        return ("  drive file %s  %r  in %s"
                % (item.get("id"), item.get("name"), item.get("_path")))

    if fresh:
        print("New since the last scan:")
        for key, item in fresh:
            print(line(key, item))
        print("")
    if retry:
        print("Still here after %d minutes, so the last attempt did not clear them:"
              % RETRY_MINUTES)
        for key, item in retry:
            print(line(key, item))
        print("")
    if stuck:
        print("STUCK. These have been raised %d or more times and are still "
              "sitting there. Do not just retry the same way: work out why it "
              "is not clearing, and if you cannot, tell Taylor what is wrong "
              "with these specific documents." % MAX_WAKES)
        for key, item in stuck:
            print(line(key, item))
        print("")

    print("Load the tk-family-documents skill and process these. Read each one, "
          "rename it to the MM-DD-YYYY - Vendor - Description pattern, file it "
          "by folder id, and PROPOSE the task or bill rather than creating it.")
    gate(True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # never leave cron without a gate
        print("tkfamily_scan crashed: %s" % exc, file=sys.stderr)
        if "--status" not in sys.argv[1:]:
            gate(False)
        sys.exit(1)
