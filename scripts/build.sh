#!/usr/bin/env bash
# Build causadb binary distribution via PyInstaller.
# Usage: bash scripts/build.sh
# Output: dist/causadb
# Optional: COSIGN_KEY_PATH=<path_to_key> bash scripts/build.sh will also sign.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Building causadb binary ==="
cd "$REPO_DIR"

# Ensure PyInstaller is available
if ! command -v pyinstaller &>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

# Run PyInstaller
pyinstaller build_binary.spec --clean --noconfirm 2>&1

# Sign with cosign if key is available (local builds only)
if [ -n "${COSIGN_KEY_PATH:-}" ]; then
    if command -v cosign &>/dev/null; then
        echo "=== Signing with cosign ==="
        cosign sign-blob --key "$COSIGN_KEY_PATH" \
            --output-signature dist/causadb.sig \
            dist/causadb
        echo "Signature saved to dist/causadb.sig"
    else
        echo "WARNING: COSIGN_KEY_PATH set but cosign not found. Install:"
        echo "  curl https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 -o /usr/local/bin/cosign && chmod +x /usr/local/bin/cosign"
    fi
fi

# --- Packaging ---
if [ "${BUILD_INSTALLERS:-1}" = "1" ]; then
  echo "[build] Packaging installers..."

  # Make unix installer executable
  chmod +x scripts/packaging/unix/install.sh

  # Create tarball with unix installer
  mkdir -p dist
  tar -czf dist/causadb-unix-installer.tar.gz \
    scripts/packaging/unix/install.sh
fi

# Windows installer is built separately in CI (needs Windows runner)
# See .github/workflows/release.yml

echo "=== Build complete ==="
echo "Binary at: $REPO_DIR/dist/causadb"