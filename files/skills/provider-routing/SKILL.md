---
name: provider-routing
description: "Use when the user asks to change or inspect the model route — /mode fast|smart|deep|build|local, 'which model are you using', 'switch to deep', 'use the cheap model' — or when a task's cost/quality tier should be chosen deliberately. Covers the nicks-stack-route CLI, the five modes, confirmation rules for premium and coding routes, and fallback policy."
version: 1.0.0
author: Nick's Stack
license: MIT
metadata:
  hermes:
    tags: [routing, providers, models, cost-control, modes, anthropic, openrouter]
    related_skills: [coding-agent-routing, secret-manager-setup]
---

# Provider Routing

Five named modes, one declarative map, no automatic switching. The map is
`/root/.hermes/routing.yaml`; the CLI is `nicks-stack-route`. Every route is
inspectable — if you cannot say which provider and model answered, something is
wrong.

## Telegram syntax

Hermes has no custom slash commands: `/new`, `/reset`, `/status`, `/mcp`,
`/reasoning` and `/reload` are built in, and a plugin cannot add `/mode`. So
`/mode …` arrives as ordinary message text and **you** act on it. Treat these as
the command, case-insensitively, whether or not the leading slash is present:

| The user sends | You run | Then |
|---|---|---|
| `/mode` | `nicks-stack-route show` | Report mode, provider, model, why |
| `/mode fast` | `nicks-stack-route set fast` | Confirm the new route in one line |
| `/mode smart` | `nicks-stack-route set smart` | Confirm |
| `/mode deep` | `nicks-stack-route set deep` | Confirm, and note deep needs per-task confirmation |
| `/mode build` | `nicks-stack-route set build` | Confirm, and note nothing runs without approval |
| `/mode local` | `nicks-stack-route set local` | Report that it is not installed yet |
| `/modes` or "what modes are there" | `nicks-stack-route modes` | Show the table |
| "why are you using that model" | `nicks-stack-route explain <mode>` | Quote the reason |

Natural phrasings map to the same commands: "use the cheap model" → `set fast`,
"think hard about this" → propose `deep` and ask before switching.

## The five modes

| Mode | Route | Use for |
|---|---|---|
| `fast` | `anthropic` / `claude-haiku-4-5` | Routine email, calendar, summaries, classification, formatting |
| `smart` | `anthropic` / `claude-sonnet-5` — **the gateway's own model** | Default. Business reasoning, Notion, Gmail, Calendar, Drive, CRM |
| `deep` | `anthropic` / `claude-opus-5` | Executive strategy, acquisitions, complex insurance/tax/legal/financial work |
| `build` | Claude Code, else Codex — local CLIs | Software engineering and repository changes |
| `local` | Ollama | Not installed yet. Fails cleanly, never falls back to a paid route |

`smart` is what the gateway already runs, so answering in smart mode costs
nothing extra — no subprocess, no added latency. The other model-backed modes
are one-shot subprocesses:

```bash
nicks-stack-route run fast -q "Summarise this thread in three bullets: ..."
```

Selecting a mode with `set` records a preference; it does not change the model
you answer with. To actually use `fast` or `deep` for a piece of work, run it
through `nicks-stack-route run`.

## Rules that are not negotiable

**Deep is never automatic.** `run deep` exits 3 without `--confirm`. Ask the
user first — name the model and say why it is worth the premium — then re-run
with `--confirm`. Never add `--confirm` on the user's behalf.

**Build never runs unapproved.** `nicks-stack-route run build` reports which
specialist is authenticated and prints the command; it does not execute it. Get
explicit approval, then run the command yourself and follow the
`coding-agent-routing` skill (Hermes stays the conductor; review the diff
afterwards). Claude Code and Codex are locally authenticated CLIs — never try to
drive a consumer subscription through an API key, and never claim a specialist
ran if its auth check failed.

**Local fails clean.** If Ollama is not installed, say so and stop. Do not
silently spend money on a paid route the user did not choose.

**Fallback goes one hop, downward only.** A failed `fast` retries once on
`smart`. Nothing ever escalates into `deep` automatically — the CLI refuses.
When a fallback happens, say so: "fast was overloaded, answered on smart".

**Never print secrets.** The CLI reports key *presence*, never values. Do not
echo `.env`, `.op.env`, or `op item get --format=json` into chat.

## Reporting a route

When asked what you are running, answer with all four facts:

```
mode      : fast
provider  : anthropic
model     : claude-haiku-4-5
why       : least expensive model that still supports the tool loop
```

`nicks-stack-route show` prints exactly that. `--json` is available if you need
to parse it.

## Checking availability

```bash
nicks-stack-route preflight        # can each mode run right now?
nicks-stack-route probe --mode fast   # 1-token live call (costs a few tokens)
```

`probe` is the only command that spends money. Use it after a deployment or when
a route misbehaves — not routinely.

## What this layer does not do

- It does not change `config.yaml` or restart the gateway. Changing the
  *baseline* model is a repo edit to `files/config.yaml` plus a redeploy.
- It does not route automatically by inspecting the task. v1 is explicit on
  purpose: an opaque router that silently picks Opus is a billing surprise.
- It does not persist a mode per conversation. Hermes has no supported
  per-conversation model state, so the selected mode is per-agent and applies
  until it is changed again.
- OpenRouter and Gemini are not wired to any mode. See the `alternates` block in
  `routing.yaml` for the exact verification commands that would promote them.
