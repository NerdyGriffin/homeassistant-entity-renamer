#!/bin/bash

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
