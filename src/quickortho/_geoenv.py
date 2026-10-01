"""GDAL·PROJ 데이터 경로 고정.

Windows에 PostGIS, QGIS(OSGeo4W) 같은 GIS 프로그램을 설치하면 시스템 환경변수 PROJ_LIB·PROJ_DATA·GDAL_DATA가
그 프로그램의 데이터 폴더를 가리키게 된다. rasterio는 이 환경변수를 자기 패키지에 들어 있는 데이터보다 먼저 쓰므로,
버전이 다른 proj.db를 읽다가 정사 모자이크 단계에서 다음 오류로 실패한다.

    The EPSG code is unknown. PROJ: proj_create_from_database: ...\\proj.db contains
    DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 5 is expected. It comes from another PROJ installation.

SDK는 ``import quickortho`` 시점(rasterio를 불러오기 전)에 이 환경변수를 rasterio 패키지(데스크톱 앱 설치본은
PyInstaller 번들)에 함께 들어 있는 데이터 폴더로 바꿔, 라이브러리와 짝이 맞는 데이터만 쓰게 한다.
이 파이썬 프로세스 안에서만 바뀌고 시스템 설정은 그대로다.

- pyproj는 자기 패키지 데이터를 환경변수보다 먼저 쓰므로 영향이 없다
- GDAL_DRIVER_PATH(다른 GDAL 버전의 플러그인 폴더)도 지운다. 버전이 다른 플러그인을 불러오면 프로세스가 죽을 수 있다
- 패키지 안에 데이터가 없는 설치(conda 등)는 그 환경의 설정을 그대로 둔다
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

#: 이 프로세스에서 바꾼 환경변수와 원래 값 (진단용, ``quickortho version``·``doctor``에 표시)
overridden: dict[str, str] = {}


def _same(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


def isolate() -> dict[str, str]:
    """rasterio 패키지에 들어 있는 PROJ·GDAL 데이터를 쓰도록 환경변수를 고정한다. 반환: 바꾼 변수와 원래 값."""
    try:
        spec = importlib.util.find_spec("rasterio")
    except (ImportError, ValueError):
        return {}
    if spec is None or not spec.origin:
        return {}
    pkg = Path(spec.origin).parent
    proj = pkg / "proj_data"
    gdal = pkg / "gdal_data"
    changed: dict[str, str] = {}
    if (proj / "proj.db").is_file():
        for key in ("PROJ_DATA", "PROJ_LIB"):
            old = os.environ.get(key)
            if old and not _same(old, proj):
                changed[key] = old
            os.environ[key] = str(proj)
        old = os.environ.pop("GDAL_DRIVER_PATH", None)
        if old:
            changed["GDAL_DRIVER_PATH"] = old
    if gdal.is_dir():
        old = os.environ.get("GDAL_DATA")
        if old and not _same(old, gdal):
            changed["GDAL_DATA"] = old
        os.environ["GDAL_DATA"] = str(gdal)
    overridden.update(changed)
    return changed
