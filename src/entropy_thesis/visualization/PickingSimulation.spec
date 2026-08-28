# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for PickingSimulation.exe.

Place this file beside picking_animation_desktop.py under:
    src/entropy_thesis/visualization/

Build from Windows with:
    python -m PyInstaller --noconfirm --clean PickingSimulation.spec
"""

from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
SCRIPT = SPEC_DIR / "picking_animation_desktop.py"

if not SCRIPT.exists():
    raise FileNotFoundError(f"Desktop source not found: {SCRIPT}")

# Runtime warehouse data / monthly JSON are deliberately NOT embedded in the EXE.
# They can be hundreds of MB and change independently of the viewer.  The EXE
# automatically looks one directory above dist/, which is the normal project root.

a = Analysis(
    [str(SCRIPT)],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The standalone viewer does not need the thesis simulation stack.
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "networkx",
        "simpy",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PickingSimulation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
