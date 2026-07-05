#!/bin/bash
cd "$(dirname "$0")"

PYTHON=""
for CANDIDATE in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3 python; do
    if command -v "$CANDIDATE" >/dev/null 2>&1 && "$CANDIDATE" -c "import tkinter" >/dev/null 2>&1; then
        PYTHON="$CANDIDATE"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "No encontre un Python con tkinter instalado."
    echo "Instala Python desde https://www.python.org/downloads/ (incluye tkinter),"
    echo "o si usas Homebrew: brew install python-tk"
    read -p "Presiona Enter para cerrar..."
    exit 1
fi

if ! "$PYTHON" -c "import requests" >/dev/null 2>&1; then
    echo "Instalando dependencia 'requests'..."
    "$PYTHON" -m pip install --user requests
fi

"$PYTHON" smokesync_gui.py

echo ""
read -p "Presiona Enter para cerrar..."
