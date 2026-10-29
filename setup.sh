#!/usr/bin/env bash
# One-time setup: create the virtualenv and install the SDK.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Setup done. Fill accounts.csv, then run ./run.sh"
