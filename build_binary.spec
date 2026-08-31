# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec para causadb.
Build: pyinstaller build_binary.spec
Output: dist/causadb  (Linux/macOS) o dist/causadb.exe (Windows)
"""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['causadb/cli/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'causadb.cli',
        'causadb',
        'causadb._event_types',
        'causadb._event_registry',
        'causadb._event_schema',
        'causadb._schema_validator',
        'causadb._replay_engine',
        'causadb._ledger_writer',
        'causadb._ledger_reader',
        'causadb._ledger_validator',
        'causadb._ledger_index',
        'causadb._workspace',
        'causadb._init',
        'causadb._config',
        'causadb._vigilante',
        'causadb._daemon',
        'causadb._ocb_manager',
        'causadb._snapshot',
        'causadb._blob_store',
        'causadb._checkpoint',
        'causadb._attribution',
        'causadb._redactor',
        'causadb._cost_rollup',
        'causadb._score',
        'causadb._distill',
        'causadb._skill_registry',
        'causadb._resume',
        'causadb._revive',
        'causadb._chronicle_index',
        'causadb._causal_attrib',
        'causadb._dag_cache',
        'causadb.compliance',
        'causadb.otel',
        'causadb.mcp',
        'causadb.mcp.server',
        'watchfiles',
        'pathspec',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy',
        'PIL',
        'cv2',
        'tensorflow',
        'torch',
        '_tkinter',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='causadb',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traitlets=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
