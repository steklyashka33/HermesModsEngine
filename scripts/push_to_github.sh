#!/usr/bin/env bash
# Push HermesModsEngine to GitHub without any service restarts
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# If runner.py is present, delegate directly to engine push command
if [ -f "${MODS_DIR}/runner.py" ]; then
    exec python3 "${MODS_DIR}/runner.py" push "$@"
fi

echo "=== HermesModsEngine Push Helper ==="
echo "Working directory: ${MODS_DIR}"

export GIT_ASKPASS="${SCRIPT_DIR}/git-askpass.sh"
export GIT_TERMINAL_PROMPT="0"

# Verify git repository
if ! git -C "${MODS_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: ${MODS_DIR} is not a git repository." >&2
    exit 1
fi

BRANCH="$(git -C "${MODS_DIR}" rev-parse --abbrev-ref HEAD)"
REMOTE_URL="$(git -C "${MODS_DIR}" remote get-url origin 2>/dev/null || echo "")"

echo "Current branch: ${BRANCH}"
echo "Remote origin: ${REMOTE_URL}"

# Ensure on main
if [ "${BRANCH}" != "main" ]; then
    echo "Switching / renaming current branch to main..."
    git -C "${MODS_DIR}" branch -M main
    BRANCH="main"
fi

# Show last commit
echo "Latest commit:"
git -C "${MODS_DIR}" log -1 --stat || echo "(No commits yet)"

echo ""
echo "Executing: git push -u origin ${BRANCH}..."
git -C "${MODS_DIR}" push -u origin "${BRANCH}"

echo ""
echo "=== Push completed successfully! ==="
