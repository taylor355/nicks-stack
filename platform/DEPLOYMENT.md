# Taylor AI Platform — Deployment Guide

**Platform v1.1.1 — frozen.** From here the work is agent identity and company
builds; infrastructure changes should be bug fixes only.

Portable deployment onto an **existing** Ubuntu machine — an Orgo Hermes
computer, a VPS, or bare metal. No Orgo Scale plan, no template publishing, no
golden image.

## What the platform is

| Layer | What it is |
|---|---|
| **Tooling** | `bootstrap.sh` (install), `update.sh` (update + rollback point), `verify.sh` (read-only health), `jack` (one health command) |
| **Provider layer** | Explicit routing across Anthropic, OpenRouter, Gemini and local Ollama. Five modes, no automatic escalation, every route inspectable |
| **Services** | Supervisor-managed: `hermes-gateway`, `agentphone-bridge`, `ollama` (optional). Supervisor is this platform's init for services — not systemd |
| **Secret plane** | 1Password service account resolves every key at agent start. No secret is ever baked into the repo |
| **Integrations** | Telegram, Composio, AgentMail, AgentPhone, Latitude, Orgo, Obsidian, Claude Code, Codex |
| **Source of truth** | `platform.yaml` (declared: version, services, identity, companies) + `platform-manifest.json` (detected: what this machine actually has) |

### v1.1.1 — Composio hardening (fail closed)

Architecture unchanged — this is the hardening pass on v1.1.0.

| | Before | After |
|---|---|---|
| Init fails after a good run | `mcp.env` survived; gateway sourced a **dead URL** and burned the 60s connect timeout | Every failure path calls `teardown()`: `mcp.env` deleted **and** the config entry spliced out |
| Never initialised | `url: ${COMPOSIO_MCP_URL}` expanded to **empty** | The composio entry is **absent from `config.yaml` entirely** — it exists only when a validated session does |
| Toolkit resolution fails | `toolkits=None` → **unscoped session, every toolkit** | Refuses to create a session at all. Fail closed |
| Gateway message | "starting without Composio tools" (untrue — it had a stale URL) | "Composio UNAVAILABLE … entry removed … no stale endpoint will be contacted" |
| Strictness | opt-in `--strict`, never passed | **strict is the default**; `--allow-partial` to opt out |

**How the empty URL became impossible.** `config.yaml` now ships with sentinels
and nothing between them:

```yaml
  # >>> composio (runtime-managed) >>>
  # <<< composio <<<
```

`composio_session.py` splices the entry in only after the session validates,
and splices it out on every failure. It is a **line-based splice, not a YAML
round-trip** — `yaml.safe_dump` would strip every comment in that heavily
commented file. `activate()` and `teardown()` are a matched pair: both files
change together or neither does.

**Metadata** in `~/.hermes/composio/session.json`: `session_id`, `user_id`,
`created_at`, `last_verified`, `toolkits`, `header_type`, `transport`.
`created_at` survives a resume; `last_verified` updates on every success. No
secrets.

**Diagnostics:**

```bash
sudo jack composio            # or: jack composio status
sudo jack composio status --json
sudo jack composio status --offline   # skip the live validity call
sudo jack composio resolve
```

```
Composio
────────────────────
Project API Key      ✓
Session              ✓ valid
Session ID           sess_…
User                 taylor
MCP URL              ✓
Header               x-api-key
Runtime config       ✓ active in config.yaml
Toolkits
  ✓ gmail
  ✓ googlecalendar
  ✓ googledrive
  ✓ googlecontacts
  ✓ notion

Last verified
2026-08-03 04:57:33 UTC
```

**verify.sh** gained the five requested checks: missing Project API key ·
session invalid · empty MCP URL / runtime incoherence · stale `mcp.env` ·
toolkit scope mismatch. A **clean absence** (no URL and no config entry) is a
note, not a failure — that is the designed state when Composio is down. One
without the other is a **critical** incoherence.

#### Acceptance tests (all passing)

| Test | Proves | Result |
|---|---|---|
| Healthy `init` | entry injected, `mcp.env` written | config entry = 1, `mcp.env` present |
| Composio dies after a good run | **stale URLs cannot survive** | `mcp.env` absent, config entry = 0, exit 1 |
| Never initialised | **empty URLs are impossible** | `composio` not in `mcp_servers`; no server has an empty url |
| Toolkit resolution fails | **scope never widens** | no session created, `toolkits=None` never sent, no `session.json` |
| Stored session invalidated | **auto-recreates, identity preserved** | new session, `user_id: taylor`, 5 toolkits |
| Composio unavailable | **gateway still starts cleanly** | wrapper takes the else branch, sources nothing |
| Secret leakage | none | 0 occurrences of URL or key in `status` and `status --json` |

Reproduce on the VM:

```bash
sudo jack composio status                       # baseline: all ✓
sudo mv /root/.hermes/.env /root/.hermes/.env.bak   # simulate a lost key
sudo nicks-stack-composio-session init; echo "exit=$?"   # must be 1
sudo grep -c '^  composio:' /root/.hermes/config.yaml    # must be 0
ls /root/.hermes/composio/mcp.env 2>&1                   # must be "No such file"
sudo mv /root/.hermes/.env.bak /root/.hermes/.env
sudo nicks-stack-composio-session init          # recovers
sudo supervisorctl restart hermes-gateway
sudo jack composio status                       # all ✓ again
```

### v1.1.0 — Composio Sessions replaces the legacy static MCP endpoint

**What was wrong.** The config used the legacy static wiring —
`connect.composio.dev/mcp` + `x-consumer-api-key: ${COMPOSIO_CONSUMER_KEY}`.
That endpoint answered 401 and enumerated zero toolkits. The current model is
the **Sessions API**.

**The audit (installed SDK, not documentation).** `composio 0.18.1` was
installed and introspected; every method below came from the package, not a
doc page:

| What | Verified surface |
|---|---|
| Entry point | `composio.sessions` → `ToolRouter`. `composio.tool_router` is a **deprecated alias** since 0.17.0 |
| Create | `sessions.create(*, user_id, toolkits=[...], mcp=True)` → `ToolRouterSessionWithMcp` |
| Resume | `sessions.use(session_id, *, mcp=True)` |
| Durable id | `session.session_id` (**not** `.id`) |
| Endpoint | `session.mcp` → `ToolRouterMCPServerConfig(type, url, headers)`, type ∈ `http`/`sse` |
| Slug resolution | `client.toolkits.list(search=…, limit=…)` |

**Rotation — answered from SDK source, not assumed.** In
`ToolRouter._create_mcp_server_config`:

```python
headers={"x-api-key": self._client.api_key}
```

`session.mcp.headers` is built **client-side from `COMPOSIO_API_KEY`**. It is
not a server-issued bearer token and **does not rotate**. Only `session.mcp.url`
comes from the server. So the design re-fetches the URL on **every gateway
start** by resuming the persisted session — URL expiry can never strand the
agent, and no refresh daemon is needed.

**How the URL reaches Hermes.** `hermes-gateway-run.sh` already sources
`/root/.env` and `~/.hermes/.env` before exec'ing the gateway. It now also runs
`nicks-stack-composio-session init` and sources one derived file. `config.yaml`
became:

```yaml
composio:
  url: ${COMPOSIO_MCP_URL}          # from ~/.hermes/composio/mcp.env, re-minted each start
  headers:
    x-api-key: ${COMPOSIO_API_KEY}  # exactly what session.mcp.headers contains
```

Env expansion inside a `url:` was already proven by the `ideabrowser` server.

**Toolkit slugs are resolved, never invented.** `platform.yaml` declares
*search terms*; `client.toolkits.list(search=…)` resolves each to a real slug at
session creation, and an unresolvable one is a loud failure, not a silent wrong
slug.

| File | Role |
|---|---|
| `~/.hermes/composio/session.json` | the durable session id — resumed, never re-created |
| `~/.hermes/composio/mcp.env` | derived, 0600, regenerated every gateway start |

Both live in a **derived** tree: `update.sh` excludes `composio/` from the
preserved-data proof (`DERIVED_TREES=(profiles composio)`), so a re-minted URL
never trips "preserved data regressed", and rollback behaviour is unchanged.

**Retired:** `COMPOSIO_CONSUMER_KEY`, `x-consumer-api-key`, and the hardcoded
`connect.composio.dev/mcp`. `verify.sh` **fails** if any of them reappear.
**AgentMail is untouched** — still installed, still reserved.

#### Diagnostics

```bash
sudo nicks-stack-composio-session status         # six facts, presence only
sudo nicks-stack-composio-session status --json
sudo nicks-stack-composio-session resolve        # capability -> real slug
sudo bash platform/verify.sh                     # section 8b
```

Reports, separately: `COMPOSIO_API_KEY` available · SDK importable · session id
persisted · session created/resumed · MCP url obtained · MCP headers obtained ·
toolkits scoped. **No key, URL, header or token value is ever printed.**

#### Acceptance tests

Mechanical first:

```bash
sudo nicks-stack-composio-session status     # all six must read ok
sudo bash platform/verify.sh                 # section 8b green
sudo jack doctor | sed -n '/^Identity/,/^Companies/p'
sudo grep -c 'connect.composio.dev\|x-consumer-api-key' /root/.hermes/config.yaml   # must be 0
```

Then behavioural — ask Jack, then check *how*:

| # | Ask | Pass | Fail |
|---|---|---|---|
| 1 | "What emails have I missed?" | Composio Gmail tools | AgentMail, or any Google-native path |
| 2 | "What's on my calendar tomorrow?" | Composio calendar toolkit | anything else |
| 3 | "Find the Q3 deck in my Drive" | Composio drive toolkit | anything else |
| 4 | "What's Mark's email address?" | Composio contacts toolkit | anything else |
| 5 | "Add a row to my Notion CRM" | Composio notion toolkit | anything else |
| 6 | "Catch me up on email" (fresh session) | **never** touches AgentMail | any AgentMail call |
| 7 | "Check my AgentMail inbox" | AgentMail **is** used — explicit request | refusing to comply |
| 8 | Unset `COMPOSIO_API_KEY`, ask for calendar | says unavailable, names the missing key | **any** Google-native fallback |

Test 8 is the one that catches a silent regression — run it.

#### Restart resumes, never re-creates

```bash
sudo grep -o '"session_id": "[^"]*"' /root/.hermes/composio/session.json
sudo supervisorctl restart hermes-gateway && sleep 20
sudo grep -o '"session_id": "[^"]*"' /root/.hermes/composio/session.json   # SAME id
```

### v1.0.6 — capability migration: Composio-backed Google and Notion

**Decision.** Jack never uses a Hermes-native Google integration. Gmail, Google
Calendar, Google Drive, Google Contacts and Notion all go through **Composio**.
AgentMail stays installed but is **reserved** for the autonomous business agents
(Outlaw, Anderson Tax, …) and is off by default for Jack.

**Audit result — read this first.** There was no Hermes-native Google
implementation to remove. The repository has **no** `gws` or `google-workspace`
CLI, **no** Google MCP server, **no** Google plugin in `plugins.enabled`, and no
`GOOGLE_*` key in the env contract. The only Google-adjacent entry is
`providers.gemini`, which is a *model provider* and out of scope. The three
`gws` / "Google Workspace" hits are prose in research and troubleshooting notes,
two of which already say not to use it.

What did exist were **assumptions**, and those are what changed:

| Was | Now |
|---|---|
| `SOUL.md`: "Your email inbox is `AGENTMAIL_INBOX` … via the agentmail MCP" | Capability-routing section: Google + Notion via Composio; AgentMail reserved |
| Email skill: "you MUST check BOTH mail sources … AgentMail alone is NEVER sufficient" | Composio Gmail is the source; AgentMail only on explicit instruction |
| Nothing declared which implementation backs which capability | `platform.yaml` → `capabilities` |
| Nothing prevented a Google-native path being added later | `platform.yaml` → `forbidden_implementations`, enforced by verify.sh |

#### Capability matrix

| Capability | Implementation | Reached via | Forbidden |
|---|---|---|---|
| **Gmail** | Composio | toolkit `gmail` — `GMAIL_LIST_THREADS`, `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_THREAD_ID` | Hermes-native Google, `google-workspace`/`gws`, himalaya |
| **Google Calendar** | Composio | `COMPOSIO_SEARCH_TOOLS` (slug resolved live) | Hermes-native Google, `google-workspace`/`gws` |
| **Google Drive** | Composio | `COMPOSIO_SEARCH_TOOLS` (slug resolved live) | Hermes-native Google, `google-workspace`/`gws` |
| **Google Contacts** | Composio | `COMPOSIO_SEARCH_TOOLS` (slug resolved live) | Hermes-native Google, `google-workspace`/`gws` |
| **Notion** | Composio | `COMPOSIO_SEARCH_TOOLS` (slug resolved live) | Hermes-native Notion |
| **AgentMail** | `mcp_servers.agentmail` — installed, kept | MCP `list_threads` etc. | **Reserved** — Jack must not use it unless explicitly instructed |

Only Composio identifiers actually confirmed in this repository are named
(`gmail`, `agent_mail`, the `GMAIL_*` / `AGENT_MAIL_*` tools). Calendar, Drive,
Contacts and Notion resolve their toolkit at run time with
`COMPOSIO_SEARCH_TOOLS` rather than hardcoding a slug this repo has never
verified.

#### Where it is enforced

1. `files/platform.yaml` → `capabilities` + `forbidden_implementations` — the declaration.
2. `files/SOUL.md` → capability-routing section — carries the rule into Jack's prompt.
3. `files/skills/email/agentmail-setup/SKILL.md` — Composio Gmail is the source; AgentMail gated.
4. `platform/verify.sh` section 8b — **fails** if a forbidden MCP server, plugin or binary appears.
5. `jack doctor` → Identity section — renders the matrix and flags violations.

```bash
sudo jack doctor                 # Identity section shows the matrix
sudo bash platform/verify.sh     # section 8b enforces it
python3 /root/.hermes/scripts/platform/lib.py capabilities   # machine-readable
```

Nothing about routing, providers, profiles or the runtime changed.

### v1.0.5 — stop starting Hermes to read configuration

**Root cause.** On this Hermes, `hermes config get <key>` is not a file read.
The CLI completes its full startup — resolve every `op://` reference, load
plugins, connect MCP servers, start adapters — and *then* prints the value.
This is **upstream Hermes CLI behaviour, not an invocation bug, not an
environment issue, and not something to work around.** The correct response is
to stop calling it.

**Where the platform was calling it.** Every Hermes invocation in the platform,
split by whether it needs a live runtime:

| Call site | Command | Group | v1.0.5 |
|---|---|---|---|
| `lib.profile_measure_layout()` | `hermes config get secrets.onepassword.env` | **B — config read** | **removed**; layout is declared in `routing.yaml` and resolved by parsing files |
| `lib.hermes_run()` ← `hermes_probe` | `hermes chat` | A — inference | kept, now process-group reaped |
| `gateway-run.sh` | `hermes gateway run` | A — the service | unchanged |
| `agentphone_bridge.py` | `hermes chat` | A — inference | unchanged |
| `lib.versions_detect()` | `hermes --version` | B — but no file holds a *runtime* version | kept, 15s ceiling |
| `bootstrap.sh`, `verify.sh` | `hermes --version` | B — install smoke test | kept |

The v1.0.4 detector ran that `config get` **once per candidate layout**. A
candidate that did not isolate loaded the **default** config, so a
configuration read booted the entire default runtime: 21 secret resolutions
plus MCP OAuth against Composio, Agent Cards, Linear, X, Vidiq and Latitude,
and a Telegram adapter start. That is the log the VM was showing.

**The answer, in the words you asked for: replace config reads with direct YAML
parsing.** `profiles.selection` in `routing.yaml` now states the layout, and
`profile_detect_layout()` resolves it by checking that the config file the
layout points at exists. The default, `hermes-home`, is not a guess — it is
what the VM reported (`jack profiles` → "isolated via hermes-home") before the
call was removed. **Zero Hermes processes are started by `jack profiles`,
`jack doctor`, `verify.sh` or `nicks-stack-provider-doctor` on their default
paths.**

**Why the doctor still hung, and why warnings outlived it.** Two separate
causes, both in our code:

1. `subprocess.run(timeout=…)` kills only the **direct child**. Hermes spawns
   its MCP servers as its own children, so killing `hermes` left them running,
   reparented to init and still authenticating — which is why MCP warnings kept
   appearing after the provider doctor had exited. `hermes_run()` now uses
   `start_new_session=True` and signals the whole process group (TERM, then
   KILL), so nothing survives the call.
2. The 120s per-check ceiling exists for a **cold `hermes chat`**. The default
   validation path has been a capped HTTP call since v1.0.4, where 120s per
   check plus `guarded()`'s +5s join across four providers is ~8 minutes of
   apparent hang. When no Hermes leg is in play the ceiling is now 20s
   (`API_TIMEOUT`); an explicit `--timeout` always wins.

Nothing about routing, providers or profile derivation changed.

```bash
sudo jack profiles                    # no hermes process is started
sudo nicks-stack-provider-doctor      # no hermes process is started
sudo nicks-stack-provider-doctor --via hermes   # the one path that does start it
```

If a future Hermes changes how it selects a profile, change
`profiles.selection` in `routing.yaml` — that is the single knob, and it is
read from YAML.

### v1.0.4 — the profile now actually isolates, and probes are capped

**Bug 1 — the v1.0.3 profile was built correctly and then ignored.** A direct
`hermes chat` still resolved the whole default map (AgentMail, AgentPhone,
GitHub, Firecrawl, Telegram, XAI…), not the profile's three. Cause was in
v1.0.3's own code: `hermes_child_env()` set **both** `HERMES_HOME` (repointed
into the profile) **and** `HERMES_PROFILE=validation`. If Hermes resolves a
named profile as `$HERMES_HOME/profiles/$HERMES_PROFILE/`, that combination
asks for `profiles/validation/profiles/validation/` — which does not exist — so
it falls back to the default config. The profile was never in play.

The fix is not a different guess. Exactly **one** selection layout is applied,
and only one that has been **measured to work on this machine**:

| Layout | What it sets | Grounded in |
|---|---|---|
| `profile-var` | `HERMES_PROFILE=<name>`, `HERMES_HOME` left alone | named profiles live at `~/.hermes/profiles/<name>/` (`hermes-gateway-onboarding/SKILL.md`); `HERMES_PROFILE` (`latitude-telemetry-hermes/…/config.py`) |
| `hermes-home` | `HERMES_HOME=<profile dir>`, no profile name | `HERMES_HOME` is the config root (`gateway-run.sh`, `bootstrap.sh`, `orgo_desktop/client.py`) |
| `home-root` | `HOME=<profile dir>` + `HERMES_HOME=<profile dir>/.hermes` | for a resolver that derives the config dir from `$HOME` |

Each candidate is applied to a real `hermes config get secrets.onepassword.env`
— **the exact thing being isolated** — and wins only if Hermes then reports the
profile's trimmed map instead of the default one. No model call, no secret
value: the output is `op://` reference paths. The winner is cached beside the
derived config and re-measured whenever the profile is rebuilt.

If **no** layout isolates, that is reported as a fact and probes run on the
full runtime. A profile is never allowed to silently pretend:

```bash
sudo jack profiles          # "isolated via profile-var" / "NOT ISOLATED - hermes ignores it"
sudo nicks-stack-provider-doctor --runtime   # the measurement and every attempt
```

`verify.sh` section 8 raises an advisory when the profile is built but not
honoured — the exact silent regression v1.0.3 shipped.

**Bug 2 — validation asked for the model's full completion budget.** OpenRouter
prices a request by its **maximum possible cost**, so a probe advertising up to
128000 output tokens returns **HTTP 402** on an account that could easily afford
the four tokens the probe actually uses.

Every validation probe is now capped, through one shared implementation
(`lib.api_probe()`) used by both `nicks-stack-provider-doctor` and
`jack doctor --providers`, so a call cannot be capped in one tool and uncapped
in the other:

| Vendor | Cap field |
|---|---|
| Anthropic | `max_tokens` |
| OpenRouter / OpenAI-compatible | `max_tokens` |
| Gemini | `generationConfig.maxOutputTokens` |
| Ollama | `options.num_predict` |

The default is `validation.max_tokens: 16` in `routing.yaml` (clamped 8–64).
**There is no verified way to cap `hermes chat`** — no `--max-tokens` flag is
documented for it, and `providers.<name>.max_output_tokens` is recorded in-repo
as an unrecognised provider key — so the Hermes runtime leg is now **opt-in**:

```bash
sudo nicks-stack-provider-doctor                 # capped vendor call (default)
sudo nicks-stack-provider-doctor --via hermes    # exercise the Hermes runtime (uncapped)
sudo nicks-stack-provider-doctor --via both      # capped call, then the runtime leg
sudo nicks-stack-provider-doctor --max-tokens 8
sudo jack doctor --providers                     # capped
sudo jack doctor --providers --via both
```

When both legs run they are reported separately, so a runtime problem is never
mistaken for a credential problem. HTTP 402 now classifies as `quota` with the
cause spelled out rather than as a generic provider error.

**This applies only to validation and probe commands.** `nicks-stack-route run`
is real work and is never capped.

### v1.0.3 — runtime profiles

v1.0.2 established that a one-shot and the gateway must start the same way. The
remaining cost was *what* that startup loads. A validation probe needs a model,
a credential and nothing else, but it was building the gateway's entire runtime
to say `ok`:

| | Full runtime | `validation` profile |
|---|---|---|
| MCP servers | 13 declared (12 enabled, slowest `connect_timeout: 300`) | **0** |
| Plugins | 16 (browser, web search, Spotify, Orgo desktop, telemetry, providers) | **5** — provider plugins only |
| `op://` references resolved at start | 21 | **3** — only the keys routing names |
| Memory / sessions / streaming | on | off |
| Config size | ~11.6 KB | ~1.8 KB |

**A profile is not a new code path.** Hermes' own layout for a named profile is
`~/.hermes/profiles/<name>/`, with the default profile living directly in
`~/.hermes/` (`skills/autonomous-ai-agents/hermes-gateway-onboarding/SKILL.md`),
and `HERMES_PROFILE` is the profile-name variable
(`local-packages/latitude-telemetry-hermes/.../config.py`). `HERMES_HOME` is the
config root — exported by `gateway-run.sh`, set by `bootstrap.sh`, read by
`scripts/orgo_desktop/client.py`. So a profile is the **same `hermes` startup
pointed at a smaller config tree**. Nothing patches or wraps Hermes.

The derived config is **generated from the machine's real `config.yaml`**, never
hand-written, and rebuilt whenever that file changes (a sha256 stamp sits beside
it). It cannot drift from the runtime it is a subset of. Which plugins and
credentials survive is *computed* from `routing.yaml`, not hardcoded: every
plugin ending in `-provider` plus any plugin named like a provider the routing
map addresses, and every `key_env` the map names.

Who runs where — declared in `routing.yaml` under `profiles.use`:

| Work | Profile | Why |
|---|---|---|
| `nicks-stack-provider-doctor`, `jack doctor --providers`, `nicks-stack-route probe` | `validation` | unattended, calls no tools |
| `nicks-stack-route run` | `gateway` (full) | real work needs real tools |
| the supervised `hermes-gateway` service | `gateway` (full) | never derived, never touched |

**A profile can make validation faster; it can never make it fail.** If it
cannot be built, if `dir:` would resolve outside `HERMES_HOME`, or if a probe
fails inside it for a startup/config reason, the call runs on the full runtime
and says the profile was bypassed. A genuine credential or provider failure is
*not* retried — that would just be a slower way to get the same answer.

Inspect and rebuild:

```bash
sudo jack profiles              # what each profile loads, and the delta
sudo jack profiles --json
sudo jack profiles --rebuild    # force a regeneration now
sudo nicks-stack-provider-doctor --runtime   # parity + profiles, spends nothing
```

`bootstrap.sh` rebuilds derived profiles on every run, so a fresh deploy is
already lean. `update.sh` treats `~/.hermes/profiles/` as **regenerated**, not
preserved — it is derived state, so it is excluded from the preserved-data
integrity proof (otherwise any update that legitimately changes `config.yaml`
would report a false "preserved data regressed").

To disable profiles entirely, point `profiles.use.validation` at `gateway` in
`routing.yaml` and redeploy — everything reverts to v1.0.2 behaviour.

### v1.0.2 — Hermes runtime parity (one bug, no redesign)

Symptom: Anthropic, OpenRouter and Gemini credentials all validated, all three
catalogs were reachable, Gemini's direct API answered and Ollama served
locally — yet **every** failure was a `hermes chat` timeout, while the
supervised gateway kept working normally.

Cause: the gateway and the one-shot were not running the same runtime.
`hermes-gateway-run.sh` exports `HOME`/`HERMES_HOME`/`PATH`, sources
`/root/.env` and `~/.hermes/.env` with `set -a` ("it does NOT auto-export
either file"), and launches with `--accept-hooks`. The AgentPhone bridge — the
production one-shot caller — does the same thing in Python (`env.update(...)`
from `~/.hermes/.env`, `HERMES_YOLO_MODE=1`, `--max-turns`, a 900s ceiling).
The provider doctor, the routing CLI and `jack doctor` did none of it: they
called `subprocess.run(["hermes", "chat", ...])` with whatever environment they
happened to inherit and no launch flags. Three consequences, any one of which
is enough to hang a probe:

| Delta | Gateway / bridge | Doctor + router before v1.0.2 | Consequence |
|---|---|---|---|
| Environment | both `.env` files bridged in | bare inherited env | no provider key in the child; Hermes falls back to resolving all 21 `op://` references at start |
| 1Password token | reachable in the process that resolved the map | absent | `op read` prompts on `/dev/tty` — the documented ~30s-per-reference stall |
| Launch flags | `--accept-hooks` (gateway), `--max-turns` (bridge) | neither | a hook approval prompt blocks on a TTY that isn't there; an unbounded agentic turn runs past any short ceiling |

And the reason the gateway "continues to function": it is a **warm, long-lived
process**. It paid secret resolution and MCP/plugin startup once at boot — and
`config.yaml` declares MCP servers with `connect_timeout` up to 300s. Every
`hermes chat` is a **cold start** that pays all of it again. A 30s ceiling
could not survive that even on a perfectly configured machine.

Fix — one shared helper, no routing or provider changes:

| Change | Where |
|---|---|
| `hermes_child_env()` builds the child environment exactly as `gateway-run.sh` does (`.env` files parsed, never executed; `OP_SERVICE_ACCOUNT_TOKEN` from `.op.env`; `HOME`/`HERMES_HOME`/`PATH`) | `scripts/platform/lib.py` |
| `hermes_chat_cmd()` is now the single place the one-shot invocation is built — `--accept-hooks` always, `--max-turns` where the caller is unattended | `scripts/platform/lib.py` |
| `hermes_run()` closes stdin, so nothing can block on a terminal that isn't there | `scripts/platform/lib.py` |
| `hermes_runtime_diagnose()` names the specific cause instead of a bare "timed out" | `scripts/platform/lib.py` |
| provider doctor, routing CLI and `jack doctor` all spawn hermes through those helpers | `provider-routing/doctor.py`, `provider-routing/route.py`, `platform/doctor.py` |
| per-check ceiling raised 30s → 120s (cold start is real; the bridge allows 900s) | provider doctor, `jack doctor` |
| verify.sh section 8 proves the parity inputs exist, and fails when the 1Password map is enabled with no reachable token | `platform/verify.sh` |

Inspect the parity on a machine without spending anything:

```bash
sudo nicks-stack-provider-doctor --runtime          # human-readable
sudo nicks-stack-provider-doctor --runtime --json   # machine-readable
```

It reports key **names** only — no secret value is read, resolved or printed.

If it says the 1Password map is enabled but no token is reachable, that is the
stall, and the fix is the token, not the timeout:

```bash
sudo sh -c 'umask 077; echo "OP_SERVICE_ACCOUNT_TOKEN=ops_..." > /root/.hermes/.op.env'
sudo supervisorctl restart hermes-gateway
sudo nicks-stack-provider-doctor --runtime
```

### v1.0.1 — validation bug fixes (no behaviour redesign)

Found during real validation on the Orgo VM and fixed:

| # | Bug | Fix |
|---|---|---|
| 1 | `jack doctor` reported `unknown on unknown` for git | repo is discovered, then branch/commit read from it |
| 2 | Hardcoded repo paths | `/opt/nicks-stack`, `/root/nicks-stack`, `$HOME/nicks-stack`, `$NICKS_STACK_REPO`, the manifest's recorded path, or the git root |
| 3 | Manual `ollama serve` fought the supervised service | bootstrap stops it (TERM, then KILL) before Supervisor takes the port |
| 4 | provider-doctor could hang | per-check ceiling + a wall-clock guard; a hung provider no longer stalls the rest (ceiling raised to 120s in v1.0.2) |
| 5 | Native Gemini failure blocked validation | Gemini is `required: false`; reported separately, exit code unaffected, models still reachable via OpenRouter |
| 6 | Stale Anthropic aliases (`claude-haiku-4-5`) | modes declare a **tier** (haiku/sonnet/opus); the id resolves live from `GET /v1/models`, cached 24h, pinned id is the offline fallback |
| 7 | Opaque failures | every failure carries `cause` (credential / network / model-id / hermes-config / provider-error) and a `fix` line |

Refresh the Anthropic catalog on demand:

```bash
sudo nicks-stack-provider-doctor --refresh-models
```

## The one command to know

```bash
sudo jack doctor
```

One report, eight sections: Platform · Services · Providers · Integrations ·
Identity · Companies · Versions · Warnings. It makes no billable call unless you
add `--providers`. Everything it reports comes from
`~/.hermes/scripts/platform/lib.py`, the same detection library `bootstrap.sh`,
`update.sh`, `verify.sh` and the routing CLI use — so no two tools can disagree
about the machine.

```bash
sudo jack doctor              # full report, free
sudo jack doctor --providers  # + one real inference per provider
sudo jack doctor --json       # machine-readable
sudo jack version             # platform + component versions
sudo jack manifest show       # the machine-readable manifest
sudo jack mode show           # current routing mode
sudo jack profiles            # runtime profiles: validation vs gateway
```

## How future company deployments inherit this platform

`Outlaw OS`, `Anderson OS` and `Signl OS` are declared in `platform.yaml` and
each becomes its own deployment of **this same platform** — one machine per
company, no shared state:

1. Provision a VM and clone this repo at the platform version you want.
2. `sudo bash platform/bootstrap.sh` — identical tooling, identical services.
3. Give that company its own 1Password vault and its own service account, then
   repoint the `op://` references (see §5, *Creating an isolated second agent*).
4. Onboard its own Telegram bot — never reuse a token across companies.
5. Layer the company's identity and workflows on top. The platform does not
   change; only what runs on it does.

The platform version in `platform.yaml` is what a future deployment reads to
know exactly what it is running. Bump it only for platform releases.


---

## 0. Managed vs preserved — the rule everything else follows

Every command below obeys one policy. Know it before you run anything.

**Managed** — owned by the repo. Replaced when the repo's copy differs; the
previous version is backed up first.

```
/root/.hermes/config.yaml            /usr/local/bin/hermes-gateway-run.sh
/root/.hermes/SOUL.md                /usr/local/bin/nicks-stack-*
/root/.hermes/plugins/               /usr/local/bin/obsidian-launch
/root/.hermes/skills/                /root/Desktop/*.desktop
/root/.hermes/scripts/               /root/.config/autostart/nicks-stack-*.desktop
/root/.hermes/local-packages/        /etc/supervisor/conf.d/nicks-stack.conf
/root/.hermes_agentphone_bridge/agentphone_bridge.py
/usr/share/backgrounds/wallpaper.jpg
```

**Preserved** — owned by you and the agent. Never overwritten. `update.sh`
fingerprints all of it before an update and fails the run if a single byte moved.

```
/root/.hermes/.env                   secrets + Telegram pairing
/root/.hermes/.op.env                1Password service-account token
/root/.hermes/auth.json              model account / OAuth credentials
/root/.hermes/memories/              agent memories
/root/.hermes/state/                 sessions and runtime state
/root/.hermes/<anything else>        anything the agent wrote
/root/.hermes_agentphone_bridge/env  bridge tuning + AgentPhone identity
/root/Documents/HermesVault/         Obsidian vault: your notes, company data
/root/.config/obsidian/obsidian.json vault registry
```

The two `env` files are the one nuance: bootstrap **appends** default keys that
are missing, and never rewrites, reorders, or removes an existing line.
`update.sh` enforces that key-by-key — a key that vanishes or whose value
changes fails the update.

---

## 1. Fresh Ubuntu / Orgo install

On a machine that has never run the stack.

```bash
# 1. Get the repo onto the machine
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/taylor355/nicks-stack.git ~/nicks-stack
cd ~/nicks-stack
git checkout template-v1

# 2. Install (~10–20 min: Hermes, op, cloudflared, Obsidian, npm helpers)
sudo bash platform/bootstrap.sh

# 3. Confirm the install before onboarding
sudo bash platform/verify.sh
```

`bootstrap.sh` logs every action to `/var/log/nicks-stack-bootstrap.log` and
stops on the first error. It is safe to re-run after fixing a failure — it picks
up where it left off and skips what is already correct.

At this point `verify.sh` will pass its critical checks and raise advisories for
the things onboarding is *for* (no model account, no Telegram pairing, no
1Password token). Finish the setup:

```bash
sudo /usr/local/bin/nicks-stack-onboard.sh
```

The onboarding walks four steps: connect the model account, scan the Telegram QR,
paste a 1Password service-account token (optional but recommended), and print the
next steps for the rest of the integrations. On a desktop session it also opens
by itself, and **Nick's Stack Setup** is on the desktop.

Then re-verify — this time everything should be green:

```bash
sudo bash platform/verify.sh
```

### With 1Password (recommended)

The stack's secret plane resolves keys from 1Password at every `hermes` start.
Set the vault up once, on 1password.com:

1. Vault **`Hermes`** → Secure Note **`Hermes Agent Secrets`**.
2. Add fields named exactly like the env vars: `ANTHROPIC_API_KEY`,
   `COMPOSIO_CONSUMER_KEY`, `AGENTMAIL_API_KEY`, … (the full contract is listed
   at the bottom of `files/hermes.env`).
3. Create a service account with access to that one vault.
4. Paste its token during onboarding step 3/4, or:

```bash
sudo sh -c 'umask 077; echo "OP_SERVICE_ACCOUNT_TOKEN=ops_..." > /root/.hermes/.op.env'
sudo /usr/local/bin/nicks-stack-op-enable
sudo supervisorctl restart hermes-gateway
```

The map ships **disabled** on purpose: a token-less `op` prompts on `/dev/tty`
and stalls every interactive `hermes` start. `nicks-stack-op-enable` flips it on
once a token exists, and is idempotent.

> **The default model provider needs a key.** `config.yaml` runs
> `claude-opus-5` on provider `anthropic`, whose `ANTHROPIC_API_KEY` comes from
> `op://Hermes/Hermes Agent Secrets/ANTHROPIC_API_KEY`. Without either that
> field or an `ANTHROPIC_API_KEY=` line in `/root/.hermes/.env`, `verify.sh`
> fails critically — the agent has no model to think with.

---

## 2. Existing-machine update

Pull the repo, then let `update.sh` do the rest. It takes a rollback point
*before* anything changes and refuses to report success if preserved data moved.

```bash
cd ~/nicks-stack
git fetch origin template-v1
git checkout template-v1
git pull origin template-v1

# Dry run first — changes nothing, tells you what it would touch
sudo bash platform/update.sh --check-only

# Apply
sudo bash platform/update.sh
```

`update.sh` runs six steps: fingerprint preserved data → take the rollback point
→ re-run `bootstrap.sh` → re-enable the 1Password map and restart the services →
prove the preserved data is byte-identical → run `verify.sh`. It exits non-zero
if any of that fails, and prints the rollback point either way.

Useful variants:

```bash
sudo bash platform/update.sh --no-restart      # apply now, restart during a window
sudo bash platform/update.sh --no-verify       # skip the verify.sh run
sudo bash platform/update.sh --op-write-test   # also probe 1Password write access
```

Log: `/var/log/nicks-stack-update.log`.

> **Why the restart is part of the update:** Hermes has no hot reload. A changed
> `config.yaml` only takes effect when the gateway restarts. With `--no-restart`
> you are running the old config until you do:
> `sudo supervisorctl restart hermes-gateway`.

### Config changes only (no repo update)

If you edited `/root/.hermes/config.yaml` by hand on the machine, do **not** run
`update.sh` — it re-places `config.yaml` from the repo and your edit is reverted
(the old copy lands in the rollback point). Either make the change in the repo
and update, or restart with your local edit in place:

```bash
sudo supervisorctl restart hermes-gateway
sudo bash platform/verify.sh
```

---

## 3. Verification

Read-only. Never writes a file, never touches 1Password, never restarts a
service. Safe to run at any time, including while the agent is working.

```bash
sudo bash platform/verify.sh            # full report
sudo bash platform/verify.sh --quiet    # failures + advisories + summary only
echo $?                                 # 0 = healthy, 1 = critical failure, 2 = could not run
```

It checks nine groups: platform, binaries, Hermes files and permissions, config
integrity, model credentials, the 1Password secret plane, services, onboarding
state, and a user-data inventory.

**Critical** (exit 1) means the stack is broken: a missing binary, an unparseable
`config.yaml`, a `0600` file that is no longer `0600`, an empty skills tree, a
supervisor program that is not registered, or no reachable source for the model
provider's API key.

**Advisory** (exit 0) means something is not set up yet but nothing is broken:
Telegram not paired, 1Password not connected, AgentPhone dormant, a missing
desktop entry.

Use it in a cron or CI check:

```bash
sudo bash platform/verify.sh --quiet || echo "nicks-stack UNHEALTHY on $(hostname)"
```

### 1Password write-permission test

`verify.sh` tests **read** access only — testing write would break its read-only
guarantee. The write test is opt-in and lives in `update.sh`:

```bash
sudo bash platform/update.sh --op-write-test --check-only
```

`--check-only` makes this a pure probe: the deployment is not modified, no
rollback point is created, and the only write that happens anywhere is the
temporary 1Password field.

What it does, exactly:

1. Reads the service-account token from `/root/.hermes/.op.env` (never printed).
2. Adds **one new field** to `op://Hermes/Hermes Agent Secrets`, named
   `nicks_stack_write_probe_<pid>_<epoch>` — unique per run, so it can never
   collide with a real field or a concurrent run. Its value is the literal
   string `ok`, not a secret.
3. Reads that field back and compares it to `ok`.
4. Deletes the field, then confirms it is gone.

No existing field is read, written, or printed. If the probe fails mid-way, a
cleanup trap deletes the field anyway; if even that fails, the script tells you
the exact field name to remove by hand.

Doing it manually is four commands:

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(sudo sed -n 's/^OP_SERVICE_ACCOUNT_TOKEN=//p' /root/.hermes/.op.env)"
FIELD="nicks_stack_write_probe_$$_$(date +%s)"

op item edit "Hermes Agent Secrets" --vault Hermes "${FIELD}[text]=ok"   # write
op read "op://Hermes/Hermes Agent Secrets/${FIELD}"                      # expect: ok
op item edit "Hermes Agent Secrets" --vault Hermes "${FIELD}[delete]="   # clean up

unset OP_SERVICE_ACCOUNT_TOKEN
```

**Why write access matters:** the agent installs its own keys. Onboarding writes
the Telegram token, the AgentMail skill saves the inbox address it creates, and
"here's my API key" in chat ends with the agent storing it. A read-only service
account still runs the stack — it just cannot persist anything it obtains, so
those keys live only in `/root/.hermes/.env` and are lost when the machine is
rebuilt. Grant write access if you want the vault to be the durable record.

---

## 4. Rollback

Every `update.sh` run creates a rollback point before touching anything:

```
/opt/nicks-stack/backups/<timestamp>-update/
├── MANIFEST.txt              what this point is, which commit produced it
├── preserved-before.sha256   fingerprint of every preserved file, pre-update
├── preserved-after.sha256    the same, post-update (the proof)
├── envkeys-before_*          env key names + value hashes (no values)
└── root/ usr/ etc/           the managed files as they were, at their real paths
```

`bootstrap.sh` also writes points at `/opt/nicks-stack/backups/<timestamp>/`
(no `-update` suffix) whenever it replaces a managed file during an install.

List what you have:

```bash
ls -1t /opt/nicks-stack/backups/
sudo cat /opt/nicks-stack/backups/<timestamp>-update/MANIFEST.txt
```

### Option A — restore the files (fastest, no repo state involved)

```bash
B=/opt/nicks-stack/backups/<timestamp>-update      # set this first

# Single files
sudo cp -a "$B/root/.hermes/config.yaml" /root/.hermes/config.yaml
sudo cp -a "$B/root/.hermes/SOUL.md"     /root/.hermes/SOUL.md
sudo chmod 600 /root/.hermes/config.yaml

# Whole trees (replace, don't merge)
for t in plugins skills scripts local-packages; do
  sudo rm -rf "/root/.hermes/$t"
  sudo cp -a "$B/root/.hermes/$t" "/root/.hermes/$t"
done

# Launchers and the supervisor conf
sudo cp -a "$B/usr/local/bin/." /usr/local/bin/
sudo cp -a "$B/etc/supervisor/conf.d/nicks-stack.conf" /etc/supervisor/conf.d/ 2>/dev/null || true

# Re-enable 1Password (the restored config.yaml ships the map disabled) and restart
sudo /usr/local/bin/nicks-stack-op-enable
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl restart hermes-gateway agentphone-bridge
sudo bash platform/verify.sh
```

The rollback point also holds copies of `.env`, `.op.env`, `auth.json`, the
bridge `env`, and `obsidian.json`. **Restore those only if they were actually
damaged** — they are preserved files, so a normal update never changes them, and
restoring them discards any key or pairing added since the backup:

```bash
sudo cp -a "$B/root/.hermes/.env" /root/.hermes/.env && sudo chmod 600 /root/.hermes/.env
```

### Option B — roll the repo back and re-deploy (keeps everything consistent)

```bash
cd ~/nicks-stack
git log --oneline -10                    # find the commit you want
git checkout <previous-commit>
sudo bash platform/update.sh             # re-deploys that commit, new rollback point
```

This is the better option when the bad change came from the repo, because the
machine ends up matching a known commit rather than a hand-assembled state.

### What rollback does not undo

Rolling back **never** reverts preserved data — memories, sessions, vault notes,
`.env` keys, and the Telegram pairing stay exactly as they are. That is the
intent: a config rollback must not cost the agent its history. If preserved data
is what got damaged, restore those specific files from the backup as shown above.

---

## 5. Creating an isolated second agent

**Use a second machine.** The stack is single-tenant by construction: fixed paths
(`/root/.hermes`, `/root/Documents/HermesVault`), fixed supervisor program names
(`hermes-gateway`, `agentphone-bridge`), and a fixed bridge port (`8787`).
Running two agents on one box means changing all of that — a redesign, not a
deployment step. Provision a second Orgo computer (or VPS) and bootstrap it.

What must differ for the two agents to be genuinely isolated:

| Boundary | Agent 1 | Agent 2 |
|---|---|---|
| Machine | computer A | computer B |
| 1Password vault + item | `Hermes` / `Hermes Agent Secrets` | e.g. `Hermes-Ops` / `Hermes Agent Secrets` |
| 1Password service account | account A (scoped to vault A) | account B (scoped to vault B) |
| Telegram bot | bot A | bot B (fresh QR — never reuse the token) |
| AgentMail inbox | inbox A | inbox B |
| AgentPhone number + agent id | number A | number B |
| Orgo `ORGO_DEFAULT_COMPUTER_ID` | computer A's id | computer B's id |
| Latitude project | project A | project B |
| Obsidian vault contents | vault A's notes | starts from the repo skeleton |

Never copy `/root/.hermes/.env`, `/root/.hermes/.op.env`, or
`/root/.hermes/auth.json` from the first machine to the second. That is exactly
what breaks isolation: both agents would then share one bot, one inbox, and one
key set, and either could act as the other.

### Steps

```bash
# On machine B — same install as a fresh one
git clone https://github.com/taylor355/nicks-stack.git ~/nicks-stack
cd ~/nicks-stack && git checkout template-v1
sudo bash platform/bootstrap.sh
```

Point agent B at its own 1Password vault by rewriting the `op://` references —
the only per-agent edit the config needs:

```bash
# Replace the vault name in every op:// reference (item name stays the same)
sudo sed -i 's|op://Hermes/|op://Hermes-Ops/|g' /root/.hermes/config.yaml
sudo grep -c 'op://Hermes-Ops/' /root/.hermes/config.yaml   # expect: 20
```

Then give agent B its own token and finish onboarding — a fresh Telegram QR
mints a new bot, so the two agents are unreachable from each other's chats:

```bash
sudo sh -c 'umask 077; echo "OP_SERVICE_ACCOUNT_TOKEN=ops_<agent-B-token>" > /root/.hermes/.op.env'
sudo /usr/local/bin/nicks-stack-op-enable
sudo /usr/local/bin/nicks-stack-onboard.sh
```

Verify agent B against **its** vault — both scripts take the vault and item from
the environment, defaulting to agent 1's:

```bash
sudo env NICKS_STACK_OP_VAULT=Hermes-Ops bash platform/verify.sh
sudo env NICKS_STACK_OP_VAULT=Hermes-Ops bash platform/update.sh --op-write-test --check-only
```

Keep that variable in the update command for agent B from then on, or put it in
`/root/.bashrc` on machine B so it is never forgotten:

```bash
echo 'export NICKS_STACK_OP_VAULT=Hermes-Ops' | sudo tee -a /root/.bashrc
```

> **The `sed` edit is machine-local.** It is not in the repo, so the next
> `update.sh` re-places `config.yaml` from the repo and reverts it (the edited
> copy goes to the rollback point). On machine B, re-run the `sed` after every
> update — or, better, carry a branch whose `files/config.yaml` already points at
> the second vault and deploy machine B from that branch.

### Shared identity, separate machines

If instead you want two machines running the *same* agent identity — a
warm spare, or a migration — that is the opposite problem, and the answer is the
1Password vault: point machine B at the **same** vault, give it the same
`op://` references, and let the secret plane rehydrate the keys. Telegram, however,
is single-holder: one bot token can only be long-polled by one process, so the
spare must stay stopped (`sudo supervisorctl stop hermes-gateway`) until it takes over.

---

## 6. Provider routing (modes)

Five named modes, one declarative map, no automatic switching. The map is
`files/routing.yaml` → `/root/.hermes/routing.yaml`; the CLI is
`nicks-stack-route` (a symlink to
`/root/.hermes/scripts/provider-routing/route.py`).

| Mode | Provider / model | For |
|---|---|---|
| `fast` | `anthropic` / `claude-haiku-4-5` | Routine email, calendar, summaries, classification, formatting |
| `smart` | `anthropic` / `claude-sonnet-5` — **the gateway's own model** | Default. Business reasoning, Notion, Gmail, Calendar, Drive, CRM |
| `deep` | `anthropic` / `claude-opus-5` | Executive strategy, acquisitions, complex insurance/tax/legal/financial work |
| `build` | Claude Code, else Codex (local CLIs) | Repository changes — approval required, never auto-run |
| `local` | Ollama | Not installed. Fails clean; never falls back to a paid route |

`smart` is what the gateway runs, so answering in smart mode costs nothing
extra. The other model-backed modes are one-shot subprocesses using the same
CLI flags the AgentPhone bridge uses in production:

```bash
hermes chat -Q --source nicks-stack-routing -m <model> --provider <provider> -q "<prompt>"
```

Changing modes therefore needs **no gateway restart** and cannot disturb
Telegram, sessions or memory.

### Commands

```bash
nicks-stack-route show                 # mode, provider, model, why
nicks-stack-route modes                # every mode + availability
nicks-stack-route set fast             # select a mode (persisted)
nicks-stack-route explain deep         # why this route, and its fallback
nicks-stack-route run fast -q "..."    # one-shot in a route
nicks-stack-route run deep --confirm -q "..."   # premium needs --confirm
nicks-stack-route preflight            # can each mode run right now?
nicks-stack-route probe --mode fast    # 1-token live call (costs a few tokens)
```

The selected mode is stored in `/root/.hermes/state/nicks-stack-mode.json`,
which `update.sh` treats as preserved data — a deployment never resets it.

### Telegram syntax

Hermes has no custom slash commands (`/new`, `/reset`, `/status`, `/mcp`,
`/reasoning`, `/reload` are built in; a plugin cannot add `/mode`). So `/mode …`
arrives as ordinary message text and the `provider-routing` skill tells Jack to
act on it:

| Send in Telegram | Jack runs |
|---|---|
| `/mode` | `nicks-stack-route show` |
| `/mode fast` \| `smart` \| `deep` \| `build` \| `local` | `nicks-stack-route set <mode>` |
| `/modes` | `nicks-stack-route modes` |
| "which model are you using?" | `nicks-stack-route show` |

### Cost controls

- **deep is never automatic** — `run deep` exits 3 without `--confirm`, and no
  mode may name a confirmation-required mode as its fallback (`verify.sh` fails
  the deployment if one does).
- **build never runs unapproved** — the router reports which specialist is
  authenticated and prints the command; it never executes a coding agent.
- **local fails clean** — exits 4 with "not installed/configured"; no paid
  fallback.
- **fallback is one hop, downward only** — a failed `fast` retries once on
  `smart`, and the retry is always announced.

### OpenRouter and Gemini

Neither is wired to a mode, deliberately:

- **OpenRouter** — the provider is verified (`openrouter` and
  `openrouter-provider` are in `plugins.enabled`, `OPENROUTER_API_KEY` is in the
  1Password map). The *model id* is not: those are `vendor/model` strings that
  change. Verify one, then point a mode at it.
- **Gemini** — `GEMINI_API_KEY` is mapped, but this Hermes ships no
  Gemini/Google provider plugin, so a plugin name, `key_env` and transport would
  all be guesses.

The exact promotion commands live in the `alternates` block of
`files/routing.yaml`. Run them on the machine, then edit `routing.yaml` in the
repo and redeploy — never hand-edit the deployed copy, which `update.sh`
replaces.

### Provider validation (the doctor)

`nicks-stack-provider-doctor` validates every provider end to end and is the
only thing that should be trusted to say a provider works:

```bash
sudo nicks-stack-provider-doctor                  # all providers, real inference
sudo nicks-stack-provider-doctor --provider gemini
sudo nicks-stack-provider-doctor --no-inference   # credential + catalog only, no spend
sudo nicks-stack-provider-doctor --runtime        # gateway-vs-one-shot parity + profiles
sudo nicks-stack-provider-doctor --json
```

Three checks per provider, stopping at the first failure so a later step can
never report a misleading PASS:

1. **credential** — resolved from the environment, `~/.hermes/.env`, or the
   1Password map via `op read`. Values are never printed.
2. **catalog** — the configured model id is checked against the vendor's own
   live model list. A stale pin fails loudly instead of routing somewhere
   unintended.
3. **inference** — one real, minimal completion. Through `hermes chat` when the
   CLI is present, so the actual runtime path is exercised.

Output is a PASS/FAIL block per provider followed by the current mode,
provider, model, available and unavailable providers, and the installed Ollama
models. Exit code is 0 only when every checked provider passed.

| Provider | Mechanism | Credential |
|---|---|---|
| Anthropic | `anthropic` plugin (`plugins.enabled`) | `ANTHROPIC_API_KEY` |
| OpenRouter | `openrouter` plugin (`plugins.enabled`) | `OPENROUTER_API_KEY` |
| Gemini | custom provider → Google's OpenAI-compatible endpoint, `--provider custom:gemini` | `GEMINI_API_KEY` |
| Ollama | custom provider → local daemon, `--provider custom:ollama` | none (local) |

Gemini and Ollama are declared in `files/config.yaml` → `providers:` using the
same key shape as the existing `vercel-ai-gateway` entry (`base_url`,
`key_env`, `transport: chat_completions`). Neither invents a plugin name.

Ollama specifics: the daemon is detected at `$OLLAMA_HOST` (default
`http://127.0.0.1:11434`), installed models come from `/api/tags`, and nothing
about the model set is assumed. If the daemon is down or no model is pulled,
`local` mode exits 4 and never falls back to a paid route.

### Changing the baseline model

The router does not touch `config.yaml`. To change what the *gateway itself*
runs, edit `files/config.yaml` → `model.default` in the repo, keep
`routing.yaml`'s `smart` mode in sync (verify.sh cross-checks them and fails if
they drift), then redeploy and restart.

## 7. Enabling local AI (Ollama) on a company VM

Ollama is a **managed platform service** here, exactly like the Hermes gateway
and the AgentPhone bridge: one `[program:ollama]` block in
`/etc/supervisor/conf.d/nicks-stack.conf`, started by
`/usr/local/bin/nicks-stack-ollama-run.sh`, restarted on boot and on crash.

> **This platform uses Supervisor, not systemd.** `bootstrap.sh` reports what
> PID 1 actually is, and Ollama's upstream installer writes a `systemd` unit
> that would never start here — bootstrap disables that unit if it appears, so
> only the supervised service ever binds port 11434. Never run `ollama serve`
> by hand on a deployed VM: two servers fight over the port and the supervised
> one loses silently. Use `supervisorctl` (below).

### Nothing is downloaded unless you ask

Bootstrap never installs Ollama and never pulls a model by default, and it
assumes no internet. Three separate, explicit opt-ins:

| Flag | What it does | Needs network |
|---|---|---|
| `--with-ollama` | Supervises an Ollama that is **already installed** | no |
| `--install-ollama` | Also installs the Ollama binary | yes |
| `--ollama-models "a b"` | Also pulls those models | yes |

The service wrapper is **dormant-gated**: if the program is configured before
Ollama exists, the service sleeps instead of crash-looping, and starts serving
the moment the binary appears.

### Case A — VM that already has Ollama (this machine)

```bash
cd ~/nicks-stack && git pull origin template-v1
sudo bash platform/bootstrap.sh --with-ollama
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status ollama
```

`bootstrap.sh` auto-detects an installed Ollama, so a plain
`sudo bash platform/update.sh` on this machine also picks it up.

### Case B — fresh company VM, air-gapped or metered

```bash
# 1. Install Ollama however your policy allows (mirror, image, manual .tgz)
# 2. Copy the model blobs to the new VM (no download needed):
#    from a machine that already has them —
sudo tar -C /usr/share/ollama/.ollama -czf ollama-models.tgz models
#    on the new VM —
sudo tar -C /usr/share/ollama/.ollama -xzf ollama-models.tgz

# 3. Supervise it — no network touched:
sudo bash platform/bootstrap.sh --with-ollama
sudo supervisorctl reread && sudo supervisorctl update
```

### Case C — fresh company VM with internet

```bash
sudo bash platform/bootstrap.sh \
  --install-ollama \
  --ollama-models "qwen3:4b nomic-embed-text"
sudo supervisorctl reread && sudo supervisorctl update
sudo bash platform/verify.sh
sudo nicks-stack-provider-doctor --provider ollama
```

### The standard model set

| Model | Role | Pull command |
|---|---|---|
| `qwen3:4b` | Chat / reasoning — what `local` mode routes to | `ollama pull qwen3:4b` |
| `nomic-embed-text` | Embeddings — for retrieval, **not** chat | `ollama pull nomic-embed-text` |

Embedding models cannot answer a prompt. Both the router and the doctor exclude
them from inference automatically, so an Ollama with *only* `nomic-embed-text`
reports "pull a chat model" rather than routing into a model that will fail.

### Supervisor commands

```bash
sudo supervisorctl status                     # all three services
sudo supervisorctl status ollama
sudo supervisorctl start ollama
sudo supervisorctl stop ollama
sudo supervisorctl restart ollama
sudo supervisorctl reread && sudo supervisorctl update   # after a conf change
sudo tail -50 /var/log/orgo/ollama.err.log    # why it will not start
```

### Verification

```bash
sudo bash platform/verify.sh                  # section 10 = Local AI (Ollama)
sudo nicks-stack-provider-doctor --provider ollama
sudo nicks-stack-route run local -q "Summarise: the quarterly renewal is due."
```

`verify.sh` checks: Ollama installed · serving under Supervisor · API reachable
· models installed · `qwen3` present · `nomic-embed-text` present. Those checks
are **critical** on a machine configured for local AI (a supervised `ollama`
program exists) and **advisory** everywhere else, so VMs that do not use local
AI still pass.

### Turning it off

```bash
sudo supervisorctl stop ollama
# then remove the [program:ollama] block from
# /etc/supervisor/conf.d/nicks-stack.conf, or redeploy from a machine
# without Ollama installed and without --with-ollama
sudo supervisorctl reread && sudo supervisorctl update
```

`local` mode then reports unavailable and never falls back to a paid route.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `verify.sh`: "no source for ANTHROPIC_API_KEY" | The 1Password map is disabled, the field is missing, or no `.env` fallback | `sudo /usr/local/bin/nicks-stack-op-enable`, or add the field in 1Password, or add the key to `/root/.hermes/.env` |
| `verify.sh`: "secrets.onepassword.enabled = false" | `config.yaml` was just re-placed by an install or update | `sudo /usr/local/bin/nicks-stack-op-enable && sudo supervisorctl restart hermes-gateway` |
| `verify.sh`: "op whoami fails" | Service-account token expired or revoked | Mint a new one, rewrite `/root/.hermes/.op.env`, restart the gateway |
| `hermes-gateway` not RUNNING | The wrapper waits for `config.yaml` **and** a non-empty `auth.json` — by design, it stays dormant instead of crash-looping | Run `sudo /usr/local/bin/nicks-stack-onboard.sh` (step 1/4) |
| `agentphone-bridge` not RUNNING | Dormant until `AGENTPHONE_API_KEY` and `AGENTPHONE_AGENT_ID` are both in `.env` | Add the keys, then `sudo supervisorctl restart agentphone-bridge` |
| `bootstrap.sh`: "another bootstrap is already running" | A stale lock, or a genuine concurrent run | Check with `ps aux \| grep bootstrap.sh`; if none, remove the lock file named in the error |
| Update reports "preserved data regressed" | Something removed or changed a preserved file during the run | Do not re-run. Read `preserved-after.sha256` vs `preserved-before.sha256` in the rollback point to see exactly which file, then restore it from the backup |
| Model calls fail with an auth error | Key present but wrong or unfunded | `sudo -i hermes -z "Reply with exactly: ok"` and read the error; check the key in 1Password |
| `hermes chat` times out but the gateway keeps working | A cold one-shot is not getting the gateway's runtime: no bridged `.env`, no 1Password token, or a hook prompt with no TTY | `sudo nicks-stack-provider-doctor --runtime` names the cause. Fixed for all platform tools in v1.0.2 — if you still see it from a hand-rolled call, source both env files and pass `--accept-hooks` |
| `--runtime`: "1Password map is ENABLED … but no OP_SERVICE_ACCOUNT_TOKEN is reachable" | `config.yaml` has 21 `op://` references and `op read` has no token, so it prompts on `/dev/tty` once per reference | Write the token to `/root/.hermes/.op.env` (mode 600) and restart the gateway — do not raise the timeout |
| A probe reports "[ran on the full runtime]" or "[full runtime]" | The `validation` profile could not be built or failed at startup, so the call fell back | `sudo jack profiles` shows the reason; `sudo jack profiles --rebuild` regenerates it. Validation still works meanwhile — this is a performance notice, not a failure |
| `jack profiles` shows validation with the same counts as gateway | `profiles.use.validation` points at `gateway`, or `routing.yaml` has no `profiles:` block | Set `profiles.use.validation: validation` in `routing.yaml` and redeploy |
| `jack profiles` shows the profile unusable | `profiles.selection` in `routing.yaml` names an unknown layout, or the config file it points at is missing | `sudo jack profiles --rebuild`; valid values are `profile-var`, `hermes-home`, `home-root` |
| `hermes config get <key>` is slow and starts MCP/Telegram | Upstream Hermes CLI behaviour: the CLI completes full startup before printing a config value | Not a platform bug and not worked around. Read `~/.hermes/config.yaml` (or `jack profiles --json`) instead of asking the CLI |
| `jack doctor`: "FORBIDDEN mcp_servers.X is configured" | A Hermes-native Google/Notion integration was added | Remove it from `config.yaml`; the capability belongs to Composio (`platform.yaml` → `capabilities`) |
| Jack reaches for AgentMail on personal mail | An older `SOUL.md` or email skill is deployed | `sudo bash platform/update.sh`; `SOUL.md` must contain the capability-routing section |
| A Composio-backed capability fails | Composio not connected, or the app not linked in Composio | Check `COMPOSIO_CONSUMER_KEY`, then connect the app in Composio. Do **not** substitute a Google-native path |
| MCP auth warnings continue after a platform command exits | Pre-v1.0.5: killing `hermes` orphaned its MCP server children | Fixed — hermes children run in their own process group and are reaped as a group. If seen on v1.0.5+, the warnings are the long-running gateway's, not a leftover probe |
| Validation still resolves AgentMail/GitHub/Telegram/etc. | The profile is not being honoured, or was built before the fix | `sudo jack profiles --rebuild` then `sudo jack profiles`; the state column must read "isolated via …" |
| OpenRouter returns HTTP 402 during validation | An uncapped request — OpenRouter charges against maximum possible cost | Use the default capped path (`nicks-stack-provider-doctor` with no `--via`). `--via hermes` cannot be capped and can 402 on a low-credit account |
| `verify.sh`: "declares dir=… which is not inside /root/.hermes" | A `profiles.<name>.dir` in `routing.yaml` escapes `HERMES_HOME` | Make it relative and inside `HERMES_HOME`, e.g. `profiles/validation` |
| `/mode` does nothing in Telegram | The `provider-routing` skill is not deployed, or the CLI symlink is missing | `sudo bash platform/verify.sh` → section 7; redeploy with `platform/update.sh` |
| `nicks-stack-route run deep` exits 3 | By design — premium routes need explicit consent | Re-run with `--confirm` after the user agrees |
| `nicks-stack-route run local` exits 4 | By design — Ollama is not installed | Use `fast`/`smart`, or wait for the Ollama sprint |
| verify.sh: "falls back into premium mode" | A `routing.yaml` edit lets a cheap route escalate into `deep` | Point the fallback at `smart` and redeploy |
| `supervisorctl status ollama` shows no such process | The program is not in the conf on this machine | `sudo bash platform/bootstrap.sh --with-ollama` then `supervisorctl reread && supervisorctl update` |
| ollama service stuck in STARTING/BACKOFF | The binary is missing — the wrapper sleeps by design | Install Ollama, or check `/var/log/orgo/ollama.err.log` |
| Port 11434 already in use | A hand-started `ollama serve` is racing the supervised one | `pkill -f "ollama serve"` then `sudo supervisorctl restart ollama` |
| local mode says "only embedding models are installed" | Only `nomic-embed-text` is pulled | `ollama pull qwen3:4b` |
| Obsidian will not launch on the desktop | Sandbox flags | `obsidian-launch` already passes `--no-sandbox --disable-gpu`; check `DISPLAY` is `:99` |

Logs worth reading, in order:

```bash
sudo tail -100 /var/log/nicks-stack-bootstrap.log     # install
sudo tail -100 /var/log/nicks-stack-update.log        # update
sudo tail -100 /var/log/orgo/hermes-gateway.err.log   # the agent itself
sudo tail -100 /root/.hermes_agentphone_bridge/supervisor.err.log
```

---

## Command summary

```bash
# Install
sudo bash platform/bootstrap.sh
sudo /usr/local/bin/nicks-stack-onboard.sh

# Update
git pull origin template-v1
sudo bash platform/update.sh --check-only
sudo bash platform/update.sh

# Verify
sudo bash platform/verify.sh
sudo bash platform/update.sh --op-write-test --check-only

# Rollback
ls -1t /opt/nicks-stack/backups/
# then Option A (restore files) or Option B (git checkout + update.sh) above

# Local AI
sudo bash platform/bootstrap.sh --with-ollama
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status ollama

# Routing
nicks-stack-provider-doctor
nicks-stack-route show
nicks-stack-route set fast
nicks-stack-route run deep --confirm -q "..."

# Second agent (machine B)
sudo bash platform/bootstrap.sh
sudo sed -i 's|op://Hermes/|op://Hermes-Ops/|g' /root/.hermes/config.yaml
sudo env NICKS_STACK_OP_VAULT=Hermes-Ops bash platform/verify.sh
```
