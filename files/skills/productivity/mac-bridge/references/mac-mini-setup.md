# Setting up the Mac mini

**Where you do this:** at the Mac mini, with a keyboard and screen. Not on the
Orgo VM, not on claude.ai, not from your phone. Two of the steps are browser
sign-ins that must happen on the machine that will run the software, and one
step grants a macOS permission that only exists in System Settings.

**How long:** about 25 minutes, most of it waiting on downloads.

**What's already done for you on the VM side:** Tailscale is installed and
supervised, an SSH key has been minted, and the bridge script, config and
skill are deployed. When you finish the Mac steps, one line of config on the
VM turns everything on.

---

## Step 0 — Stop the Mac from sleeping

An always-on Mac that falls asleep is an intermittently-on Mac, and every
lane here fails silently when it naps.

System Settings → Energy Saver → **Prevent automatic sleeping when the
display is off**: on.

Or in Terminal:

```bash
sudo pmset -a sleep 0 disksleep 0
```

---

## Step 1 — Tailscale

This is the private link between the VM and the Mac. It is not a public
server: nothing gets published to the internet and you do not open a port on
your router.

1. Install from <https://tailscale.com/download/mac> (App Store version is
   fine too).
2. Sign in. A personal account is free and covers well past two machines.
3. Note the machine name Tailscale gives it — probably `mac-mini`. That name
   is what the VM will use.

Then generate a key so the VM can join the same network:

1. Go to <https://login.tailscale.com/admin/settings/keys>
2. **Generate auth key** → tick *Reusable* off, *Ephemeral* off, expiry 90
   days is fine.
3. Copy it. **Do not paste it into chat.** Put it in 1Password, in the
   `Hermes` vault, item `Hermes Agent Secrets`, as a new field named
   `TAILSCALE_AUTHKEY`.

Jack can read that vault and will bring the VM onto the network without the
key ever appearing in a message.

---

## Step 2 — Remote Login

macOS cannot run a Tailscale SSH server, so the Mac's own SSH does the
authenticating. Tailscale just supplies the private address.

System Settings → General → Sharing → **Remote Login**: on.

Set *Allow access for* to **Only these users** and add your own account.

Now authorise the VM's key. In Terminal on the Mac, paste this whole block —
it is one command and it creates the file if it doesn't exist:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMoTMKxqebv6pQvWyUvR4GEk9R9khreGy3ieL3Z/zNYZ jack-55@orgo-vm' >> ~/.ssh/authorized_keys && \
chmod 600 ~/.ssh/authorized_keys && echo "authorized"
```

That is a **public** key. It is safe to paste, safe to store, and useless on
its own — the matching private half is on the VM and never leaves it.

While you're here, note your short username:

```bash
whoami
```

---

## Step 3 — Ollama

**Done, and solved a different way than this originally said.**

`launchctl setenv OLLAMA_HOST` did not take — `lsof` showed Ollama still bound
to 127.0.0.1 after a restart, because `launchctl setenv` lands in the GUI
session and the app did not pick it up. Rather than fight that, the daemon was
LEFT on loopback and the VM forwards a port to it over the ssh link it already
has (supervisor keeps `mac-ollama-tunnel` alive).

That is strictly more private than the original instruction: Ollama never
becomes reachable on the network at all. Measured live: qwen3:8b answered in
18.7s cold at 23 tok/s.

If you ever do want it on the network directly, the original approach was:

```bash
mkdir -p ~/Library/LaunchAgents && cat > ~/Library/LaunchAgents/com.jack.ollamahost.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jack.ollamahost</string>
  <key>ProgramArguments</key>
  <array><string>/bin/launchctl</string><string>setenv</string><string>OLLAMA_HOST</string><string>0.0.0.0</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.jack.ollamahost.plist 2>/dev/null; echo "persisted"
```

Pull a model worth using. On Apple Silicon an 8B model answers in seconds:

```bash
ollama pull qwen3:8b
```

**Be honest with yourself about what this is for.** An 8B model is not close
to Sonnet on the work Jack normally does. It is free and private, which makes
it right for bulk classification, redacting something before it goes to a
vendor, and staying useful when an API is down. It is not a replacement for
the paid tiers, and Jack will never quietly demote your work into it.

---

## Step 4 — Claude Code, signed into your subscription

This is the one that saves you money. Build work runs here on your flat-rate
plan instead of being billed per token through the API.

**Claude Code is already installed** at `~/.local/bin/claude` (v2.1.220), so
skip the install. It is just not signed in — the headless probe answers
"Not logged in - Please run /login".

Start it and sign in:

```bash
claude
```

At the prompt type `/login` and follow the browser flow. Use the account your
subscription is on. Then `/exit`.

Prove it works headlessly — this is exactly what Jack will do:

```bash
claude -p "Reply with exactly: BRIDGE-OK" --max-turns 1
```

If that prints `BRIDGE-OK`, the lane is live.

> **Why here and not on the VM.** A consumer subscription is a browser login
> on a machine you own. Authenticating it on a cloud VM would mean minting a
> long-lived token for your personal plan and parking it on a rented machine.
> That is the thing the routing policy forbids, and running it on the Mac
> removes the need entirely. Claude Code is installed on the VM, but it is
> deliberately not signed in and not used for build work.

---

## Step 5 — Codex, the backup lane

**Already done.** Codex CLI 0.146.0 is installed and signed in, and it has
already run a real build through this bridge — it produced an HTML file on the
Mac which was fetched back to the VM. Nothing for you to do here.

---

## Step 6 — BlueBubbles, for iMessage

iMessage is macOS-only. BlueBubbles is a small server that exposes the
Messages app over HTTP, and it is the only way to do this.

1. Make sure **Messages** is signed into your Apple ID on this Mac and that
   you can send a text from it by hand.
2. Download BlueBubbles Server from <https://bluebubbles.app/downloads/>
3. Open it. It will walk you through granting **Full Disk Access** — System
   Settings → Privacy & Security → Full Disk Access → add BlueBubbles. This
   cannot be scripted; macOS requires you to click it.
4. In BlueBubbles → Settings, set a **server password**. Make it long and
   random. Put it in 1Password, `Hermes` vault, item `Hermes Agent Secrets`,
   field name `BLUEBUBBLES_PASSWORD`.
5. Leave the port at **1234**. Do **not** enable any tunnel, ngrok, or dynamic
   DNS option it offers — the whole point of Tailscale is that this stays off
   the public internet.

---

## Step 7 — Tell Jack

Send him this on Telegram, filling in the two blanks:

> Mac is ready. Tailscale name is `mac-mini`, my username is `______`.
> Auth key and BlueBubbles password are in the Hermes vault. Turn the bridge
> on and my number for texts is `+1__________`.

He will bring the VM onto your Tailscale network, fill in
`platform.yaml → mac_bridge`, flip `enabled: true`, and then run:

```
python3 /root/.hermes/scripts/platform/mac_bridge.py status
```

That check is not a config readout. It sends a real one-turn prompt to Claude
Code, a real model list request to Ollama, and a real server query to
BlueBubbles. Every line it prints green means something actually answered.

---

## What you'll be able to do afterwards

| You say | What happens |
|---|---|
| "Build me a one-page PDF summary of this lease" | Jack drafts the brief, asks you to confirm, runs it on Claude Code on your plan, and sends the PDF back on Telegram |
| "Claude's limit is hit" | He switches to Codex without being asked twice |
| "Summarise these forty documents, don't send them anywhere" | Runs on Ollama, on your own hardware, free |
| "Text me the morning brief" | Arrives as an iMessage, capped at six texts a day so it never becomes noise |

Telegram stays the conversation. iMessage is a one-way notice channel for
things worth interrupting a day for — the morning brief, something due today
that nobody has actioned, a calendar conflict inside two hours. Not job
status. Not warnings. Not McKell's household items.
