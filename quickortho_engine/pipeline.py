"""fast ortho 파이프라인: 스캔 → SfM → 좌표 정렬 → 간이 DSM → 정사투영 → COG."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
import rasterio
from rasterio.transform import from_origin

from . import __version__
from .ortho import build_views, finalize_cog, native_gsd, render_orthomosaic
from .protocol import Emitter
from .scan import scan_folder
from .sfm import SfmOptions, run_sfm
from .surface import build_dsm, georeference, remove_z_outliers, sparse_points


@dataclass
class OrthoOptions:
    gsd_m: float | None = None  # 지정하지 않으면 원본 GSD × gsd_scale
    gsd_scale: float = 2.0
    keep_work: bool = False
    cache_budget_mb: int = 600
    sfm: SfmOptions = field(default_factory=SfmOptions)


class PeakMemory:
    """프로세스 RSS를 주기적으로 측정해 최대값을 기록한다 (OS 공통)."""

    def __init__(self, interval: float = 0.5) -> None:
        self._proc = psutil.Process(os.getpid())
        self._interval = interval
        self._stop = threading.Event()
        self.peak = 0
        self._th = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self._proc.memory_info().rss)
            self._stop.wait(self._interval)

    def __enter__(self) -> "PeakMemory":
        self._th.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._th.join()
        self.peak = max(self.peak, self._proc.memory_info().rss)


def run_ortho(folder: Path, out_dir: Path, opts: OrthoOptions, out: Emitter) -> dict:
    t_start = time.perf_counter()
    folder = Path(folder)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    timings: dict[str, float] = {}
    warnings: list[str] = []

    with PeakMemory() as mem:
        # 0) 스캔
        t0 = time.perf_counter()
        scan = scan_folder(folder, emitter=out)
        timings["scan_s"] = time.perf_counter() - t0
        images = [im for im in scan["images"] if im["selected"]]
        gps = {
            im["file"]: (im["lon"], im["lat"], _alt(im))
            for im in images
            if im["lat"] is not None and im["lon"] is not None
        }
        if len(images) < 3:
            raise RuntimeError("처리 가능한 영상이 3장 미만임")
        if len(gps) < 3:
            raise RuntimeError("GPS 정보가 있는 영상이 3장 미만이라 좌표를 부여할 수 없음")
        if scan["summary"]["rolling_shutter_warning"]:
            warnings.append("롤링 셔터 카메라(Mavic 2 등) 영상이 포함됨: 고속 비행 시 왜곡 가능")
        if scan["summary"]["oblique"]:
            warnings.append(f"경사 촬영 영상 {scan['summary']['oblique']}장 포함")

        # 1) SfM
        rec, sfm_stats = run_sfm(
            folder, [im["file"] for im in images], work, has_gps=len(gps) >= 3, opts=opts.sfm, out=out
        )
        timings.update(
            features_s=sfm_stats["time_features_s"],
            matching_s=sfm_stats["time_matching_s"],
            mapping_s=sfm_stats["time_mapping_s"],
        )
        if sfm_stats["num_registered"] < len(images):
            warnings.append(
                f"{len(images) - sfm_stats['num_registered']}장은 정합되지 않아 모자이크에서 제외됨"
            )

        # 2) 좌표 정렬
        t0 = time.perf_counter()
        out.stage("georef", "GPS로 좌표 정렬 (UTM)")
        geo = georeference(rec, gps)
        out.log(f"좌표계 EPSG:{geo.projector.epsg}, GPS 잔차(RMS, 수평) {geo.gps_residual_m:.2f} m")
        timings["georef_s"] = time.perf_counter() - t0

        # 3) 간이 DSM
        t0 = time.perf_counter()
        out.stage("dsm", "희소 점군으로 간이 DSM 생성")
        pts = remove_z_outliers(sparse_points(rec))
        if len(pts) < 10:
            raise RuntimeError("DSM 생성 실패: 유효한 3D 점이 너무 적음")
        ground_z = float(np.median(pts[:, 2]))
        views = build_views(rec, ground_z)
        src_gsd = native_gsd(views, ground_z)
        gsd = opts.gsd_m or src_gsd * opts.gsd_scale
        vb = np.array([v.bbox for v in views])
        bounds = _snap_bounds((vb[:, 0].min(), vb[:, 1].min(), vb[:, 2].max(), vb[:, 3].max()), gsd)
        area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        dsm_res = max(0.5, 1.5 * math.sqrt(area / len(pts)))
        dsm = build_dsm(pts, bounds, dsm_res)
        _write_dsm(dsm, out_dir / "dsm.tif", geo.projector.crs)
        timings["dsm_s"] = time.perf_counter() - t0

        # 4) 정사투영
        t0 = time.perf_counter()
        out.stage("ortho", f"정사 모자이크 생성 (GSD {gsd * 100:.1f} cm)")
        tmp = out_dir / "orthomosaic.tmp.tif"
        ortho_stats = render_orthomosaic(
            views, dsm, folder, tmp, geo.projector.crs, gsd, src_gsd, bounds, out,
            cache_budget_mb=opts.cache_budget_mb,
        )
        timings["ortho_s"] = time.perf_counter() - t0

        # 5) COG 변환과 미리보기
        t0 = time.perf_counter()
        out.stage("finalize", "COG 변환·미리보기 생성")
        final = out_dir / "orthomosaic.tif"
        finalize_cog(tmp, final)
        tmp.unlink(missing_ok=True)
        _write_preview(final, out_dir / "preview.png")
        timings["finalize_s"] = time.perf_counter() - t0

        if not opts.keep_work:
            shutil.rmtree(work, ignore_errors=True)

    timings["total_s"] = time.perf_counter() - t_start
    report = {
        "engine_version": __version__,
        "input": {
            "folder": str(folder),
            "total_files": scan["summary"]["total_files"],
            "selected": len(images),
            "with_gps": len(gps),
            "cameras": scan["cameras"],
        },
        "sfm": {k: v for k, v in sfm_stats.items() if not k.startswith("time_")},
        "georef": {
            "epsg": geo.projector.epsg,
            "gps_residual_rms_m": geo.gps_residual_m,
            "num_aligned": geo.num_aligned,
            "note": "절대 위치 정확도는 GNSS 수준(수 m)임. 정밀 위치가 필요하면 GCP 필요",
        },
        "dsm": {"resolution_m": dsm_res, "num_points": dsm.num_points},
        "ortho": {**ortho_stats, "source_gsd_m": src_gsd},
        "outputs": {
            "orthomosaic": str(final),
            "dsm": str(out_dir / "dsm.tif"),
            "preview": str(out_dir / "preview.png"),
        },
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "peak_memory_mb": round(mem.peak / 1024 / 1024, 1),
        "warnings": warnings,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _alt(im: dict) -> float:
    for key in ("abs_alt", "rel_alt"):
        if im.get(key) is not None:
            return float(im[key])
    return 0.0


def _snap_bounds(b: tuple[float, float, float, float], gsd: float) -> tuple[float, float, float, float]:
    x0 = math.floor(b[0] / gsd) * gsd
    y0 = math.floor(b[1] / gsd) * gsd
    x1 = math.ceil(b[2] / gsd) * gsd
    y1 = math.ceil(b[3] / gsd) * gsd
    return (x0, y0, x1, y1)


def _write_dsm(dsm, path: Path, crs) -> None:
    rows, cols = dsm.z.shape
    # 격자점이 화소 중심이 되도록 반 칸 이동
    transform = from_origin(dsm.x0 - dsm.res / 2, dsm.y0 + dsm.res / 2, dsm.res, dsm.res)
    with rasterio.open(
        path, "w", driver="GTiff", width=cols, height=rows, count=1, dtype="float32",
        crs=crs, transform=transform, compress="deflate",
    ) as ds:
        ds.write(dsm.z, 1)


def _write_preview(path: Path, png_path: Path, max_size: int = 2048) -> None:
    with rasterio.open(path) as ds:
        scale = min(1.0, max_size / max(ds.width, ds.height))
        w, h = max(1, int(ds.width * scale)), max(1, int(ds.height * scale))
        data = ds.read(out_shape=(4, h, w), resampling=rasterio.enums.Resampling.average)
    from PIL import Image

    Image.fromarray(np.moveaxis(data, 0, -1), "RGBA").save(png_path)
