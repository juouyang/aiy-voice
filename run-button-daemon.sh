#!/usr/bin/env bash
set -euo pipefail

source "$HOME/.aiy_volume.env" 2>/dev/null || true
omlx_env_file="${AIY_OMLX_ENV_FILE:-$HOME/.config/aiy-voice/omlx.env}"
if test -r "$omlx_env_file"; then
  set -a
  source "$omlx_env_file"
  set +a
fi
ntfy_env_file="${AIY_NTFY_ENV_FILE:-$HOME/.config/aiy-voice/ntfy.env}"
if test -r "$ntfy_env_file"; then
  set -a
  source "$ntfy_env_file"
  set +a
fi
openai_env_file="${AIY_OPENAI_ENV_FILE:-$HOME/.config/aiy-voice/openai.env}"
if test -r "$openai_env_file"; then
  set -a
  source "$openai_env_file"
  set +a
fi
project_dir="${AIY_PROJECT_DIR:-$HOME/aiy-voice}"
exec "$HOME/.venvs/aiy/bin/python" -u "$project_dir/aiy_button_daemon.py"
