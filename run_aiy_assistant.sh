#!/usr/bin/env bash
set -euo pipefail
source "$HOME/.aiy_openai.env"
source "$HOME/.aiy_volume.env" 2>/dev/null || true
exec "$HOME/.venvs/aiy/bin/python" -u "$HOME/aiy-assistant/aiy_assistant.py"
