#!/usr/bin/env bash
# Push Muse Studio's keys from env.local to Railway as service variables.
# Deliberately does NOT push BLOTATO_IG_ACCOUNT_ID - clients pick their account in the UI.
# Usage:  cd muse-studio && bash scripts/push-railway-vars.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f env.local ]; then
  echo "env.local not found - copy env.example to env.local and fill it in first." >&2
  exit 1
fi

get() { grep -E "^$1=" env.local | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }

FAL_KEY=$(get FAL_KEY)
OPENROUTER_API_KEY=$(get OPENROUTER_API_KEY)
OPENROUTER_IDEAS_MODEL=$(get OPENROUTER_IDEAS_MODEL)
BLOTATO_API_KEY=$(get BLOTATO_API_KEY)
STUDIO_USER=$(get STUDIO_USER)
STUDIO_PASSWORD=$(get STUDIO_PASSWORD)

for req in FAL_KEY OPENROUTER_API_KEY STUDIO_PASSWORD; do
  if [ -z "${!req}" ]; then
    echo "Missing $req in env.local - a hosted deploy needs it (STUDIO_PASSWORD keeps the app locked)." >&2
    exit 1
  fi
done

ARGS=(--set "FAL_KEY=$FAL_KEY" --set "OPENROUTER_API_KEY=$OPENROUTER_API_KEY" --set "STUDIO_PASSWORD=$STUDIO_PASSWORD")
[ -n "$OPENROUTER_IDEAS_MODEL" ] && ARGS+=(--set "OPENROUTER_IDEAS_MODEL=$OPENROUTER_IDEAS_MODEL")
[ -n "$BLOTATO_API_KEY" ] && ARGS+=(--set "BLOTATO_API_KEY=$BLOTATO_API_KEY")
[ -n "$STUDIO_USER" ] && ARGS+=(--set "STUDIO_USER=$STUDIO_USER")

railway variables "${ARGS[@]}"
echo "Variables pushed. Redeploy to pick them up:  railway up"
