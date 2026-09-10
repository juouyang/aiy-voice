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
project_dir="${AIY_PROJECT_DIR:-$HOME/aiy-voice}"
"$HOME/.venvs/aiy/bin/python" -u "$project_dir/aiy_button_echo.py"
