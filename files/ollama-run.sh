#!/bin/bash
# ==========================================================================
# Nick's Stack — Ollama server wrapper (supervised entrypoint)
# ==========================================================================
# Same idiom as the gateway and AgentPhone bridge wrappers: the supervised
# service goes DORMANT (sleeps, does not crash-loop) until Ollama is actually
# installed, then execs the server under a lifetime flock.
#
# This machine runs Supervisor, not systemd. The upstream Ollama installer
# writes a systemd unit that never starts here — this wrapper is what makes
# Ollama a managed service on this platform, and it is deliberately the only
# thing that starts the server.
#
# No secrets are sourced. Ollama needs no credentials; only OLLAMA_HOST and
# OLLAMA_MODELS are read out of ~/.hermes/.env, by name, so the server process
# never inherits the agent's API keys.
set +e
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/root/.local/bin:$PATH

ENV_FILE=/root/.hermes/.env

read_env() {
  # read_env <KEY> <default> — value only, no sourcing, no other variable.
  local key="$1" default="$2" val=""
  if [ -r "$ENV_FILE" ]; then
    val="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?${key}=//p" "$ENV_FILE" | tail -1)"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
  fi
  printf '%s' "${val:-$default}"
}

# `ollama serve` binds OLLAMA_HOST. The rest of the stack stores it as a URL
# (http://127.0.0.1:11434) because the routing layer and the doctor call the
# API with it — strip the scheme so the server gets the host:port it expects.
OLLAMA_HOST_RAW="$(read_env OLLAMA_HOST 'http://127.0.0.1:11434')"
BIND="${OLLAMA_HOST_RAW#http://}"
BIND="${BIND#https://}"
BIND="${BIND%/}"
export OLLAMA_HOST="$BIND"

# Optional: keep models on a specific volume (unset = Ollama's own default).
OLLAMA_MODELS_DIR="$(read_env OLLAMA_MODELS '')"
if [ -n "$OLLAMA_MODELS_DIR" ]; then
  export OLLAMA_MODELS="$OLLAMA_MODELS_DIR"
fi

# Dormant until installed. Bootstrap can write this service before Ollama
# exists (--with-ollama on a machine that has not installed it yet), and a
# sleeping service is far better than a FATAL one in `supervisorctl status`.
until command -v ollama >/dev/null 2>&1; do sleep 30; done

# Lifetime flock: an Orgo boot race can start two supervisords, and two
# `ollama serve` processes fight over :11434 forever. Same defence the gateway
# wrapper uses.
mkdir -p /var/lib/orgo
exec flock /var/lib/orgo/ollama.lock ollama serve
