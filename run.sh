#!/usr/bin/env bash
set -e

VENV_DIR="venv"

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating python virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Install/upgrade requirements
echo "Ensuring requirements are installed..."
pip install --no-cache-dir -r requirements.txt

# Pass all given arguments to the python script
python pstmortem.py "$@"
