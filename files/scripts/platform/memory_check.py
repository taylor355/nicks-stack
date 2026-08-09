#!/usr/bin/env python3
"""Report how close Jack's working memory is to the cap that truncates it.

`memory.memory_char_limit` in config.yaml is 2,200 and Hermes enforces it by
CUTTING the file. No error, no warning, no log line. A MEMORY.md that grows past
the limit does not fail, it quietly stops containing the oldest things Jack
learned, and the first sign is him not knowing something he was told last month.

Found at 86% on 2026-08-09, entirely because the people roster was duplicated
from SOUL.md into MEMORY.md. That is the shape of the problem: it does not fill
with live state, it fills with reference material that already lives somewhere
better.

The same silence applies to the user profile (`user_char_limit`), so both are
checked.

Usage:
    memory_check.py            human summary
    memory_check.py --json     machine readable, for verify.sh
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

WARN_AT = 0.80     # advisory
CRIT_AT = 0.95     # about to start losing things

FILES = (
    ("MEMORY.md", ("memories", "MEMORY.md"), "memory_char_limit", 2200),
    ("user profile", ("memories", "user.md"), "user_char_limit", 1375),
)


def report() -> dict:
    cfg = lib.load_yaml(lib.CONFIG_FILE).get("memory") or {}
    out = {"entries": [], "worst": 0.0, "state": "ok"}
    for label, parts, key, default in FILES:
        path = lib.HERMES_HOME.joinpath(*parts)
        limit = int(cfg.get(key) or default)
        try:
            chars = len(path.read_text())
        except OSError:
            out["entries"].append({"label": label, "path": str(path),
                                   "exists": False, "chars": 0,
                                   "limit": limit, "pct": 0.0})
            continue
        pct = (float(chars) / limit) if limit else 0.0
        entry = {"label": label, "path": str(path), "exists": True,
                 "chars": chars, "limit": limit, "pct": round(pct, 3),
                 "headroom": limit - chars}
        out["entries"].append(entry)
        out["worst"] = max(out["worst"], pct)

    if out["worst"] >= CRIT_AT:
        out["state"] = "critical"
    elif out["worst"] >= WARN_AT:
        out["state"] = "warn"
    return out


def main() -> int:
    data = report()
    if "--json" in sys.argv[1:]:
        print(json.dumps(data))
        return 0

    print("Jack's memory headroom")
    print("")
    for e in data["entries"]:
        if not e["exists"]:
            print("  %-14s not present (%s)" % (e["label"], e["path"]))
            continue
        bar_len = 34
        filled = min(bar_len, int(e["pct"] * bar_len))
        bar = "#" * filled + "." * (bar_len - filled)
        print("  %-14s %s  %d of %d chars (%.0f%%), %d left"
              % (e["label"], bar, e["chars"], e["limit"],
                 e["pct"] * 100, e["headroom"]))
    print("")
    if data["state"] == "critical":
        print("CRITICAL. Hermes truncates silently at the limit, so the oldest")
        print("lines are about to stop existing without any error anywhere.")
        print("Move reference material into the Notion hub and prune resolved")
        print("threads. Live state only.")
    elif data["state"] == "warn":
        print("Getting full. Nothing is lost yet. The usual cause is reference")
        print("material that belongs in Notion, not live state.")
    else:
        print("Fine. Live state only is the rule; depth belongs in Notion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
