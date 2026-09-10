#!/usr/bin/env bash
set -euo pipefail

source "$HOME/.aiy_volume.env" 2>/dev/null || true
project_dir="${AIY_PROJECT_DIR:-$HOME/aiy-voice}"
exec "$HOME/.venvs/aiy/bin/python" -u "$project_dir/aiy_button_daemon.py"
