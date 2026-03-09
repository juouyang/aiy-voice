#!/usr/bin/env bash
set -euo pipefail
exec "$HOME/.venvs/aiy/bin/python" -u "$HOME/aiy-assistant/button_shutdown_guard.py"
