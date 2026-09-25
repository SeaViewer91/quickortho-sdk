# -*- mode: python ; coding: utf-8 -*-
# QuickOrtho 엔진 번들 (onedir). 실행: pyinstaller quickortho-engine.spec
# onedir를 쓰는 이유: onefile은 실행할 때마다 임시 폴더에 압축을 풀어 기동이 수 초 느려진다.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("pycolmap", "rasterio", "pyproj", "shapely"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("quickortho_engine")

a = Analysis(
    ["run_engine.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="quickortho-engine",
    console=True,  # stdout(JSON-lines) 통신을 위해 콘솔 앱으로 빌드 (창은 앱에서 숨김)
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="quickortho-engine")
