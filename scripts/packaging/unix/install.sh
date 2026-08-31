#!/usr/bin/env bash
# CausaDB Unix Installer
set -euo pipefail

CAUSADB_HOME="${CAUSADB_HOME:-$HOME/.causadb}"
INSTALL_PREFIX="${PREFIX:-/usr/local}"
PY="${PYTHON:-python3}"

echo "CausaDB Installer"
echo "================="
echo "  Target: $INSTALL_PREFIX"
echo "  Python: $PY"
echo "  Home:   $CAUSADB_HOME"
echo ""

# Check Python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Install Python 3.10+ first."
  exit 1
fi

# Check Python version >= 3.10
PY_VERSION=$($PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
  echo "ERROR: Python 3.10+ required, found $PY_VERSION"
  exit 1
fi

# Create home dir
mkdir -p "$CAUSADB_HOME"
mkdir -p "$CAUSADB_HOME/ledgers"

# Install via pip
echo "Installing CausaDB..."
$PY -m pip install --user causadb 2>/dev/null || $PY -m pip install causadb
# Fallback: local install from source if pip fails
if ! command -v causadb >/dev/null 2>&1; then
  echo "pip install failed, trying local source..."
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
  $PY -m pip install --user -e "$PROJECT_ROOT"
fi

# Verify
if ! command -v causadb >/dev/null 2>&1; then
  echo "ERROR: causadb command not found after install"
  exit 1
fi

# Create config if missing
if [ ! -f "$CAUSADB_HOME/config.json" ]; then
  echo '{"api_key": null, "ledgers_dir": "'"$CAUSADB_HOME/ledgers"'"}' > "$CAUSADB_HOME/config.json"
fi

# Optional: install systemd unit
if [ -d "/etc/systemd/system" ] && [ "$(id -u)" -eq 0 ]; then
  cat > /etc/systemd/system/causadb.service << 'UNIT'
[Unit]
Description=CausaDB Daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/causadb serve --port 7457
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  echo "Systemd unit installed. Enable with: systemctl enable --now causadb"
fi

echo ""
echo "✓ CausaDB installed: $(causadb --version 2>/dev/null || echo 'version unknown')"
echo "✓ Home: $CAUSADB_HOME"
echo ""
echo "Next steps:"
echo "  causadb serve                  # start daemon"
echo "  causadb --help                 # see all commands"
