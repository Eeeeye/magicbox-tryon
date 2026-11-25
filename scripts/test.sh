#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
node --test tests/ui_state.test.cjs
python3 tests/integration.py
python3 tests/tryon_integration.py
python3 tests/result_contract.py
.venv/bin/python -m unittest discover -s tests -p 'test_mask.py'
.venv/bin/python tests/visual_acceptance.py --receipts-only
