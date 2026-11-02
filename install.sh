#!/usr/bin/env bash
# ==============================================================================
# HermesModsEngine — Universal One-Line Automated Installer & Synchronizer
#
# Production-grade, zero-downtime installer and synchronizer for HermesModsEngine.
# Detects runtime environment (Linux / macOS), verifies Python 3.10+, locates
# installed Hermes Agent, sets secure file permissions, safely clones or updates
# without wiping user modifications, compiles atomic caches, and applies mods
# live (NEVER restarts systemd or gateway services).
#
# Quick Installation:
#   curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash
#
# Local Execution:
#   ./install.sh [options]
#
# Options:
#   --target <dir>     Target install path (default: ~/.hermes/mods)
#   --repo <url>       Git repository URL (default: official repo)
#   --branch <branch>  Git branch to clone/checkout (default: main)
#   --dry-run          Simulate checks without modifying files on disk
#   --no-apply         Install/update engine without running 'runner.py apply'
#   --cron             Automatically install nightly auto-update cron job
#   --enable-cron      Alias for --cron
#   --cron-schedule <s> Custom cron schedule expression (default: "0 4 * * *")
#   --force            Force reset or fresh clone when resolving conflicts
#   -h, --help         Display this help message
#
# Environment Variables:
#   HERMES_HOME        Base directory for Hermes Agent (default: ~/.hermes)
#   HERMES_MODS_DIR    Target installation directory (overrides default)
#   HERMES_MODS_REPO   Git repository URL
#   HERMES_MODS_BRANCH Git branch name
#   HERMES_ROOT        Path to Hermes Agent core repository
#   HERMES_PYTHON      Explicit Python 3.10+ interpreter path
# ==============================================================================

set -eo pipefail

# --- Color and Styling ---
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    RESET="$(tput sgr0)"
    BOLD="$(tput bold)"
    GREEN="$(tput setaf 2)"
    BLUE="$(tput setaf 4)"
    CYAN="$(tput setaf 6)"
    YELLOW="$(tput setaf 3)"
    RED="$(tput setaf 1)"
    MAGENTA="$(tput setaf 5)"
else
    RESET=""
    BOLD=""
    GREEN=""
    BLUE=""
    CYAN=""
    YELLOW=""
    RED=""
    MAGENTA=""
fi

# --- Configuration Defaults ---
DEFAULT_REPO_URL="https://github.com/steklyashka33/HermesModsEngine.git"
REPO_URL="${HERMES_MODS_REPO:-$DEFAULT_REPO_URL}"
BRANCH="${HERMES_MODS_BRANCH:-main}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="${HERMES_MODS_DIR:-$HERMES_HOME/mods}"
DRY_RUN=false
APPLY_MODS=true
ENABLE_CRON=false
CRON_SCHEDULE="0 4 * * *"
FORCE=false

# --- Logging Helpers ---
log_info() {
    printf "${BLUE}ℹ${RESET} %s\n" "$*"
}

log_step() {
    printf "${CYAN}▶${RESET} ${BOLD}%s${RESET}\n" "$*"
}

log_success() {
    printf "${GREEN}✔${RESET} %s\n" "$*"
}

log_warning() {
    printf "${YELLOW}⚠${RESET} %s\n" "$*"
}

log_error() {
    printf "${RED}✖${RESET} ${BOLD}%s${RESET}\n" "$*" >&2
}

print_banner() {
    cat << 'EOF'
  _    _                                __  __           _       ______             _            
 | |  | |                              |  \/  |         | |     |  ____|           (_)           
 | |__| | ___ _ __ _ __ ___   ___  ___ | \  / | ___   __| |___  | |__   _ __   __ _ _ _ __   ___ 
 |  __  |/ _ \ '__| '_ ` _ \ / _ \/ __|| |\/| |/ _ \ / _` / __| |  __| | '_ \ / _` | | '_ \ / _ \
 | |  | |  __/ |  | | | | | |  __/\__ \| |  | | (_) | (_| \__ \ | |____| | | | (_| | | | | |  __/
 |_|  |_|\___|_|  |_| |_| |_|\___||___/|_|  |_|\___/ \__,_|___/ |______|_| |_|\__, |_|_| |_|\___|
                                                                                __/ |             
                                                                               |___/              
EOF
    printf "  ${BOLD}Enterprise Hot-Patching & Extension Engine for Hermes Agent${RESET}\n"
    printf "  ${MAGENTA}Zero Downtime${RESET} • ${CYAN}Delta Snapshots${RESET} • ${GREEN}AST Validated${RESET} • ${YELLOW}8-Language i18n${RESET}\n\n"
}

print_usage() {
    cat << EOF
Usage: install.sh [OPTIONS]

Options:
  --target <dir>     Target install path (default: ~/.hermes/mods)
  --repo <url>       Git repository URL (default: $DEFAULT_REPO_URL)
  --branch <branch>  Git branch to clone (default: main)
  --dry-run          Simulate installation without modifying files
  --no-apply         Install engine repository without running 'runner.py apply'
  --cron             Automatically install nightly auto-update cron job
  --enable-cron      Alias for --cron
  --cron-schedule <s> Custom cron schedule expression (default: "0 4 * * *")
  --force            Force installation, overwrite uncommitted changes or conflicts
  -h, --help         Display this help message

Environment Variables:
  HERMES_HOME        Base directory for Hermes Agent (default: ~/.hermes)
  HERMES_MODS_DIR    Target installation directory (overrides default)
  HERMES_MODS_REPO   Git repository URL
  HERMES_MODS_BRANCH Git branch name
  HERMES_ROOT        Path to Hermes Agent core repository
  HERMES_PYTHON      Explicit Python 3.10+ interpreter path

EOF
}

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET_DIR="$2"
            shift 2
            ;;
        --repo)
            REPO_URL="$2"
            shift 2
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --no-apply)
            APPLY_MODS=false
            shift
            ;;
        --cron|--enable-cron)
            ENABLE_CRON=true
            shift
            ;;
        --cron-schedule)
            ENABLE_CRON=true
            CRON_SCHEDULE="$2"
            shift 2
            ;;
        --force)
            FORCE=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
    esac
done

# --- Helper: Find Python 3.10+ ---
find_python() {
    local candidates=()

    # 1. Explicit environment variable
    if [[ -n "${HERMES_PYTHON:-}" ]]; then
        candidates+=("$HERMES_PYTHON")
    fi

    # 2. Known Hermes virtual environments
    if [[ -x "/usr/local/lib/hermes-agent/venv/bin/python3" ]]; then
        candidates+=("/usr/local/lib/hermes-agent/venv/bin/python3")
    fi
    if [[ -x "/usr/local/lib/hermes-agent/.venv/bin/python3" ]]; then
        candidates+=("/usr/local/lib/hermes-agent/.venv/bin/python3")
    fi
    if [[ -x "$HERMES_HOME/venv/bin/python3" ]]; then
        candidates+=("$HERMES_HOME/venv/bin/python3")
    fi

    # Probe dynamic Hermes installations venv
    for venv_py in "$HERMES_HOME"/installs/*/environments/*/venv/bin/python3; do
        if [[ -x "$venv_py" ]]; then
            candidates+=("$venv_py")
            break
        fi
    done

    # 3. macOS Homebrew paths
    if [[ -x "/opt/homebrew/bin/python3" ]]; then
        candidates+=("/opt/homebrew/bin/python3")
    fi
    if [[ -x "/usr/local/bin/python3" ]]; then
        candidates+=("/usr/local/bin/python3")
    fi

    # 4. Standard path candidates
    if command -v python3 >/dev/null 2>&1; then
        candidates+=("$(command -v python3)")
    fi
    for v in 14 13 12 11 10; do
        if command -v "python3.$v" >/dev/null 2>&1; then
            candidates+=("$(command -v "python3.$v")")
        fi
    done

    # Validate version >= 3.10
    for py in "${candidates[@]}"; do
        if [[ -x "$py" ]]; then
            local is_valid
            is_valid="$("$py" -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || echo 0)"
            if [[ "$is_valid" -eq 1 ]]; then
                echo "$py"
                return 0
            fi
        fi
    done

    return 1
}

# --- Helper: Detect Hermes Agent Core Root ---
detect_hermes_core() {
    local py_bin="$1"

    # 1. HERMES_ROOT / HERMES_ROOT_DIR / HERMES_AGENT_PATH environment variable
    local env_root="${HERMES_ROOT:-${HERMES_ROOT_DIR:-${HERMES_AGENT_PATH:-}}}"
    if [[ -n "$env_root" ]] && [[ -d "$env_root" ]]; then
        echo "$env_root"
        return 0
    fi

    # 2. Check hermes CLI output if hermes executable exists
    if command -v hermes >/dev/null 2>&1; then
        local cli_dir
        cli_dir="$(hermes --version 2>/dev/null | grep -i "Install directory:" | head -n1 | awk '{print $3}' || true)"
        if [[ -n "$cli_dir" ]] && [[ -d "$cli_dir" ]]; then
            echo "$cli_dir"
            return 0
        fi
    fi

    # 3. Standard filesystem locations
    local std_locations=(
        "/usr/local/lib/hermes-agent"
        "/opt/hermes-agent"
        "$HOME/hermes-agent"
        "$HERMES_HOME/agent"
    )
    for loc in "${std_locations[@]}"; do
        if [[ -d "$loc" ]] && { [[ -d "$loc/agent" ]] || [[ -f "$loc/hermes_constants.py" ]]; }; then
            echo "$loc"
            return 0
        fi
    done

    # 4. Probe via Python sys.path, site-packages, and imported modules
    if [[ -n "$py_bin" ]]; then
        local detected
        detected="$("$py_bin" -c '
import sys, os
from pathlib import Path

# Module inspection
for mod_name in ["hermes_cli", "hermes_agent", "hermes"]:
    try:
        mod = __import__(mod_name)
        if hasattr(mod, "__file__") and mod.__file__:
            p = Path(mod.__file__).resolve()
            cur = p.parent
            for _ in range(5):
                if (cur / "agent").is_dir() or (cur / "hermes_constants.py").is_file():
                    print(str(cur.resolve()))
                    sys.exit(0)
                cur = cur.parent
    except Exception:
        pass

# sys.path search
for p_str in sys.path:
    if not p_str:
        continue
    p = Path(p_str).resolve()
    if not p.is_dir():
        continue
    if (p / "agent").is_dir() and (p / "hermes_constants.py").is_file():
        print(str(p.resolve()))
        sys.exit(0)
    for parent in [p.parent, p.parent.parent]:
        if (parent / "agent").is_dir() and (parent / "hermes_constants.py").is_file():
            print(str(parent.resolve()))
            sys.exit(0)
' 2>/dev/null || true)"
        if [[ -n "$detected" ]] && [[ -d "$detected" ]]; then
            echo "$detected"
            return 0
        fi
    fi

    return 1
}

# --- Main Installation Flow ---
main() {
    print_banner

    log_step "Step 1: Inspecting Runtime Environment"

    # 1. Verify Git CLI
    if ! command -v git >/dev/null 2>&1; then
        log_error "Git is not installed."
        log_info "Please install Git and retry:"
        log_info "  Debian/Ubuntu: sudo apt update && sudo apt install -y git"
        log_info "  macOS (Homebrew): brew install git"
        log_info "  RHEL/CentOS/Fedora: sudo dnf install -y git"
        exit 1
    fi
    log_success "Git CLI detected: $(git --version)"

    # 2. Verify Python 3.10+
    local python_bin
    if ! python_bin="$(find_python)"; then
        log_error "Python 3.10+ is required, but no compatible interpreter was found."
        log_info "Please install Python 3.10 or newer (or set HERMES_PYTHON=/path/to/python3)."
        exit 1
    fi

    local py_version
    py_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
    log_success "Python interpreter verified: $python_bin (version $py_version)"

    # 3. Detect Hermes Agent Core
    local hermes_core_dir
    if hermes_core_dir="$(detect_hermes_core "$python_bin")"; then
        export HERMES_ROOT="$hermes_core_dir"
        log_success "Hermes Agent core detected: $hermes_core_dir"
    else
        log_warning "Hermes Agent core not automatically located. Operating in standalone engine mode."
        log_info "You can specify HERMES_ROOT=/path/to/hermes-agent if installed elsewhere."
    fi

    log_info "Target Directory:   $TARGET_DIR"
    log_info "Source Repository:  $REPO_URL (branch: $BRANCH)"

    # Handle Dry-Run Mode
    if [[ "$DRY_RUN" == true ]]; then
        printf "\n"
        log_warning "DRY RUN MODE ENABLED: No changes will be written to disk."
        if [[ "$ENABLE_CRON" == true ]]; then
            log_info "Simulating auto-update cron setup: runner.py cron install --schedule \"$CRON_SCHEDULE\""
        fi
        log_info "Checks completed successfully. Ready for deployment."
        exit 0
    fi

    log_step "Step 2: Deploying HermesModsEngine"

    mkdir -p "$(dirname "$TARGET_DIR")"

    if [[ -d "$TARGET_DIR/.git" ]]; then
        log_info "Existing Git repository found at $TARGET_DIR. Updating from remote..."
        (
            cd "$TARGET_DIR"
            git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
            git fetch origin "$BRANCH" --tags
            if [[ "$FORCE" == true ]]; then
                log_warning "Force flag enabled: resetting local tracked files to origin/$BRANCH"
                git reset --hard "origin/$BRANCH"
            else
                git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
                # Pull changes without destroying untracked custom mods or user secrets
                git pull origin "$BRANCH" --ff-only 2>/dev/null || git pull origin "$BRANCH" --rebase=false 2>/dev/null || log_warning "Local modifications detected. Repository fetched."
            fi
        )
        log_success "Repository updated successfully."
    elif [[ -d "$TARGET_DIR" ]]; then
        log_info "Directory $TARGET_DIR exists without Git metadata. Initializing in-place safely..."
        (
            cd "$TARGET_DIR"
            git init
            git remote add origin "$REPO_URL" 2>/dev/null || git remote set-url origin "$REPO_URL"
            git fetch origin "$BRANCH" --depth 1
            if [[ "$FORCE" == true ]]; then
                git reset --hard "origin/$BRANCH"
            else
                # Checkout origin branch files while preserving existing custom mods and secrets
                git checkout -B "$BRANCH" "origin/$BRANCH" 2>/dev/null || {
                    log_warning "Resolving checkout into existing directory..."
                    git checkout -B "$BRANCH"
                    git pull origin "$BRANCH" --rebase=false 2>/dev/null || true
                }
            fi
        )
        log_success "In-place repository initialization completed."
    else
        log_info "Cloning $REPO_URL into $TARGET_DIR..."
        git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET_DIR"
        log_success "Repository cloned successfully."
    fi

    # 4. Ensure Permissions and Executable Bits
    if [[ -f "$TARGET_DIR/runner.py" ]]; then
        chmod +x "$TARGET_DIR/runner.py"
    else
        log_error "runner.py missing in $TARGET_DIR. Installation aborted."
        exit 1
    fi

    # Strict 0600 POSIX File Security for secrets
    if [[ -f "$HERMES_HOME/secrets.yaml" ]]; then
        chmod 0600 "$HERMES_HOME/secrets.yaml"
        log_success "Enforced POSIX 0600 permissions on $HERMES_HOME/secrets.yaml"
    fi
    if [[ -f "$TARGET_DIR/secrets.yaml" ]]; then
        chmod 0600 "$TARGET_DIR/secrets.yaml"
        log_success "Enforced POSIX 0600 permissions on $TARGET_DIR/secrets.yaml"
    fi

    log_step "Step 3: Running Pre-flight Diagnostics"
    (
        cd "$TARGET_DIR"
        "$python_bin" runner.py doctor --no-color
    )
    log_success "Engine diagnostics passed."

    log_step "Step 4: Compiling Dynamic JSON Caches"
    log_info "Compiling .commands_cache.json, .locales_cache.json, and .mcp_cache.json..."
    (
        cd "$TARGET_DIR"
        "$python_bin" runner.py commands --json >/dev/null 2>&1 || "$python_bin" runner.py commands
    )
    log_success "Runtime JSON caches successfully generated."

    if [[ "$APPLY_MODS" == true ]]; then
        log_step "Step 5: Transactional Application of Active Production Mods"
        log_info "Applying mods with zero downtime (In-Memory AST verification)..."
        (
            cd "$TARGET_DIR"
            "$python_bin" runner.py apply
        )
        log_success "All registered mods applied successfully."
    else
        log_info "Skipping mod application (--no-apply was specified)."
        log_info "You can apply mods at any time by running: python3 $TARGET_DIR/runner.py apply"
    fi

    log_step "Step 6: Verifying Ecosystem Health Status"
    (
        cd "$TARGET_DIR"
        "$python_bin" runner.py status
    )

    if [[ "$ENABLE_CRON" == true ]]; then
        log_step "Step 7: Configuring Automated Nightly Updates (Cron)"
        log_info "Scheduling nightly three-phase auto-update ($CRON_SCHEDULE)..."
        (
            cd "$TARGET_DIR"
            "$python_bin" runner.py cron install --schedule "$CRON_SCHEDULE"
        )
        log_success "Automated auto-update cron job configured successfully."
    fi

    printf "\n"
    printf "${GREEN}════════════════════════════════════════════════════════════════════════════════${RESET}\n"
    printf "  ${BOLD}${GREEN}🎉 HermesModsEngine Successfully Installed & Verified!${RESET}\n"
    printf "${GREEN}════════════════════════════════════════════════════════════════════════════════${RESET}\n\n"
    printf "  ${BOLD}Essential Commands:${RESET}\n"
    printf "    • View ecosystem status:         ${CYAN}python3 %s/runner.py status${RESET}\n" "$TARGET_DIR"
    printf "    • Auto-update cron status:       ${CYAN}python3 %s/runner.py cron status${RESET}\n" "$TARGET_DIR"
    printf "    • List all active mods:          ${CYAN}python3 %s/runner.py list${RESET}\n" "$TARGET_DIR"
    printf "    • Update engine in 1 command:    ${CYAN}python3 %s/runner.py update${RESET}\n" "$TARGET_DIR"
    printf "    • Inspect slash commands UX:     ${CYAN}python3 %s/runner.py commands${RESET}\n" "$TARGET_DIR"
    printf "    • Run healthcheck & doctor:      ${CYAN}python3 %s/runner.py doctor${RESET}\n" "$TARGET_DIR"
    printf "    • View unified core diff:        ${CYAN}python3 %s/runner.py diff${RESET}\n" "$TARGET_DIR"
    printf "    • Create a new custom mod:       ${CYAN}python3 %s/runner.py create <my-mod> --type folder${RESET}\n\n" "$TARGET_DIR"
    printf "  ${BOLD}Documentation & Resources:${RESET}\n"
    printf "    • AI Agent Handbook:             %s/AGENTS.md\n" "$TARGET_DIR"
    printf "    • Architecture Guide:            %s/docs/architecture.md\n" "$TARGET_DIR"
    printf "    • Mod Specification:             %s/SPECIFICATION.md\n\n" "$TARGET_DIR"
    printf "  ${BOLD}Note:${RESET} Zero service restarts were performed. All caches updated live.\n\n"
}

main "$@"
