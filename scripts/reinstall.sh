#!/usr/bin/env bash
# Reinstall pyclopse from local source and restart the service.
# Usage: ./scripts/reinstall.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing from $REPO_DIR ..."
uv tool install --reinstall --from "$REPO_DIR" pyclopse 2>&1 | tail -3

echo "Stopping service..."
pyclopse service stop
sleep 2
echo "Starting service..."
pyclopse service start

echo "Done."
