#!/bin/bash

# Get the current directory
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if venv exists and if it's broken (points to old location)
if [ -d "venv" ]; then
    if [ -f "venv/pyvenv.cfg" ]; then
        VENV_PATH=$(grep "command.*venv" venv/pyvenv.cfg | sed 's/.*= .* -m venv //' || echo "")
        if [ -n "$VENV_PATH" ] && [ "$VENV_PATH" != "$CURRENT_DIR/venv" ]; then
            echo "Detected virtual environment from old location: $VENV_PATH"
            echo "Removing old virtual environment..."
            rm -rf venv
        fi
    fi
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install requirements
echo "Installing dependencies..."
./venv/bin/pip install -r requirements.txt

echo "Setup complete. You can activate the virtual environment with:"
echo "source venv/bin/activate"
source venv/bin/activate
