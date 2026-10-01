"""다른 GIS 프로그램(PostGIS 등)의 PROJ_LIB 설정이 SDK를 깨뜨리지 않는지 확인한다."""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

RASTERIO_DIR = Path(importlib.util.find_spec("rasterio").origin).parent
WHEEL_DATA = (RASTERIO_DIR / "proj_data" / "proj.db").is_file()


def _old_proj_dir(tmp_path: Path) -> Path:
    """레이아웃 버전이 낮은 proj.db (PostGIS 3.6 번들 PROJ와 같은 상황)."""
    d = tmp_path / "postgis" / "proj"
    d.mkdir(parents=True)
    shutil.copy(RASTERIO_DIR / "proj_data" / "proj.db", d / "proj.db")
    con = sqlite3.connect(d / "proj.db")
    con.execute("UPDATE metadata SET value='2' WHERE key='DATABASE.LAYOUT.VERSION.MINOR'")
    con.commit()
    con.close()
    return d


def _run(code: str, env: dict) -> subprocess.CompletedProcess:
    # 이 시험 프로세스가 SDK를 이미 불러왔다면 고정된 경로가 환경변수에 들어 있으므로 빼고 시작함
    base = {k: v for k, v in os.environ.items() if k not in ("PROJ_DATA", "PROJ_LIB", "GDAL_DATA", "GDAL_DRIVER_PATH")}
    return subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1])


@pytest.mark.skipif(not WHEEL_DATA, reason="rasterio 패키지에 PROJ 데이터가 없는 설치(conda 등)")
@pytest.mark.parametrize("var", ["PROJ_LIB", "PROJ_DATA"])
def test_foreign_proj_data_is_ignored(tmp_path, var):
    bad = _old_proj_dir(tmp_path)
    env = {var: str(bad), "GDAL_DATA": str(tmp_path / "nowhere"), "GDAL_DRIVER_PATH": str(tmp_path / "plugins")}
    # SDK 없이 rasterio만 쓰면 실패해야 시험이 의미가 있음
    plain = _run("from rasterio.crs import CRS; CRS.from_epsg(32652)", env)
    assert plain.returncode != 0 and "another PROJ installation" in plain.stderr
    # SDK를 먼저 불러오면 내장 데이터를 씀
    code = (
        "import quickortho, os; from quickortho import _geoenv as geoenv; from rasterio.crs import CRS; "
        "print(CRS.from_epsg(5186).to_epsg(), sorted(geoenv.overridden), 'GDAL_DRIVER_PATH' in os.environ)"
    )
    res = _run(code, env)
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    assert out.startswith("5186 ")
    assert repr(var) in out and "'GDAL_DATA'" in out
    assert out.endswith("False")  # 다른 GDAL의 플러그인 폴더 설정은 지움
