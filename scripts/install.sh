#!/usr/bin/env bash
set -euo pipefail
cd /workspace/NBAPPBOT
python3 -m venv .venv
.venv/bin/python -m pip install --cache-dir data/pip-cache -r requirements.lock
.venv/bin/python -m pip install --cache-dir data/pip-cache --no-deps -e .
.venv/bin/python -m nbabot init
.venv/bin/python -m nbabot recover
