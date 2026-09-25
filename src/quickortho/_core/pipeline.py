"""fast ortho 파이프라인: 스캔 → SfM → 좌표 정렬 → 간이 DSM → 정사투영 → COG."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import psutil
import pycolmap
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

from .._version import __version__
from ..errors import InputError, ProcessingError
from ..options import OrthoOptions
from .ortho import build_views, finalize_cog, native_gsd, render_orthomosaic
from .project import Project, to_absolute
from .protocol import Emitter
from .scan import scan_folder
from .sfm import run_sfm
from .surface import build_dsm, georeference, remove_z_outliers, sparse_points


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


REPORT_VERSION = 1
PRODUCT_FILES = ("orthomosaic.tif", "dsm.tif", "preview.png")


def run_align(
    folder: Path, out_dir: Path, opts: OrthoOptions, out: Emitter
) -> tuple[dict, pycolmap.Reconstruction, int]:
    """스캔 → SfM → GPS 좌표 정렬. 결과를 out_dir/project/에 저장한다.

    반환: (정렬 보고서, 투영 좌표계(절대값) 재구성, EPSG)
    이전 정사 모자이크 결과물은 새 정렬과 맞지 않으므로 지운다.
    """
    t_start = time.perf_counter()
    folder = Path(folder)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    timings: dict[str, float] = {}
    warnings: list[str] = []

    with PeakMemory() as mem:
        try:
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
            if not scan["images"]:
                raise InputError(f"JPG 영상이 없음: {folder}", "no_images")
            if len(images) < 3:
                raise InputError("처리 가능한 영상이 3장 미만임", "too_few_images")
            if len(gps) < 3:
                raise InputError("GPS 정보가 있는 영상이 3장 미만이라 좌표를 부여할 수 없음", "too_few_gps")
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

            # 보정용 프로젝트 저장 (지역 좌표계)
            proj = Project(out_dir)
            proj.save_base(rec, geo.projector.epsg, geo.origin)
            proj.save_meta(folder, gps, __version__, render={})
        finally:
            if not opts.keep_work:
                shutil.rmtree(work, ignore_errors=True)

    for name in PRODUCT_FILES + ("report.json",):
        (out_dir / name).unlink(missing_ok=True)
    timings["total_s"] = time.perf_counter() - t_start
    align = {
        "report_version": REPORT_VERSION,
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
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "peak_memory_mb": round(mem.peak / 1024 / 1024, 1),
        "warnings": warnings,
    }
    proj.save_align_report(align)
    (out_dir / "report.json").write_text(json.dumps(align, ensure_ascii=False, indent=2), encoding="utf-8")
    return align, rec, geo.projector.epsg


def run_render(
    out_dir: Path,
    opts: OrthoOptions,
    out: Emitter,
    rec_abs: pycolmap.Reconstruction | None = None,
    epsg: int | None = None,
) -> dict:
    """현재 재구성(보정 결과가 있으면 보정 결과)으로 DSM·정사 모자이크를 만들고 report.json을 갱신한다."""
    t_start = time.perf_counter()
    proj = Project(out_dir)
    proj.require()
    image_dir = proj.image_dir
    if not image_dir.is_dir():
        raise InputError(f"원본 영상 폴더를 찾을 수 없음: {image_dir}", "image_dir_missing")
    if rec_abs is None:
        rec, frame, _ = proj.load_current()
        rec_abs, epsg = to_absolute(rec, frame), frame.epsg
    assert epsg is not None
    timings: dict[str, float] = {}
    with PeakMemory() as mem:
        products = render_products(rec_abs, image_dir, proj.ortho_dir, epsg, opts, out, timings)
    proj.update_render({"gsd_m": opts.gsd_m, "gsd_scale": opts.gsd_scale, "cache_budget_mb": opts.cache_budget_mb})
    timings["render_total_s"] = time.perf_counter() - t_start
    return compose_report(proj, products, timings, mem.peak)


def compose_report(proj: Project, products: dict, render_timings: dict[str, float], render_peak: int) -> dict:
    """정렬 보고서 + 마지막 정사 모자이크 결과 + (보정했으면) 보정 결과를 합쳐 report.json을 쓴다.

    timings_s: 정렬 단계 시간 + 이번 정사 모자이크 단계 시간. total_s는 둘의 합이다.
    peak_memory_mb: 정렬 단계와 이번 정사 모자이크 단계 중 큰 값.
    """
    align = proj.align_report()
    report = dict(align)
    report.update(products)
    report["report_version"] = REPORT_VERSION
    report["engine_version"] = __version__
    t = {k: v for k, v in align.get("timings_s", {}).items() if k != "total_s"}
    align_total = float(align.get("timings_s", {}).get("total_s", 0.0))
    render_total = float(render_timings.get("render_total_s", 0.0))
    t.update({k: round(v, 2) for k, v in render_timings.items() if k != "render_total_s"})
    t["total_s"] = round(align_total + render_total, 2)
    report["timings_s"] = t
    report["peak_memory_mb"] = max(float(align.get("peak_memory_mb", 0.0)), round(render_peak / 1024 / 1024, 1))
    warnings = list(align.get("warnings", []))
    if proj.has_refined():
        part = proj.refine_report()
        if not part:  # v0.2.0 엔진이 보정한 프로젝트: 기존 report.json에서 보정 결과를 가져온다
            old_p = proj.ortho_dir / "report.json"
            old = json.loads(old_p.read_text(encoding="utf-8")) if old_p.exists() else {}
            if "refine" in old:
                part = {"georef": old.get("georef"), "refine": old["refine"],
                        "warnings": old["refine"].get("warnings", [])}
        if part.get("georef"):
            report["georef"] = part["georef"]
        if part.get("refine"):
            report["refine"] = part["refine"]
        warnings += part.get("warnings", [])
    else:
        report.pop("refine", None)
    report["warnings"] = warnings
    (proj.ortho_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def run_ortho(folder: Path, out_dir: Path, opts: OrthoOptions, out: Emitter) -> dict:
    """정렬 + 정사 모자이크 (v0.2.0 엔진의 ortho 명령과 같은 결과)."""
    _, rec, epsg = run_align(folder, out_dir, opts, out)
    return run_render(out_dir, opts, out, rec_abs=rec, epsg=epsg)


def render_products(
    rec: pycolmap.Reconstruction,
    image_dir: Path,
    out_dir: Path,
    epsg: int,
    opts: OrthoOptions,
    out: Emitter,
    timings: dict[str, float],
) -> dict:
    """투영 좌표(절대값) 재구성으로 간이 DSM, 정사 모자이크(COG), 미리보기를 만든다.

    반환: 보고서의 dsm, ortho, outputs, preview_corners_lonlat 항목.
    """
    crs = CRS.from_epsg(epsg)
    t0 = time.perf_counter()
    out.stage("dsm", "희소 점군으로 간이 DSM 생성")
    pts = remove_z_outliers(sparse_points(rec))
    if len(pts) < 10:
        raise ProcessingError("DSM 생성 실패: 유효한 3D 점이 너무 적음", "dsm_failed")
    ground_z = float(np.median(pts[:, 2]))
    views = build_views(rec, ground_z)
    src_gsd = native_gsd(views, ground_z)
    gsd = opts.gsd_m or src_gsd * opts.gsd_scale
    vb = np.array([v.bbox for v in views])
    bounds = _snap_bounds((vb[:, 0].min(), vb[:, 1].min(), vb[:, 2].max(), vb[:, 3].max()), gsd)
    area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
    dsm_res = max(0.5, 1.5 * math.sqrt(area / len(pts)))
    dsm = build_dsm(pts, bounds, dsm_res)
    out.progress("dsm", 1, 1)  # 중단 확인 지점
    # 결과물은 임시 파일에 쓰고 마지막에 교체한다. 중단·실패 시 이전 결과물이 섞이지 않게 하기 위해서다.
    dsm_tmp = out_dir / "dsm.tmp.tif"
    tmp = out_dir / "orthomosaic.tmp.tif"
    cog_tmp = out_dir / "orthomosaic.cog.tmp.tif"
    final = out_dir / "orthomosaic.tif"
    try:
        _write_dsm(dsm, dsm_tmp, crs)
        timings["dsm_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("ortho", f"정사 모자이크 생성 (GSD {gsd * 100:.1f} cm)")
        ortho_stats = render_orthomosaic(
            views, dsm, image_dir, tmp, crs, gsd, src_gsd, bounds, out,
            cache_budget_mb=opts.cache_budget_mb,
        )
        timings["ortho_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("finalize", "COG 변환·미리보기 생성")
        finalize_cog(tmp, cog_tmp)
        out.progress("finalize", 1, 1)  # 중단 확인 지점 (결과물 교체 전)
        os.replace(cog_tmp, final)
        os.replace(dsm_tmp, out_dir / "dsm.tif")
    finally:
        for f in (dsm_tmp, tmp, cog_tmp):
            f.unlink(missing_ok=True)
    _write_preview(final, out_dir / "preview.png")
    corners = _corners_lonlat(final)
    timings["finalize_s"] = time.perf_counter() - t0
    return {
        "dsm": {"resolution_m": dsm_res, "num_points": dsm.num_points},
        "ortho": {**ortho_stats, "source_gsd_m": src_gsd},
        "outputs": {
            "orthomosaic": str(final),
            "dsm": str(out_dir / "dsm.tif"),
            "preview": str(out_dir / "preview.png"),
        },
        "preview_corners_lonlat": corners,
    }


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


def _corners_lonlat(path: Path) -> list[list[float]]:
    """래스터 네 모서리(좌상, 우상, 우하, 좌하)의 경위도. 앱 지도 오버레이용."""
    from pyproj import Transformer

    with rasterio.open(path) as ds:
        b = ds.bounds
        tr = Transformer.from_crs(ds.crs.to_epsg(), 4326, always_xy=True)
    xs = [b.left, b.right, b.right, b.left]
    ys = [b.top, b.top, b.bottom, b.bottom]
    lon, lat = tr.transform(xs, ys)
    return [[float(a), float(c)] for a, c in zip(lon, lat)]


def _write_preview(path: Path, png_path: Path, max_size: int = 2048) -> None:
    with rasterio.open(path) as ds:
        scale = min(1.0, max_size / max(ds.width, ds.height))
        w, h = max(1, int(ds.width * scale)), max(1, int(ds.height * scale))
        data = ds.read(out_shape=(4, h, w), resampling=rasterio.enums.Resampling.average)
    from PIL import Image

    Image.fromarray(np.moveaxis(data, 0, -1), "RGBA").save(png_path)
