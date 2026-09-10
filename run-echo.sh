#!/usr/bin/env bash
set -euo pipefail

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl stop aiy-shutdown-guard.service >/dev/null 2>&1 || true
fi

cleanup() {
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start aiy-shutdown-guard.service >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

source "$HOME/.aiy_volume.env" 2>/dev/null || true
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$HOME/.venvs/aiy/bin/python" -u "$script_dir/aiy_button_echo.py"
