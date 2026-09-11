#!/usr/bin/env bash
set -euo pipefail

source "$HOME/.aiy_volume.env" 2>/dev/null || true
omlx_env_file="${AIY_OMLX_ENV_FILE:-$HOME/.config/aiy-voice/omlx.env}"
if test -r "$omlx_env_file"; then
  set -a
  source "$omlx_env_file"
  set +a
fi
project_dir="${AIY_PROJECT_DIR:-$HOME/aiy-voice}"
exec "$HOME/.venvs/aiy/bin/python" -u "$project_dir/aiy_button_daemon.py"
