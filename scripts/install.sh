#!/bin/sh
# looplane installer — one-liner: curl -fsSL https://raw.githubusercontent.com/vincentxuu/looplane/main/scripts/install.sh | sh
set -eu

BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
RED="\033[31m"
RESET="\033[0m"

info()  { printf "${GREEN}▸${RESET} %s\n" "$*"; }
error() { printf "${RED}✗${RESET} %s\n" "$*" >&2; }
dim()   { printf "${DIM}%s${RESET}\n" "$*"; }

# ── Detect platform ──────────────────────────────────────────────────
OS=$(uname -s)
ARCH=$(uname -m)

# ── Ensure Python 3.11+ ─────────────────────────────────────────────
check_python() {
    if command -v python3 >/dev/null 2>&1; then
        ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            return 0
        fi
    fi
    return 1
}

# ── Ensure uv ────────────────────────────────────────────────────────
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        dim "uv $(uv --version 2>/dev/null || echo '?') found"
        return 0
    fi
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env so uv is on PATH for the rest of this script
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        . "$HOME/.cargo/env"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        error "uv install succeeded but 'uv' not found on PATH."
        error "Add ~/.local/bin to your PATH, then re-run this script."
        exit 1
    fi
}

# ── Ensure Python via uv if needed ───────────────────────────────────
ensure_python() {
    if check_python; then
        dim "Python $(python3 --version 2>&1) found"
        return 0
    fi
    info "Installing Python 3.12 via uv..."
    uv python install 3.12
}

# ── Install looplane ─────────────────────────────────────────────────
install_looplane() {
    if command -v looplane >/dev/null 2>&1; then
        info "Upgrading looplane..."
        uv tool upgrade looplane 2>/dev/null || uv tool install looplane --force
    else
        info "Installing looplane..."
        uv tool install looplane
    fi
}

# ── Main ─────────────────────────────────────────────────────────────
main() {
    printf "\n${BOLD}looplane installer${RESET}\n\n"

    ensure_uv
    ensure_python
    install_looplane

    printf "\n${GREEN}✓${RESET} ${BOLD}looplane installed successfully!${RESET}\n\n"
    echo "  Get started:"
    echo ""
    echo "    looplane              # interactive TUI"
    echo "    looplane --help       # all commands"
    echo "    looplane update       # self-update"
    echo ""

    # Remind about PATH if needed
    tool_bin="$(uv tool dir 2>/dev/null)/looplane/bin" 2>/dev/null || true
    if [ -n "$tool_bin" ] && ! echo "$PATH" | tr ':' '\n' | grep -qx "$tool_bin"; then
        dim "If 'looplane' isn't found, add this to your shell profile:"
        dim "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

main
