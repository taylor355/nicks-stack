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

  log "start $id ($spec)"
  cd "$run_dir" || return

  case "$spec" in
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
      ! -name 'DONE.tmp' -newer TASK.md -print 2>/dev/null | sed 's|^\./||'
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
