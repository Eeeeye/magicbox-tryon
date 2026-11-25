#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  interpreter="${PYTHON_BIN:-python3.12}"
  "$interpreter" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use Python 3.12"'
  "$interpreter" -m venv .venv
fi
requirements=inference/requirements.txt
if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
  requirements=inference/requirements-macos-arm64.lock
fi
.venv/bin/python -m pip install --disable-pip-version-check -r "$requirements"
.venv/bin/python -m pip check
.venv/bin/python scripts/fetch_assets.py
