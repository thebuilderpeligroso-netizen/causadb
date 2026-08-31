"""Auto-update module for CausaDB.

Checks GitHub Releases for new versions, downloads, verifies cosign
signatures, and applies updates atomically.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_API_RELEASES = "https://api.github.com/repos/causadb/causadb/releases/latest"


def get_current_version() -> str:
    """Read the current version from pyproject.toml."""
    import causadb

    pkg_dir = os.path.dirname(causadb.__file__)
    pyproject_path = os.path.join(os.path.dirname(pkg_dir), "pyproject.toml")

    if os.path.isfile(pyproject_path):
        with open(pyproject_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("version"):
                    # "version = \"0.1.0\""
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"').strip("'")
                        return val

    return "0.0.0"


def get_latest_release() -> dict:
    """Fetch the latest release metadata from GitHub Releases.

    Returns:
        dict with keys ``tag_name`` and ``assets`` (list of asset dicts).

    Raises:
        urllib.error.URLError: on network failure.
        ValueError: on JSON parse failure.
    """
    req = urllib.request.Request(
        GITHUB_API_RELEASES,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "causadb-updater"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return {"tag_name": data.get("tag_name", ""), "assets": data.get("assets", [])}


def check_update() -> dict:
    """Compare current version against the latest GitHub release.

    Returns:
        dict with ``needs_update`` (bool), ``latest_version`` (str),
        ``current_version`` (str).
    """
    current = get_current_version()
    try:
        latest = get_latest_release()
        latest_tag = latest["tag_name"]
    except Exception as e:
        logger.warning("Failed to check for updates: %s", e)
        return {"needs_update": False, "latest_version": current, "current_version": current}

    # Strip leading 'v' for comparison
    latest_clean = latest_tag.lstrip("v")
    current_clean = current.lstrip("v")

    needs = _version_gt(latest_clean, current_clean)
    return {"needs_update": needs, "latest_version": latest_tag, "current_version": current}


def _version_gt(a: str, b: str) -> bool:
    """Return True if version a > version b (semver-like comparison)."""
    try:
        parts_a = [int(x) for x in a.split(".")]
        parts_b = [int(x) for x in b.split(".")]
    except (ValueError, AttributeError):
        return a != b

    max_len = max(len(parts_a), len(parts_b))
    parts_a.extend([0] * (max_len - len(parts_a)))
    parts_b.extend([0] * (max_len - len(parts_b)))

    for pa, pb in zip(parts_a, parts_b):
        if pa > pb:
            return True
        if pa < pb:
            return False
    return False


def download_update(version: str) -> str:
    """Download the binary asset for *version* to a temp file.

    Returns the path to the downloaded binary.
    """
    latest = get_latest_release()
    assets = latest.get("assets", [])
    binary_url = None
    for asset in assets:
        if asset.get("name") == "causadb":
            binary_url = asset.get("browser_download_url")
            break

    if not binary_url:
        raise RuntimeError(f"No 'causadb' binary asset found in release {version}")

    tmpdir = tempfile.mkdtemp(prefix="causadb_update_")
    binary_path = os.path.join(tmpdir, "causadb")

    logger.info("Downloading %s -> %s", binary_url, binary_path)
    urllib.request.urlretrieve(binary_url, binary_path)
    os.chmod(binary_path, 0o755)

    return binary_path


def verify_signature(binary_path: str, sig_path: str, cert_path: str) -> bool:
    """Verify cosign signature of the binary.

    Returns True if verification passes.

    Raises:
        RuntimeError: If cosign is not found or verification fails.
    """
    cosign = shutil.which("cosign")
    if cosign is None:
        raise RuntimeError(
            "cosign not found in PATH. Install cosign to verify update signatures: "
            "https://docs.sigstore.dev/cosign/installation/"
        )

    result = subprocess.run(
        [
            cosign, "verify-blob",
            "--signature", sig_path,
            "--certificate", cert_path,
            binary_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Signature verification failed: {result.stderr.strip()}")

    logger.info("Signature verified for %s", binary_path)
    return True


def apply_update(binary_path: str) -> str:
    """Replace the current binary with the new one via atomic rename.

    Returns the path of the replaced binary.
    """
    current_binary = sys.argv[0]
    if not os.path.isfile(current_binary):
        raise RuntimeError(f"Current binary not found: {current_binary}")

    backup = current_binary + ".bak"
    shutil.copy2(current_binary, backup)
    try:
        shutil.copy2(binary_path, current_binary)
        os.chmod(current_binary, 0o755)
    except Exception:
        # Rollback
        shutil.copy2(backup, current_binary)
        raise

    return backup


def install_or_check(check_only: bool = False) -> dict:
    """Orchestrate the full update flow.

    Args:
        check_only: If True, only check and return status without downloading.

    Returns:
        dict with update status.
    """
    status = check_update()
    if not status["needs_update"]:
        return status

    if check_only:
        return status

    latest = status["latest_version"]
    logger.info("Downloading update to %s", latest)
    binary_path = download_update(latest)

    # Download signature and certificate
    latest_release = get_latest_release()
    assets = latest_release.get("assets", [])
    tmpdir = os.path.dirname(binary_path)

    sig_path = None
    cert_path = None
    for asset in assets:
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if name == "causadb.sig":
            sig_path = os.path.join(tmpdir, "causadb.sig")
            urllib.request.urlretrieve(url, sig_path)
        elif name == "causadb.pem":
            cert_path = os.path.join(tmpdir, "causadb.pem")
            urllib.request.urlretrieve(url, cert_path)

    if sig_path and cert_path:
        verify_signature(binary_path, sig_path, cert_path)

    apply_update(binary_path)
    return status