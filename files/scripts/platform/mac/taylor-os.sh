#!/bin/bash
# ==========================================================================
# taylor-os — publish Taylor OS, and see what Jack actually has
# ==========================================================================
# Taylor OS lives in ~/Documents/Taylor_OS and is the source of truth. Jack
# holds a read-only mirror that fast-forwards from GitHub every hour, so the
# ONLY thing that moves work from this Mac to Jack is a push.
#
# That is worth making a single command, because the failure is silent: on
# 2026-08-10 a commit sat unpushed for a day because `gh`'s token had expired.
# Nothing errored anywhere Taylor would see it. Jack kept syncing a stream that
# never moved and reported "up to date", which was true and useless.
#
#   taylor-os status    uncommitted, unpushed, what Jack has, routing coverage
#   taylor-os push      commit everything and push, with a message
# ==========================================================================
set -uo pipefail
# NOT ~/Documents. macOS TCC denies background (launchd) processes access to
# Documents, Desktop and Downloads, and it denies them silently — the first
# scheduled run reported "nothing to publish" while git was actually saying
# "Operation not permitted". A wrong answer, not an error. ~/Taylor_OS is
# outside that protection; ~/Documents/Taylor_OS remains as a symlink so
# nothing Taylor opens by habit breaks.
OS_DIR="$HOME/Taylor_OS"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$OS_DIR" 2>/dev/null || { echo "Taylor OS not found at $OS_DIR"; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "Cannot read the git repository at $OS_DIR."
  echo "If this ran from a schedule, macOS is probably denying folder access."
  exit 1
}

# `gh auth status` reads the login keychain, and macOS denies keychain access
# to an ssh session — so from a remote shell it reports "the token is invalid"
# for a token that is perfectly good. That false negative was reported to
# Taylor twice and sent him to re-run `gh auth login` he never needed. The same
# GUI-session boundary that blocks Claude Code's credential; it just lies more
# convincingly here, because gh phrases a permission denial as an auth failure.
#
# So: only trust the answer when we are in a session that can actually see the
# keychain. From ssh, say we cannot tell rather than saying it is broken.
in_gui_session() { [ -z "${SSH_CONNECTION:-}" ]; }
auth_ok() { gh auth status >/dev/null 2>&1; }

# Nothing writes to Taylor OS automatically — Jack's copy is read-only by
# design. So the one thing Taylor has to remember is that a NEW top-level
# folder must also appear in AGENTS.md, or Jack has no route to it and will
# quietly answer from general context instead. Remembering is a bad job for a
# person, so the tool checks it.
check_routing() {
  local missing=0
  for d in */; do
    d="${d%/}"
    case "$d" in .git|.github) continue ;; esac
    if ! grep -q "$d" AGENTS.md 2>/dev/null; then
      [ "$missing" -eq 0 ] && echo && echo "  NOT ROUTED — AGENTS.md never mentions these, so Jack will not look in them:"
      echo "    $d"
      missing=1
    fi
  done
  if [ "$missing" -eq 1 ]; then
    echo "    Add a line for each to the \"How To Find The Right Context\" list in AGENTS.md."
  fi
  return 0
}

# ── auto ────────────────────────────────────────────────────────────────────
# Runs on a schedule so Taylor never has to remember to publish. His words:
# "I don't want to keep missing context over time."
#
# TWO GUARDS make unattended committing safe here:
#
#   QUIET PERIOD. It only commits when nothing has been modified for 20
#   minutes. Without that it would commit mid-sentence, and Jack would read a
#   half-written thought as though Taylor meant it.
#
#   LOUD ON FAILURE. A silent failure is what caused this whole problem: a
#   commit sat unpushed for a day because gh's token expired and nothing said
#   so. So when a push fails, this texts him once — and only once per streak,
#   because a broken token stays broken and he does not need reminding hourly.
QUIET_MINUTES=20
STATE="$HOME/Scripts/.taylor_os_auto_state"

notify_once() {
  local msg="$1"
  [ "$(cat "$STATE" 2>/dev/null)" = "failing" ] && return 0
  echo failing > "$STATE"
  /usr/bin/osascript <<OSA >/dev/null 2>&1
tell application "Messages"
  set svc to 1st account whose service type = iMessage
  set bud to participant "+13852225570" of svc
  send "$msg" to bud
end tell
OSA
}

cmd_auto() {
  if [ -z "$(git status --porcelain)" ]; then
    # Clean tree, but a previous commit may still be unpushed.
    if [ "$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)" = "0" ]; then
      echo "$(date '+%F %T') nothing to publish"; return 0
    fi
  else
    # Newest modification across the working tree, in seconds since epoch.
    local newest now age
    newest="$(find . -newer .git/HEAD -type f -not -path './.git/*' -exec stat -f %m {} \; 2>/dev/null | sort -rn | head -1)"
    now="$(date +%s)"
    if [ -n "$newest" ]; then
      age=$(( (now - newest) / 60 ))
      if [ "$age" -lt "$QUIET_MINUTES" ]; then
        echo "$(date '+%F %T') still editing (${age}m since last change) — waiting"
        return 0
      fi
    fi
    git add -A
    git commit -q -m "Taylor OS update $(date '+%Y-%m-%d %H:%M')" || true
  fi

  if git push -q origin main 2>/dev/null; then
    echo "$(date '+%F %T') pushed"
    echo ok > "$STATE"
    return 0
  fi
  echo "$(date '+%F %T') PUSH FAILED"
  notify_once "Taylor OS auto-publish failed to push. Jack is not seeing your latest edits."
  return 1
}

cmd_status() {
  echo "Taylor OS  ($OS_DIR)"
  echo

  local dirty ahead
  dirty="$(git status --porcelain | wc -l | tr -d ' ')"
  git fetch origin >/dev/null 2>&1
  ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"

  if [ "$dirty" != "0" ]; then
    echo "  $dirty uncommitted change(s):"
    git status --porcelain | sed 's/^/    /'
    echo
  else
    echo "  working tree clean"
  fi

  if [ "$ahead" = "0" ]; then
    echo "  everything is pushed"
  else
    echo "  $ahead commit(s) NOT pushed — Jack cannot see these"
  fi

  if ! auth_ok; then
    echo
    if in_gui_session; then
      echo "  GitHub login is not valid. Push will fail until you run:"
      echo "      gh auth login"
    else
      echo "  (GitHub login cannot be checked from a remote shell — macOS"
      echo "   blocks keychain access over ssh. Run this on the Mac to know.)"
    fi
  fi

  check_routing

  echo
  echo "  local HEAD:  $(git rev-parse --short HEAD)  $(git log -1 --format=%s | cut -c1-55)"
  echo "  on GitHub:   $(git rev-parse --short origin/main 2>/dev/null || echo unknown)"
  echo "  files:       $(find . -name '*.md' -not -path './.git/*' | wc -l | tr -d ' ')"
  if [ -f "$HOME/Scripts/taylor_os_auto.log" ]; then
    echo
    echo "  auto-publish (every 2h):  $(tail -1 "$HOME/Scripts/taylor_os_auto.log")"
  fi

  echo
  echo "  Jack syncs from GitHub hourly. Anything not pushed does not exist to him."
}

cmd_push() {
  local msg="${1:-}"
  if [ -z "$msg" ]; then
    echo "Give it a message:  taylor-os push \"what changed\""; exit 2
  fi
  if ! auth_ok && in_gui_session; then
    echo "GitHub login is not valid. Run: gh auth login"; exit 1
  fi
  check_routing
  git add -A
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git commit -m "$msg" || exit 1
  fi
  git push origin main || exit 1
  echo
  echo "Pushed. Jack picks it up within the hour."
  echo "To not wait, tell him: sync Taylor OS now"
}

case "${1:-status}" in
  status) cmd_status ;;
  push)   shift; cmd_push "${1:-}" ;;
  auto)   cmd_auto ;;
  *)      echo "usage: taylor-os [status|push \"message\"|auto]"; exit 2 ;;
esac
