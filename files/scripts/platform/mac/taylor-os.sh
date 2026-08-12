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
OS_DIR="$HOME/Documents/Taylor_OS"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$OS_DIR" 2>/dev/null || { echo "Taylor OS not found at $OS_DIR"; exit 1; }

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
    echo "  GitHub login is not valid. Push will fail until you run:"
    echo "      gh auth login"
  fi

  check_routing

  echo
  echo "  local HEAD:  $(git rev-parse --short HEAD)  $(git log -1 --format=%s | cut -c1-55)"
  echo "  on GitHub:   $(git rev-parse --short origin/main 2>/dev/null || echo unknown)"
  echo "  files:       $(find . -name '*.md' -not -path './.git/*' | wc -l | tr -d ' ')"
  echo
  echo "  Jack syncs from GitHub hourly. Anything not pushed does not exist to him."
}

cmd_push() {
  local msg="${1:-}"
  if [ -z "$msg" ]; then
    echo "Give it a message:  taylor-os push \"what changed\""; exit 2
  fi
  if ! auth_ok; then
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
  *)      echo "usage: taylor-os [status|push \"message\"]"; exit 2 ;;
esac
