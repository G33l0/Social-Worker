#!/usr/bin/env bash
# Social Worker - first-time setup for Linux/macOS
set -euo pipefail

echo "=========================================================="
echo " SOCIAL WORKER - SETUP"
echo "=========================================================="

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py --init-db

echo
echo "[OK] Setup complete. Start the console with:"
echo "     source .venv/bin/activate && python main.py"
