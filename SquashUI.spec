# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe.   Folder build (default):  pyinstaller --noconfirm SquashUI.spec
#                       Single .exe:             set SQUASHUI_ONEFILE=1 first
#
# A folder build starts fastest and trips antivirus scanners least; a single .exe unpacks itself to a temp folder
# on every start. Either way tools/ and the saved settings live next to the .exe (see squashui.constants.APP_DIR).
import os

from PyInstaller.utils.hooks import collect_all

onefile = os.environ.get("SQUASHUI_ONEFILE") == "1"

datas, binaries, hiddenimports = [], [], []
for package in ("py7zr", "libtorrent"):  # py7zr pulls in compression back-ends PyInstaller can't see; libtorrent is optional
    try:
        d, b, h = collect_all(package)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

# Qt parts this app never uses: they add hundreds of MB.
excludes = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
            "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
            "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuick3D", "PySide6.QtMultimedia",
            "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtBluetooth", "PySide6.QtNfc",
            "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtRemoteObjects"]

a = Analysis(["SquashUI.pyw"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)

if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="SquashUI", icon="squashui.ico", console=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SquashUI", icon="squashui.ico", console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="SquashUI", upx=False)
