#!/bin/bash
# ==========================================================================
# Jack build runner — runs on Taylor's Mac, inside the GUI login session
# ==========================================================================
# WHY THIS EXISTS AT ALL. Jack can ssh into this Mac, and for Codex that is
# enough. It is not enough for Claude Code, because Claude Code keeps its
# subscription token in the login Keychain, and macOS refuses Keychain access
# to an ssh session:
#
#   security: SecKeychainCopySettings login.keychain-db:
#             User interaction is not allowed.
#
# So `claude -p` over ssh reports "Not logged in" even though the user is
# signed in — it cannot read its own credential. That is macOS policy, not a
# misconfiguration, and no ssh flag gets around it.
#
# A LaunchAgent does. launchd starts this script in the Aqua (GUI) session as
# the logged-in user, where the Keychain is unlocked and every tool behaves
# exactly as it does in Terminal. Jack drops a task file over ssh; this picks
# it up; results land back in the same tree for Jack to fetch.
#
# The queue is a directory. No daemon port, no auth of its own, nothing
# listening on the network — the only way to enqueue work is to already have
# ssh access to this account.
# ==========================================================================
set -uo pipefail

ROOT="$HOME/JackBuilds"
QUEUE="$ROOT/queue"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

mkdir -p "$QUEUE"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

run_one() {
  local taskfile="$1"
  local id spec run_dir
  id="$(basename "$taskfile" .task)"
  run_dir="$ROOT/$id"

  # The .task file is: line 1 = specialist, everything after = the brief.
  # Deliberately not JSON — a brief containing quotes, newlines or braces must
  # survive verbatim, and a two-part plain file cannot be mis-parsed.
  spec="$(head -1 "$taskfile")"
  mkdir -p "$run_dir"
  tail -n +2 "$taskfile" > "$run_dir/TASK.md"
  rm -f "$taskfile"
  # osascript cannot safely carry a multi-line body inline, so the text goes
  # to a file the script reads. Quotes, apostrophes and newlines survive.
  tail -n +2 "$run_dir/TASK.md" > "$run_dir/BODY.txt" 2>/dev/null || true

  log "start $id ($spec)"
  cd "$run_dir" || return

  case "$spec" in
    imessage-probe)
      # Health check that SENDS NOTHING. It asks Messages for the name of the
      # iMessage account, which fails loudly if Messages is signed out, not
      # running, or Automation permission was revoked — the three ways this
      # lane dies quietly. Sending a probe text instead would work, and would
      # also text Taylor every time the watchdog runs, which is the opposite
      # of what he asked for.
      # Do NOT try to render the account into text. `name of svc` returns a
      # class icsv id that AppleScript refuses to coerce (-1700), which made
      # the probe report a broken lane while sending worked perfectly. Binding
      # the account is the whole test: it fails if Messages is signed out, not
      # running, or Automation permission was revoked.
      /usr/bin/osascript > RESULT.log 2>&1 <<'OSAP'
tell application "Messages"
  set svc to 1st account whose service type = iMessage
  set _probe to id of svc
  return "IMESSAGE-OK"
end tell
OSAP
      ;;
    imessage)
      # Line 1 of TASK.md is the recipient; the rest is the message body.
      #
      # WHY APPLESCRIPT AND NOT BLUEBUBBLES. Taylor asked for one-way delivery
      # of a few things a day. BlueBubbles exists to expose the WHOLE message
      # history over HTTP, which means a server app, Full Disk Access to
      # chat.db, a password to manage, and one more thing that can quit after
      # an update. AppleScript needs none of that: Messages is already signed
      # in, nothing listens on a port, and no history is read.
      #
      # It only works from HERE. Over ssh, Apple events are refused outright
      # (-1743, "Not authorized to send Apple events"), the same GUI-session
      # boundary that blocks Claude Code's Keychain.
      TO="$(head -1 TASK.md)"
      BODY="$(tail -n +2 TASK.md)"
      /usr/bin/osascript > RESULT.log 2>&1 <<OSA
on run
  set theTo to "$TO"
  set theBody to (do shell script "cat " & quoted form of "$run_dir/BODY.txt")
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    set bud to participant theTo of svc
    send theBody to bud
  end tell
  return "SENT"
end run
OSA
      ;;
    codex)
      codex exec --skip-git-repo-check --sandbox workspace-write \
        "$(cat TASK.md)" > RESULT.log 2>&1 < /dev/null
      ;;
    *)
      claude -p "$(cat TASK.md)" --max-turns 40 \
        --permission-mode acceptEdits --output-format text \
        > RESULT.log 2>&1 < /dev/null
      ;;
  esac
  local rc=$?

  # Written LAST and atomically: Jack polls for DONE, so it must never appear
  # before the artifacts it describes are complete on disk.
  {
    echo "exit=$rc"
    echo "---ARTIFACTS---"
    find . -type f ! -name 'TASK.md' ! -name 'RESULT.log' ! -name 'DONE' \
      ! -name 'BODY.txt' ! -name 'DONE.tmp' -newer TASK.md -print 2>/dev/null \
      | sed 's|^\./||'
  } > "$run_dir/DONE.tmp"
  mv "$run_dir/DONE.tmp" "$run_dir/DONE"
  log "done $id exit=$rc"
}

log "build runner up (queue: $QUEUE)"
while true; do
  shopt -s nullglob
  for t in "$QUEUE"/*.task; do
    run_one "$t"
  done
  shopt -u nullglob
  sleep 3
done
