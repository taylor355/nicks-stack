#!/usr/bin/env python3
"""Strip the self-confirmation preamble off scheduled messages before delivery.

Taylor asked for messages that read like a person texting: a bold title, then
prose, then bullets. The body Jack writes is now exactly that. What would not go
away is the sentence in front of it:

    Title and single parent both confirmed correct. This came straight from the
    scanner with no app card, so I'll surface it and ask before creating.

    **Les Schwab receipt filed for the Suburban**

    $83.50 paid on August 6 for a tire rotation ...

That opening is the model reassuring itself, and it lands in front of the only
line he wanted. It survived four escalating instructions: "do this silently",
"leave out the proof", "the first characters of your response are the bold
title", and finally a literal "your response must BEGIN with the two characters
**". Every one produced a perfect body and the same preamble. It is a strong
behavioural prior, not a misunderstanding, so it gets solved mechanically
instead of by asking a fifth time.

WHAT THIS DOES

Hermes builds the outbound text in cron/scheduler.py. This inserts one call at
that point which drops everything above the first Markdown bold heading.

WHY IT IS SAFE

  * It only fires when a `**` title exists later in the message. No title, no
    change.
  * It only drops a SHORT lead-in (MAX_TRIM chars). A long opening is real
    content and is left alone.
  * It never touches a message that already starts with the title.
  * On any failure it returns the original text. Delivery is never blocked.
  * It is gated by cron.trim_preamble in config.yaml. Set it false and the
    patch is inert without unpatching anything.

MAINTENANCE

This edits a vendored Hermes file, so `hermes update` can revert it. That is
why it is idempotent and re-applied by bootstrap.sh on every deploy, the same
contract the Latitude telemetry patch already uses. `--check` reports state
without writing, and verify.sh calls it.

Usage:
    cron_trim_patch.py            apply if missing (idempotent)
    cron_trim_patch.py --check    report only, exit 0 if applied
    cron_trim_patch.py --revert   remove the patch
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/cron/scheduler.py")
MARKER = "NICKS_STACK_TRIM_PREAMBLE"

ANCHOR = "    else:\n        delivery_content = content\n"

HELPER = '''

# --- {marker} (nicks-stack) -------------------------------------------
# Drop a short self-confirmation lead-in above the first bold title. See
# scripts/platform/cron_trim_patch.py for why this is mechanical and not a
# prompt rule. Fails open: any problem returns the text unchanged.
def _nicks_stack_trim_preamble(text):
    MAX_TRIM = 400
    try:
        if not text:
            return text
        stripped = text.lstrip()
        if stripped.startswith("**"):
            return text
        import re as _re
        m = _re.search(r"^\\*\\*", stripped, _re.M)
        if not m or m.start() == 0:
            return text
        lead = stripped[:m.start()]
        if len(lead) > MAX_TRIM:
            return text
        rest = stripped[m.start():].strip()
        return rest or text
    except Exception:
        return text
# --- end {marker} -----------------------------------------------------
'''.format(marker=MARKER)

PATCHED = """    else:
        delivery_content = content
        # {marker}: gate on cron.trim_preamble (default on).
        try:
            _trim_on = True
            if user_cfg is not None:
                _trim_on = (user_cfg.get("cron", {{}}) or {{}}).get("trim_preamble", True)
            if _trim_on:
                delivery_content = _nicks_stack_trim_preamble(delivery_content)
        except Exception:
            pass
""".format(marker=MARKER)


def state(text: str) -> str:
    if MARKER not in text:
        return "absent"
    if PATCHED.rstrip() in text and HELPER.strip().splitlines()[1] in text:
        return "applied"
    return "partial"


def apply_patch() -> int:
    if not TARGET.exists():
        print("cron scheduler not found at %s" % TARGET)
        return 1
    text = TARGET.read_text()
    st = state(text)
    if st == "applied":
        print("already applied")
        return 0
    if st == "partial":
        print("a partial or older patch is present; revert first")
        return 1
    if text.count(ANCHOR) != 1:
        print("anchor not found exactly once (Hermes changed shape) "
              "- not patching, delivery is unaffected")
        return 1

    helper_anchor = "def _cron_mirror_delivery_enabled"
    if text.count(helper_anchor) < 1:
        # Fall back to inserting the helper just before the function that uses
        # it rather than failing outright.
        helper_anchor = ANCHOR
    idx = text.index(helper_anchor)
    line_start = text.rfind("\n", 0, idx) + 1
    text = text[:line_start] + HELPER.lstrip("\n") + "\n" + text[line_start:]
    text = text.replace(ANCHOR, PATCHED, 1)

    backup = TARGET.with_suffix(".py.nicks-stack-bak")
    if not backup.exists():
        backup.write_text(TARGET.read_text())
    TARGET.write_text(text)

    import py_compile
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except Exception as exc:
        TARGET.write_text(backup.read_text())
        print("patched file did not compile, reverted: %s" % exc)
        return 1
    print("applied")
    return 0


def revert() -> int:
    backup = TARGET.with_suffix(".py.nicks-stack-bak")
    if not backup.exists():
        print("no backup to revert to")
        return 1
    TARGET.write_text(backup.read_text())
    print("reverted")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--revert" in argv:
        return revert()
    if "--check" in argv:
        if not TARGET.exists():
            print("scheduler missing")
            return 1
        st = state(TARGET.read_text())
        print(st)
        return 0 if st == "applied" else 1
    return apply_patch()


if __name__ == "__main__":
    sys.exit(main())
