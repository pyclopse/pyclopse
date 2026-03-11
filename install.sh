#!/usr/bin/env bash
# pyclaw installer
# Usage: bash install.sh [--beta] [--version 0.2.1]
set -e

REPO_SSH="git@github.com:jondecker76/pyclaw.git"
BETA=false
VERSION=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --beta)    BETA=true; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Ensure uv is available
if ! command -v uv &>/dev/null; then
    echo "uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Determine ref to install
if [ "$BETA" = true ]; then
    REF="main"
    LABEL="latest from main (beta)"
elif [ -n "$VERSION" ]; then
    # Normalise: strip leading v, then re-add
    VERSION="${VERSION#v}"
    REF="v${VERSION}"
    LABEL="version ${REF}"
else
    echo "Checking for latest release..."
    REF=$(git ls-remote --tags --sort=-v:refname "$REPO_SSH" 'v*' \
        | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+$' \
        | head -1)
    if [ -z "$REF" ]; then
        echo "✗ Could not find any release tags. Check your SSH access to GitHub."
        echo "  Run: ssh -T git@github.com"
        exit 1
    fi
    LABEL="latest stable release ($REF)"
fi

echo "Installing pyclaw $LABEL..."
uv tool install "git+ssh://$REPO_SSH@$REF"

echo ""
echo "✓ pyclaw installed successfully."
echo ""
echo "Next steps:"
echo "  pyclaw init       # create ~/.pyclaw/config.yaml"
echo "  pyclaw --help     # see all commands"
