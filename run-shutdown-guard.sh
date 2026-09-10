#!/usr/bin/env bash
set -euo pipefail

daemon_service="aiy-button-daemon.service"
daemon_was_active=0

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$daemon_service"; then
  sudo systemctl stop "$daemon_service"
  daemon_was_active=1
fi

cleanup() {
  if (( daemon_was_active )); then
    sudo systemctl start "$daemon_service"
  fi
}

trap cleanup EXIT

source "$HOME/.aiy_volume.env" 2>/dev/null || true
project_dir="${AIY_PROJECT_DIR:-$HOME/aiy-voice}"
"$HOME/.venvs/aiy/bin/python" -u "$project_dir/button_shutdown_guard.py"
