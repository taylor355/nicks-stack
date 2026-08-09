#!/usr/bin/env python3
"""Restart the gateway without texting Taylor about it.

Hermes sends this to every chat with a running agent, at the very start of
stop():

    WARNING Gateway shutting down. Your current task will be interrupted.

It is not configurable. The per-session interrupt ping is deliberately ungated
in gateway/run.py, and only the home-channel broadcast honours the drain
marker's suppress_notification flag.

But the same source comment says the thing that matters: on a DRAINED shutdown
those pings "are empty by construction". The notice is sent to sessions that
have an agent mid-turn. If nothing is mid-turn, nobody is told anything.

So the fix is not to silence the message, it is to stop earning it. This waits
for in-flight scheduled runs to finish, then restarts. Taylor got eight of these
in an hour on 2026-08-09 purely because deploys landed on top of the
every-minute document job.

Usage:
    gw_restart.py                 wait for quiet, then restart
    gw_restart.py --timeout 240   how long to wait for quiet (default 180s)
    gw_restart.py --force         restart now even if a run is in flight
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

POLL_SECONDS = 5


def running_jobs() -> list:
    """Names of cron jobs with an execution in flight. Empty list on any
    failure, because a broken probe must not block a restart forever."""
    try:
        proc = subprocess.run(["hermes", "cron", "list"], capture_output=True,
                              text=True, timeout=60,
                              env=lib.hermes_child_env())
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    names, current = [], "?"
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            current = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Execution:") and "running" in stripped:
            names.append(current)
    return names


def wait_for_quiet(timeout: int) -> tuple[bool, list]:
    deadline = time.monotonic() + timeout
    busy = running_jobs()
    while busy and time.monotonic() < deadline:
        print("  waiting on: %s" % ", ".join(busy), flush=True)
        time.sleep(POLL_SECONDS)
        busy = running_jobs()
    return (not busy), busy


def restart() -> int:
    proc = subprocess.run(["supervisorctl", "restart", "hermes-gateway"],
                          capture_output=True, text=True, timeout=180)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    argv = sys.argv[1:]
    force = "--force" in argv
    timeout = 180
    if "--timeout" in argv:
        try:
            timeout = int(argv[argv.index("--timeout") + 1])
        except (IndexError, ValueError):
            print("--timeout needs a number of seconds")
            return 2

    if force:
        print("forcing restart, any in-flight run will be interrupted and "
              "Taylor will be told about it")
        return restart()

    print("waiting for scheduled runs to finish (up to %ds)" % timeout)
    quiet, busy = wait_for_quiet(timeout)
    if quiet:
        print("nothing in flight, restarting quietly")
    else:
        print("still running after %ds: %s" % (timeout, ", ".join(busy)))
        print("restarting anyway; Taylor will get an interrupt notice for these")
    return restart()


if __name__ == "__main__":
    sys.exit(main())
