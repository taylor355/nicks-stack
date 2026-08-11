---
name: mac-bridge
description: Use when a request would produce a FILE (PDF, HTML page, spreadsheet, deck, script, repo change), when Taylor asks to be texted or asks about iMessage, when he asks to use the local/free model, or when he asks what his Mac mini is doing for him. Covers handing build work to Claude Code on his own subscription, Codex as the backup lane, Ollama as the free tier, and the rules for what may be sent by iMessage rather than Telegram.
version: 1.0.0
author: Nicks Stack
metadata:
  hermes:
    tags: [mac-mini, claude-code, codex, ollama, imessage, bluebubbles, build, routing, tailscale]
    related_skills: [provider-routing, coding-agent-routing, tk-family-documents]
---

# The Mac mini

Taylor has a Mac mini at home that is always on. Three things live there
because they cannot live on this VM, and each one solves a real problem:

| On the Mac | What it gives him | Why not here |
|---|---|---|
| **Claude Code** | Build work on his own flat-rate subscription | A consumer plan is a browser login on a machine he owns. Driving it through an API key is forbidden, so the work runs where the login is. |
| **Codex** | The backup lane when the Claude limit is hit | Same reason. |
| **Ollama** | Free, private inference | This VM has 92% CPU steal. A 1.7B model never finished warming up in twenty minutes. Apple Silicon has real cores. |
| **BlueBubbles** | iMessage | iMessage is macOS-only. There is no cloud substitute. |

You reach it over **Tailscale**, a private mesh between two machines Taylor
owns. Nothing is published to the internet.

Your entire interface is one script:

```bash
python3 /root/.hermes/scripts/platform/mac_bridge.py status
python3 /root/.hermes/scripts/platform/mac_bridge.py build "<task>" --confirm
python3 /root/.hermes/scripts/platform/mac_bridge.py fetch <remote-path>
python3 /root/.hermes/scripts/platform/mac_bridge.py text "<message>"
python3 /root/.hermes/scripts/platform/mac_bridge.py models
```

## Before you promise anything

`status` is the only thing that tells you the truth, and it tells it by making
real calls — a live one-turn prompt to Claude Code, a real `/api/tags` to
Ollama, a real `/server/info` to BlueBubbles. Nothing here reads a config file
and calls it healthy.

If the bridge says it is not set up, say exactly that and stop:

> The Mac mini bridge isn't set up yet, so I can't hand this off. Want me to
> do it here instead?

**Never say a build ran on his subscription unless `status` proved the
specialist answered.** If Claude Code is not logged in, or its plan limit is
hit, say which one and offer Codex. If both are down, offer to do the work
here and tell him it will be billed to the API.

## Handing off a build

A request is a **build** when the deliverable is a file rather than an answer:
a PDF, an HTML page, a spreadsheet, a slide deck, a script, a document he will
open in something, or a change to a repository. "Summarise this lease" is not
a build. "Turn this lease into a one-page PDF summary I can send the tenant"
is.

The sequence is always the same:

1. **Say what you're about to build, in one short message**, and get a yes.
   Build never runs unapproved — the script refuses without `--confirm`, and
   you must not add `--confirm` on his behalf. This is the one place where
   asking first is required rather than polite: he is spending his own plan.
2. **Write the task as a complete brief.** The specialist on the Mac gets your
   text and nothing else — no memory, no chat history, no access to Drive.
   Include the content it needs inline. A vague brief comes back as a vague
   artifact and burns a run.
3. **Run it**, defaulting to `claude-code`. Add `--specialist codex` only if
   Claude Code reported a limit.
4. **Fetch the artifact** back with `fetch`, then attach it on Telegram.
5. **Report the result the way you report anything else**: bold title, a
   sentence of what it is, and the file. No run IDs, no directory paths, no
   log tails unless something failed.

If it produced no files, say so plainly and show him the last few lines of
what the specialist said. Do not paper over an empty run.

### What this saves

Building a document inline costs metered API tokens on every draft. Building
it on the Mac costs nothing beyond the subscription he already pays for. That
is the whole reason this exists, so prefer the handoff for anything
file-shaped and substantial. Do not hand off something trivial — a two-line
snippet is faster answered directly than shipped across a network.

## iMessage

**Telegram is the conversation. iMessage is a notice channel.** Taylor's own
framing: *"Not everything, but daily updates, I'd like to have the ability to
send a few things via text."*

He cannot reply to these usefully — they are one-way by design. So a text must
stand alone and must be worth interrupting a day for.

**May be texted:**

- The morning brief, trimmed to what actually changed
- A bill or filing due today that nobody has actioned
- A calendar conflict inside the next two hours
- A finished build he asked for by text
- Anything he explicitly said "text me" about

**Never texted:**

- Tool progress, job status, "working on it" — these do not go anywhere at all
- Anything already sent on Telegram in the same hour
- McKell's household items. He has said this twice.
- Errors and warnings. Telegram, or stay quiet.
- Anything over the character cap — send the short version by text and put the
  full version on Telegram

There is a **hard cap of six texts a day**, enforced by the script, not by
your judgement. When you hit it the send fails with `daily text cap reached`.
That is not an error to route around — it means the seventh thing belongs on
Telegram. Never pass `--force` unless Taylor asked for that specific message
in that moment.

Write a text like a text: no bold titles, no bullets, no markdown. One or two
sentences. It is going to a phone lock screen.

> Two things due today: the Cavazos Bop sales tax filing and the Ford recall
> appointment. Both still open. Details on Telegram.

## The free tier

`local` mode routes to Ollama on the Mac. It is free and nothing leaves his
house, and that is the entire case for it. An 8B model is well short of Sonnet
on the judgement work you usually do, so `local` is **manual only** — Taylor
asks for it, you never demote work into it and never escalate out of it.

Good uses: bulk classification, redacting something before it goes to a
vendor, summarising a document he would rather not send anywhere, and staying
useful if an API is down.

`mac_bridge.py models` reports what is actually pulled. If nothing is, say so
and name the pull command rather than guessing a model.

## What already texts him, and when

These run on the Mac itself under launchd, not through you. Know them, because
Taylor will ask "what sent me that" and because they are the reason your own
text budget is small:

| Time | Job | What it sends |
|---|---|---|
| 9:30am | daily-wisdom | Code of the West principle, quote, a fresh reflection. Also to McKell. |
| 9:00pm | nightly-briefing | Tomorrow's schedule, top three, overdue tasks, conflicts |
| 9:00pm | four-f-checkin | A link to his Four F check-in |

Three texts a day, and two of them land together at 9:00pm. That is already
most of what a person will tolerate, so your own six-a-day cap is a ceiling,
not a target: assume the day's budget is nearly spent before you use any of it.

A 10:30pm nudge existed and Taylor removed it the same evening he saw it. Read
that as the standing preference it is: he will take a scheduled reminder, but
not a second message chasing the first. Do not propose follow-up nudges, and
never text him twice about the same thing.

## When he asks where to set this up

The answer is the Mac mini, at a keyboard. Not this VM, not claude.ai, not his
phone — Claude Code's login is a browser sign-in on the machine that will run
it, and BlueBubbles needs Full Disk Access granted in System Settings by hand.
The full walkthrough is in `references/mac-mini-setup.md`; hand him that
rather than improvising steps.
