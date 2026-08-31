import os
import subprocess
import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "packaging")


def test_unix_installer_exists():
    path = os.path.join(SCRIPTS_DIR, "unix", "install.sh")
    assert os.path.isfile(path), "install.sh not found"
    assert os.access(path, os.X_OK), "install.sh is not executable"


def test_unix_installer_syntax():
    path = os.path.join(SCRIPTS_DIR, "unix", "install.sh")
    result = subprocess.run(["bash", "-n", path], capture_output=True)
    assert result.returncode == 0, f"Bash syntax error: {result.stderr.decode()}"


def test_unix_installer_has_required_steps():
    path = os.path.join(SCRIPTS_DIR, "unix", "install.sh")
    with open(path) as f:
        content = f.read()
    required = ["set -euo pipefail", "python3", "pip install", "config.json", "$HOME/.causadb"]
    for token in required:
        assert token in content, f"Missing token in install.sh: {token}"


def test_windows_installer_exists():
    path = os.path.join(SCRIPTS_DIR, "windows", "installer_template.ps1")
    assert os.path.isfile(path), "installer_template.ps1 not found"


def test_windows_sfx_builder_exists():
    path = os.path.join(SCRIPTS_DIR, "windows", "build_sfx.ps1")
    assert os.path.isfile(path), "build_sfx.ps1 not found"


def test_windows_installer_has_required_steps():
    path = os.path.join(SCRIPTS_DIR, "windows", "installer_template.ps1")
    with open(path) as f:
        content = f.read()
    required = ["python", "pip install", "config.json", "USERPROFILE", "causadb"]
    for token in required:
        assert token in content, f"Missing token in ps1: {token}"


def test_build_sh_includes_packaging_step():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "build.sh")
    with open(path) as f:
        content = f.read()
    assert "packaging" in content.lower(), "build.sh missing packaging step"
    assert "BUILD_INSTALLERS" in content, "build.sh missing BUILD_INSTALLERS flag"


def test_release_workflow_has_windows_job():
    path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "release.yml")
    with open(path) as f:
        content = f.read()
    assert "windows-installer" in content, "release.yml missing windows-installer job"
    assert "windows-latest" in content, "release.yml missing windows-latest runner"
