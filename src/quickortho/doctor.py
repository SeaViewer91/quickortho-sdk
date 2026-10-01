"""설치 환경 진단 (``quickortho doctor``, :func:`quickortho.doctor`)."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ._version import __version__


def _check(name: str, fn: Callable[[], str], checks: list[dict[str, Any]], warn_only: bool = False) -> None:
    try:
        detail = fn()
        checks.append({"name": name, "ok": True, "detail": detail})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": name, "ok": warn_only, "detail": f"{type(exc).__name__}: {exc}"})


def run_doctor(path: str | Path = ".") -> dict[str, Any]:
    """설치 환경을 점검해 결과를 돌려줌.

    Returns:
        ``{"ok": bool, "sdk": 버전, "checks": [{"name", "ok", "detail"}, ...]}``.
        ``ok``가 ``False``인 항목이 하나라도 있으면 전체 ``ok``가 ``False``.
    """
    path = Path(path).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    def py() -> str:
        v = sys.version_info
        if not ((3, 10) <= (v.major, v.minor) <= (3, 14)):
            raise RuntimeError(f"Python {platform.python_version()}: 지원 범위(3.10~3.14) 밖")
        return f"Python {platform.python_version()} ({platform.machine()}, {platform.system()} {platform.release()})"

    _check("Python", py, checks)

    def platform_ok() -> str:
        sysname, mach = platform.system(), platform.machine().lower()
        if sysname == "Darwin":
            if mach != "arm64":
                raise RuntimeError(f"macOS {mach}: Apple Silicon(arm64)만 지원")
            ver = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:1] or [0])
            if ver and ver[0] < 14:
                raise RuntimeError(f"macOS {platform.mac_ver()[0]}: 14 이상 필요")
        elif sysname == "Windows":
            if mach not in ("amd64", "x86_64"):
                raise RuntimeError(f"Windows {mach}: x64만 지원")
        elif sysname == "Linux":
            if mach not in ("x86_64", "amd64"):
                raise RuntimeError(f"Linux {mach}: x86_64만 지원")
        return f"{sysname} {mach}: 지원 환경"

    _check("운영체제", platform_ok, checks)

    def lib(mod: str, attr: str = "__version__") -> Callable[[], str]:
        def f() -> str:
            m = __import__(mod)
            return f"{mod} {getattr(m, attr, '?')}"
        return f

    for mod in ("numpy", "scipy", "PIL", "cv2", "shapely", "pyproj", "psutil"):
        _check(f"라이브러리 {mod}", lib(mod), checks)

    def colmap() -> str:
        import pycolmap

        cuda = getattr(pycolmap, "has_cuda", False)
        return f"pycolmap {pycolmap.__version__} (CUDA {'있음' if cuda else '없음, CPU로 처리'})"

    _check("라이브러리 pycolmap", colmap, checks)

    def gdal() -> str:
        import rasterio

        drivers = rasterio.drivers.raster_driver_extensions()
        return f"rasterio {rasterio.__version__}, GDAL {rasterio.__gdal_version__}, 드라이버 {len(set(drivers.values()))}종"

    _check("라이브러리 rasterio/GDAL", gdal, checks)

    def proj() -> str:
        import pyproj
        from pyproj import Transformer

        e, n = Transformer.from_crs(4326, 5186, always_xy=True).transform(127.0, 38.0)
        if abs(e - 200000) > 1 or abs(n - 600000) > 1:
            raise RuntimeError(f"좌표 변환 결과가 이상함: {e:.1f}, {n:.1f}")
        return f"PROJ {pyproj.proj_version_str}, EPSG:5186 변환 정상"

    _check("좌표 변환", proj, checks)

    def raster_crs() -> str:
        # 정사 모자이크 저장(rasterio)은 pyproj와 다른 PROJ를 씀. PostGIS 등의 PROJ_LIB가 섞이면 여기서만 실패함
        from rasterio.crs import CRS

        from . import _geoenv

        if CRS.from_epsg(5186).to_epsg() != 5186:
            raise RuntimeError("rasterio에서 EPSG:5186을 읽지 못함")
        note = ""
        if _geoenv.overridden:
            note = " (다른 프로그램의 설정 " + ", ".join(sorted(_geoenv.overridden)) + "은 무시하고 내장 데이터 사용)"
        return "rasterio 좌표계 정상" + note

    _check("좌표계 (GeoTIFF 저장)", raster_crs, checks)

    def resources() -> str:
        import psutil

        mem = psutil.virtual_memory()
        total_gb = mem.total / 1024**3
        detail = f"CPU {os.cpu_count()}코어, 메모리 {total_gb:.1f} GB (사용 가능 {mem.available / 1024**3:.1f} GB)"
        if total_gb < 7.5:
            raise RuntimeError(detail + " — 8GB 미만이라 큰 작업에서 메모리가 부족할 수 있음")
        return detail

    _check("CPU·메모리", resources, checks, warn_only=True)

    def disk() -> str:
        target = path if path.exists() else path.parent
        free = shutil.disk_usage(target).free / 1024**3
        detail = f"{target}: 여유 {free:.1f} GB"
        if free < 5:
            raise RuntimeError(detail + " — 5GB 미만")
        return detail

    _check("디스크", disk, checks)

    def writable() -> str:
        target = path if path.is_dir() else path.parent
        with tempfile.NamedTemporaryFile(dir=target, prefix=".quickortho_doctor_", delete=True) as f:
            f.write(b"ok")
        return f"{target}: 쓰기 가능"

    _check("쓰기 권한", writable, checks)

    def functional() -> str:
        """작은 합성 영상으로 GeoTIFF(COG) 쓰기·읽기, SQLite, 특징점 추출을 시험."""
        import numpy as np
        import pycolmap
        import rasterio
        from rasterio.transform import from_origin

        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="quickortho_doctor_") as d:
            d = Path(d)
            tif = d / "t.tif"
            with rasterio.open(tif, "w", driver="GTiff", width=64, height=64, count=1, dtype="uint8",
                               crs="EPSG:5186", transform=from_origin(0, 64, 1, 1)) as ds:
                ds.write(np.arange(64 * 64, dtype=np.uint8).reshape(1, 64, 64))
            rasterio.shutil.copy(tif, d / "c.tif", driver="COG")
            with rasterio.open(d / "c.tif") as ds:
                assert ds.read(1)[1, 1] == 65
            import sqlite3

            con = sqlite3.connect(f"{(d / 'x.db').resolve().as_uri()}?mode=rwc", uri=True)
            con.execute("create table t(a)")
            con.close()
            from PIL import Image

            rng = np.random.default_rng(0)
            img = (rng.random((240, 320, 3)) * 255).astype(np.uint8)
            imgs = d / "imgs"
            imgs.mkdir()
            Image.fromarray(img).save(imgs / "a.jpg", quality=95)
            opts = pycolmap.FeatureExtractionOptions()
            opts.num_threads = 1
            pycolmap.extract_features(d / "db.db", imgs, extraction_options=opts, device=pycolmap.Device.cpu)
            db = pycolmap.Database.open(d / "db.db")
            n = db.num_keypoints()
            db.close()
            if n == 0:
                raise RuntimeError("특징점이 추출되지 않음")
        return f"COG 쓰기·읽기, SQLite, 특징점 추출({n}개) 정상 ({time.perf_counter() - t0:.1f}초)"

    _check("기능 시험", functional, checks)

    return {"ok": all(c["ok"] for c in checks), "sdk": __version__, "checks": checks}


__all__ = ["run_doctor"]
