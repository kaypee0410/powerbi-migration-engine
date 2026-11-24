#!/usr/bin/env sh
# Starts Power BI Extraction Insights on http://127.0.0.1:5000 (creates .venv on first run).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment in .venv ..."
  python3 -m venv .venv
fi
. .venv/bin/activate
python -m pip install --disable-pip-version-check -q -r requirements.txt
exec python app.py
