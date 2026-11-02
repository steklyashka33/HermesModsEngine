#!/usr/bin/env bash
# Git askpass credential helper for HermesModsEngine authentication via GITHUB_TOKEN.
set -euo pipefail

TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ]; then
    ENV_PATH="${HERMES_ENV_PATH:-${HERMES_HOME:-$HOME/.hermes}/.env}"
    if [ -f "$ENV_PATH" ]; then
        TOKEN=$(grep -E '^GITHUB_TOKEN=' "$ENV_PATH" 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '\r"' | tr -d "'")
    fi
fi

case "${1:-}" in
    *Username*|*username*)
        echo "${GITHUB_USER:-steklyashka33}"
        ;;
    *Password*|*password*)
        echo "$TOKEN"
        ;;
    *)
        echo "$TOKEN"
        ;;
esac
