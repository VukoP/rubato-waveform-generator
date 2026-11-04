# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec for both desktop apps. Run through build_macos.sh / build_windows.ps1, which first
# write build/THIRD_PARTY_LICENSES.txt. One folder (onedir), no console window, FFmpeg not bundled.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent                                     # noqa: F821 - provided by PyInstaller
BUILD = ROOT / "build"
BRAND = ROOT / "assets" / "brand"
APP_NAME = "Rubato Waveform Generator"
VERSION = re.search(r'^__version__ = "(.+)"$', (ROOT / "rubato_waveform" / "__init__.py").read_text(), re.M).group(1)
COPYRIGHT = "Copyright 2026 VukoP"

datas = [
    (str(ROOT / "rubato_waveform" / "ui"), "rubato_waveform/ui"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "NOTICE"), "."),
    (str(BUILD / "THIRD_PARTY_LICENSES.txt"), "."),
] + collect_data_files("webview")

hiddenimports = []
if sys.platform == "win32":
    # pywebview picks its backend at run time; the hook already collects the WebView2 DLLs.
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]

excludes = ["tkinter", "pytest", "playwright", "PyInstaller", "ruff", "piplicenses", "prettytable", "yaml", "fontTools", "brotli"]

a = Analysis(                                                    # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)                                                # noqa: F821

version_file = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    numbers = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3]) + (0,) * 4
    numbers = numbers[:4]
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "VukoP"),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", APP_NAME),
                StringStruct("LegalCopyright", COPYRIGHT),
                StringStruct("OriginalFilename", APP_NAME + ".exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x409, 1200])]),
        ])
    BUILD.mkdir(exist_ok=True)
    version_file = BUILD / "version_info.txt"                    # generated each build: never a stale version
    version_file.write_text(str(info), encoding="utf-8")

exe = EXE(                                                       # noqa: F821
    pyz, a.scripts,
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=str(BRAND / ("icon.ico" if sys.platform == "win32" else "icon.icns")),
    version=str(version_file) if version_file else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)         # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(                                                # noqa: F821
        coll,
        name=APP_NAME + ".app",
        icon=str(BRAND / "icon.icns"),
        bundle_identifier="com.rubatostudio.waveform",
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": COPYRIGHT,
            "LSMinimumSystemVersion": "14.0",
            "NSRequiresAquaSystemAppearance": False,
        },
    )
