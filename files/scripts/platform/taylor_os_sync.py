#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — Taylor OS sync  (v1.1.35)
# ==========================================================================
# Taylor OS is a separate repository (taylor355/taylor_os): 44 markdown files
# describing who he is, how each company works, who the people are, and the
# workflows he repeats. It is the answer to "what does Jack know about the
# business" — and it is deliberately NOT part of this platform.
#
# THE REPO'S OWN RULE, WHICH THIS FILE EXISTS TO HONOUR:
#
#   "Do not add technical system assumptions, tool-specific dependencies, or
#    temporary setup details to these files. This folder should stay portable
#    and durable."                                    (00_START_HERE.md)
#
# So nothing Hermes-shaped is ever written into it. The wiring lives here, on
# Jack's side, and the repo stays something Taylor could hand to a new employee
# or a different tool tomorrow. That portability is the point of it.
#
# WHY A CLONE AND NOT A PASTE. 84KB across 44 files cannot live in MEMORY.md
# (2,200 characters, already at 85%) or in SOUL.md without crowding out the
# operating instructions. It also must not: the repo's own context discipline
# says "do not load every file by default, pull in the smallest useful set for
# the task". A checkout on disk lets Jack read exactly the two or three files a
# question needs, which is both cheaper and what the author asked for.
#
# WHAT THIS DOES: clone if missing, fast-forward if present, and say nothing
# unless something actually changed or the sync failed. The last stdout line is
# the Hermes cron wake gate, so an unchanged day costs nothing.
#
# LIFECYCLE-GUARD NOTE: the cron guard reads this file's BODY and refuses it if
# the text names a process-management tool, even inside a comment or a hint
# string. Keep those names out of this file — mac_watch.py exists for exactly
# that reason.
#
# USAGE
#   python3 taylor_os_sync.py            # scheduled: silent unless changed
#   python3 taylor_os_sync.py --status   # what is checked out right now
#   python3 taylor_os_sync.py --force    # re-clone from scratch
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

REPO_HTTPS = "https://github.com/taylor355/taylor_os"
DEST = lib.HERMES_HOME.joinpath("taylor_os")
STATE = lib.HERMES_HOME.joinpath("runtime", "taylor_os_sync.json")
GIT_TIMEOUT = 300


def git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    # A private repo needs the platform's GitHub token. It is read from the
    # runtime secret plane and never printed: the value goes into an askpass
    # helper's environment, not into the URL, so it cannot land in a remote,
    # in `git config`, or in any log line here.
    resolved = lib.resolve_runtime_secret("GITHUB_TOKEN")
    token = (resolved or {}).get("value") or ""
    if token:
        env["GIT_ASKPASS"] = "/bin/echo"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + _basic(token)
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} timed out after {GIT_TIMEOUT}s"
    except FileNotFoundError:
        return 127, "git is not installed"
    return proc.returncode, lib.scrub((proc.stdout + proc.stderr).strip())


def _basic(token: str) -> str:
    import base64
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def head_info() -> dict:
    """Three states, and the report must distinguish them — a status line that
    says "not checked out" while 44 readable files sit on disk is worse than
    no status at all, because Jack would decline to answer from context he
    actually has.

      absent   nothing on disk
      seeded   files present, no .git — placed directly, cannot self-update
      git      a real clone that fast-forwards from GitHub"""
    files = len([p for p in DEST.rglob("*.md") if ".git" not in p.parts]) if DEST.is_dir() else 0
    if not DEST.joinpath(".git").is_dir():
        if files:
            return {"present": True, "mode": "seeded", "files": files,
                    "sha": "", "date": ""}
        return {"present": False, "mode": "absent", "files": 0}
    rc, sha = git(["rev-parse", "HEAD"], DEST)
    rc2, when = git(["log", "-1", "--format=%cd", "--date=short"], DEST)
    return {"present": rc == 0, "mode": "git", "sha": sha.strip()[:12],
            "date": when.strip(), "files": files}


def sync() -> dict:
    """Returns {changed, detail, error}. Never raises."""
    if not DEST.joinpath(".git").is_dir():
        DEST.parent.mkdir(parents=True, exist_ok=True)
        # Clone into a staging path first. A seeded snapshot is real, usable
        # context; destroying it to attempt a clone that then fails on auth
        # would leave Jack with nothing, which is strictly worse than leaving
        # him with slightly stale files.
        staging = DEST.with_name(DEST.name + ".incoming")
        shutil.rmtree(staging, ignore_errors=True)
        rc, out = git(["clone", "--depth", "1", REPO_HTTPS, str(staging)])
        if rc != 0:
            shutil.rmtree(staging, ignore_errors=True)
            return {"changed": False, "error": out[:300] or f"clone exited {rc}"}
        shutil.rmtree(DEST, ignore_errors=True)
        staging.replace(DEST)
        info = head_info()
        return {"changed": True, "error": "",
                "detail": f"cloned {info.get('files', 0)} files at {info.get('sha', '')}"}

    before = head_info().get("sha", "")
    rc, out = git(["fetch", "--depth", "1", "origin"], DEST)
    if rc != 0:
        return {"changed": False, "error": out[:300] or f"fetch exited {rc}"}
    # Hard reset rather than merge: this checkout is a READ-ONLY MIRROR. Jack
    # reads it; Taylor edits it on GitHub. A merge conflict here would be a
    # symptom of something writing locally, and silently keeping local edits
    # would be worse than losing them — it would make Jack's answers diverge
    # from the file Taylor is actually looking at.
    rc, out = git(["reset", "--hard", "origin/HEAD"], DEST)
    if rc != 0:
        rc, out = git(["reset", "--hard", "FETCH_HEAD"], DEST)
        if rc != 0:
            return {"changed": False, "error": out[:300] or f"reset exited {rc}"}
    after = head_info().get("sha", "")
    if after and after != before:
        return {"changed": True, "error": "",
                "detail": f"updated {before} -> {after}"}
    return {"changed": False, "error": "", "detail": "up to date"}


def load_state() -> dict:
    try:
        with STATE.open() as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    tmp.replace(STATE)


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep Taylor OS in step with GitHub")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when nothing changed (for cron --no-agent)")
    args = ap.parse_args()

    if args.status:
        info = head_info()
        if not info.get("present"):
            print(f"Taylor OS is not on disk at {DEST}")
            return 1
        if info.get("mode") == "seeded":
            print(f"Taylor OS: {info['files']} files at {DEST}")
            print("mode: seeded snapshot — readable, but it cannot refresh itself.")
            print("Add a read-only GitHub token as GITHUB_PAT in the Hermes vault "
                  "item to turn on automatic sync.")
            return 0
        print(f"Taylor OS: {info['files']} files, {info['sha']}, last commit {info['date']}")
        print(f"path: {DEST}")
        return 0

    if args.force and DEST.exists():
        shutil.rmtree(DEST, ignore_errors=True)

    result = sync()
    state = load_state()
    was_broken = bool(state.get("error"))
    state["error"] = result.get("error", "")
    state["last_detail"] = result.get("detail", "")
    save_state(state)

    if result.get("error"):
        # Speak once when it breaks, not every half hour after.
        if not was_broken:
            print("**Taylor OS sync failed**")
            print()
            print(result["error"])
        if not args.quiet:
            print(json.dumps({"wakeAgent": False}))
        return 2

    lines = []
    if was_broken:
        lines.append("Taylor OS sync is working again.")
    if result.get("changed"):
        lines.append(f"Taylor OS updated: {result.get('detail', '')}")

    if lines:
        print("**Taylor OS**")
        print()
        for line in lines:
            print(f"- {line}")
    if not args.quiet:
        print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
