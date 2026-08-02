# Nick's Stack — Deployment Guide

Portable deployment of Nick's Stack onto an **existing** Ubuntu machine — an
Orgo Hermes computer, a VPS, or bare metal. No Orgo Scale plan, no template
publishing, no golden image.

`build_template.py` remains the source of truth for *what* the stack is (files,
destinations, install order, services). `platform/bootstrap.sh` is the source of
truth for *how* it lands on a running machine. The other two scripts wrap it:

| Script | Writes? | Purpose |
|---|---|---|
| `platform/bootstrap.sh` | yes | Install or re-install. Idempotent — installs only what is missing. |
| `platform/update.sh` | yes | Update an existing deployment, with a rollback point and a before/after proof that preserved data survived. |
| `platform/verify.sh` | **no** | Read-only health check. Exits non-zero if a critical check fails. |

Requirements: Ubuntu 24.04 (or Debian-family), `amd64`, root, outbound HTTPS.

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

### Changing the baseline model

The router does not touch `config.yaml`. To change what the *gateway itself*
runs, edit `files/config.yaml` → `model.default` in the repo, keep
`routing.yaml`'s `smart` mode in sync (verify.sh cross-checks them and fails if
they drift), then redeploy and restart.

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
| `/mode` does nothing in Telegram | The `provider-routing` skill is not deployed, or the CLI symlink is missing | `sudo bash platform/verify.sh` → section 7; redeploy with `platform/update.sh` |
| `nicks-stack-route run deep` exits 3 | By design — premium routes need explicit consent | Re-run with `--confirm` after the user agrees |
| `nicks-stack-route run local` exits 4 | By design — Ollama is not installed | Use `fast`/`smart`, or wait for the Ollama sprint |
| verify.sh: "falls back into premium mode" | A `routing.yaml` edit lets a cheap route escalate into `deep` | Point the fallback at `smart` and redeploy |
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

# Routing
nicks-stack-route show
nicks-stack-route set fast
nicks-stack-route run deep --confirm -q "..."

# Second agent (machine B)
sudo bash platform/bootstrap.sh
sudo sed -i 's|op://Hermes/|op://Hermes-Ops/|g' /root/.hermes/config.yaml
sudo env NICKS_STACK_OP_VAULT=Hermes-Ops bash platform/verify.sh
```
