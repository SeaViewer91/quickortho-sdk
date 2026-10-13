"""fast ortho 파이프라인: 스캔 → SfM → 좌표 정렬 → 간이 DSM → 정사투영 → COG."""

from __future__ import annotations

import json
import math
import re
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
from . import multispectral as msp
from . import water as wat
from .color import estimate_gains
from .ms_ortho import (
    band_gsd,
    build_band_sources,
    normalize_captures,
    refine_rig,
    render_ms_orthomosaic,
    write_rgb_preview,
)
from .indices import legend as index_legend
from .indices import build_indices, normalize_selection, write_indices
from .ortho import build_views, finalize_cog, native_gsd, render_orthomosaic
from .geo import Projector
from .project import Project, to_absolute
from .protocol import Emitter
from .scan import scan_folder
from .sfm import run_sfm
from .surface import build_dsm, external_dsm, georeference, plane_dsm, remove_z_outliers, sparse_points


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
PRODUCT_FILES = ("orthomosaic.tif", "dsm.tif", "preview.png", "indices.tif", "ndvi.png")


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

    msref = Project(out_dir).dir / "msref"
    msref_new = Project(out_dir).dir / "msref.new"
    pdir = Project(out_dir).dir
    water_info = None
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
                raise InputError(f"영상이 없음 (JPG 또는 다중분광 TIFF): {folder}", "no_images")
            # 다중분광: 기준 밴드를 8비트 영상으로 바꿔 SfM에 쓰고(project/msref), 정사 단계에서 모든 밴드를 투영함
            # 기준 밴드 영상은 임시 폴더(msref.new)에 만들고 정렬이 끝난 뒤 msref와 바꾼다.
            # 새 정렬이 중단·실패해도 이전 다중분광 워크스페이스는 그대로 쓸 수 있게 하기 위해서다
            ms_ds, image_dir, sfm_kw = None, folder, {}
            shutil.rmtree(msref_new, ignore_errors=True)
            _clear_water(pdir, new_only=True)
            if scan.get("multispectral"):
                ms_ds = msp.scan_multispectral(folder)
                ms_info = scan["multispectral"]
                caps = [c for c in ms_ds.flight_captures if ms_ds.ref(c).file in gps]
                if len(caps) < len(ms_ds.flight_captures):
                    warnings.append(f"GPS가 없는 캡처 {len(ms_ds.flight_captures) - len(caps)}개는 제외함")
                if ms_info["incomplete_captures"]:
                    warnings.append(f"밴드가 빠진 캡처 {ms_info['incomplete_captures']}개는 제외함")
                n_jpg = sum(1 for im in scan["images"] if im.get("band") is None)
                if n_jpg:
                    warnings.append(f"같은 폴더의 JPG {n_jpg}장은 쓰지 않음 (다중분광 영상만 처리함)")
                if len(caps) < 3:
                    raise InputError("GPS가 있는 다중분광 캡처가 3개 미만임", "too_few_gps")
                _check_radiometry_available(opts.radiometry, ms_info)
                out.stage("scan", f"다중분광 기준 밴드 영상 준비 ({len(caps)}장)")
                names = msp.write_ref_proxies(
                    ms_ds, caps, msref_new, progress=lambda i, n: out.progress("scan", i, n)
                )
                gps = {msp.proxy_name(ms_ds.ref(c).file): gps[ms_ds.ref(c).file] for c in caps}
                images = [{"file": n} for n in names]
                image_dir = msref_new
                ref0 = ms_ds.ref(caps[0])
                sfm_kw = dict(camera_model="FULL_OPENCV", camera_params=msp.camera_params_string(ref0),
                              fixed_intrinsics=True)
            if len(images) < 3:
                raise InputError("처리 가능한 영상이 3장 미만임", "too_few_images")
            if len(gps) < 3:
                raise InputError("GPS 정보가 있는 영상이 3장 미만이라 좌표를 부여할 수 없음", "too_few_gps")
            if scan["summary"]["rolling_shutter_warning"]:
                warnings.append("롤링 셔터 카메라(Mavic 2 등) 영상이 포함됨: 고속 비행 시 왜곡 가능")
            if scan["summary"]["oblique"]:
                warnings.append(f"경사 촬영 영상 {scan['summary']['oblique']}장 포함")

            # 수면 판별: 넓은 수면이 있으면 수면 안쪽의 특징점을 빼고(정합 안정), 정사 단계에서 DSM 수면을 평면으로 덮음
            if ms_ds is not None and opts.water == "on":
                warnings.append(MS_WATER_WARNING)
            if ms_ds is None and opts.water != "off":
                water_info, mask_dir = _water_for_align(pdir, image_dir, images, opts, out, timings, work)
                if mask_dir is not None:
                    sfm_kw["mask_dir"] = mask_dir

            # 1) SfM
            rec, sfm_stats = run_sfm(
                image_dir, [im["file"] for im in images], work, has_gps=len(gps) >= 3, opts=opts.sfm, out=out,
                **sfm_kw,
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
            geo = georeference(rec, gps, epsg=opts.epsg)
            out.log(f"좌표계 EPSG:{geo.projector.epsg}, GPS 잔차(RMS, 수평) {geo.gps_residual_m:.2f} m")
            timings["georef_s"] = time.perf_counter() - t0

            # 보정용 프로젝트 저장 (지역 좌표계)
            proj = Project(out_dir)
            shutil.rmtree(msref, ignore_errors=True)  # 이전 정렬의 기준 밴드 영상 (새 정렬과 맞지 않음)
            if ms_ds is not None:
                os.replace(msref_new, msref)
                image_dir = msref
            proj.save_base(rec, geo.projector.epsg, geo.origin)
            proj.save_meta(image_dir, gps, __version__, render={}, ms_source=folder if ms_ds else None)
            # 수면 판별 결과도 정렬이 끝난 뒤 바꾼다 (새 정렬이 중단·실패하면 이전 판별 결과가 남음)
            w = _commit_water(pdir, water_info)
            if w:
                warnings.append(w)
        finally:
            _clear_water(pdir, new_only=True)
            shutil.rmtree(msref_new, ignore_errors=True)
            if not opts.keep_work:
                shutil.rmtree(work, ignore_errors=True)

    for name in PRODUCT_FILES + ("report.json",):
        (out_dir / name).unlink(missing_ok=True)
    _remove_indices(out_dir)
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
            **({"multispectral": scan["multispectral"]} if scan.get("multispectral") else {}),
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
    if water_info is not None:
        align["water"] = water_info
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
    ms_ds = ms_dataset_from_meta(proj.meta())
    timings: dict[str, float] = {}
    with PeakMemory() as mem:
        products = render_products(rec_abs, image_dir, proj.ortho_dir, epsg, opts, out, timings, ms_ds=ms_ds)
    proj.update_render(opts.render_dict(), __version__)
    timings["render_total_s"] = time.perf_counter() - t_start
    return compose_report(proj, products, timings, mem.peak)


# ── 수면 처리 ───────────────────────────────────────────────────────
# 판별 결과: project/water/<영상 경로>.png (0/100/200), 요약: project/water.json {"source": "auto"|"user", ...}
# 새 정렬은 water.new/·water.new.json에 만들고 정렬이 끝나면 바꾼다. 사용자 마스크(source user)는 정렬이 덮어쓰지 않는다.


MS_WATER_WARNING = "다중분광 영상에는 아직 수면 처리를 적용하지 않음"


def _water_paths(pdir: Path, new: bool = False) -> tuple[Path, Path]:
    return (pdir / "water.new", pdir / "water.new.json") if new else (pdir / "water", pdir / "water.json")


def _clear_water(pdir: Path, new_only: bool = False) -> None:
    """임시 판별 결과(water.new 등)를 지운다. new_only=False면 저장된 판별 결과(사용자 마스크 포함)도 지운다."""
    pairs = [_water_paths(pdir, True)] + ([] if new_only else [_water_paths(pdir)])
    for d, j in pairs:
        for x in (d, d.with_name(d.name + ".tmp"), d.with_name(d.name + ".old")):
            shutil.rmtree(x, ignore_errors=True)
        for x in (j, j.with_name(j.name + ".tmp")):
            x.unlink(missing_ok=True)
    if not new_only:
        shutil.rmtree(pdir / "water.old", ignore_errors=True)


def _user_water(pdir: Path) -> bool:
    """사용자 마스크가 있는지 (요약의 source가 user이고 마스크 파일이 실제로 있음)."""
    d, j = _water_paths(pdir)
    try:
        user = json.loads(j.read_text(encoding="utf-8")).get("source") == "user"
    except (OSError, ValueError):
        return False
    return user and d.is_dir() and any(d.rglob("*.png"))


def _water_summary(mode: str, active: bool, det: "wat.Detection", source: str) -> dict:
    return {"mode": mode, "active": active, "source": source, "image_fraction": round(det.fraction, 4),
            "images_with_water": det.images_with_water, **det.stats}


def _water_for_align(pdir, image_dir, images, opts, out, timings, work) -> tuple[dict, Path | None]:
    """정렬 전 수면 판별. 반환: (보고서 water 항목, 특징점 제외 마스크 폴더 또는 None)."""
    t0 = time.perf_counter()
    names = [im["file"] for im in images]
    saved = wat.load_detection(*_water_paths(pdir)) if _user_water(pdir) else None
    det = None
    if saved is not None:  # 사용자가 넣은 마스크 (Project.set_water_masks). 이번 영상에 해당하는 것만 씀
        mine = {k: v for k, v in saved[0].masks.items() if k in set(names)}
        if mine:
            det, source = wat.summarize(mine), "user"
            out.log(f"사용자 수면 마스크 {len(mine)}장을 씀")
        else:
            out.log("사용자 수면 마스크가 이번 영상과 이름이 맞지 않아 자동으로 판별함", "warn")
    if det is None:
        out.stage("water", "수면 판별")
        det = wat.detect(image_dir, names, out, opts.sfm.num_threads)
        wat.save_detection(det, *_water_paths(pdir, new=True), source="auto")
        source = "auto"
    active = wat.decide(opts.water, det)
    info = _water_summary(opts.water, active, det, source)
    mask_dir = None
    if active:
        sizes = {im["file"]: (im["width"], im["height"]) for im in images if im.get("width")}
        n = wat.write_feature_masks(det.masks, sizes, work / "masks")
        info["feature_masked_images"] = n
        mask_dir = work / "masks" if n else None
    else:
        info["feature_masked_images"] = 0
    out.log(f"수면 비율 {det.fraction * 100:.1f}% (수면이 보이는 영상 {det.images_with_water}장) → "
            + ("수면 처리 켬" if active else "수면 처리 안 함"))
    timings["water_s"] = time.perf_counter() - t0
    return info, mask_dir


def _commit_water(pdir: Path, water_info: dict | None) -> str | None:
    """정렬이 끝난 뒤 새 판별 결과로 바꾼다. 판별하지 않았으면(off) 이전 자동 판별 결과는 지운다 (사용자 마스크는 둠).

    반환: 실패했을 때 경고 문장 (정렬 결과는 살림. 판별 결과는 정사 단계에서 필요하면 다시 만듦).
    """
    if _user_water(pdir):
        return None
    new_d, new_j = _water_paths(pdir, new=True)
    d, j = _water_paths(pdir)
    old = pdir / "water.old"
    try:
        shutil.rmtree(old, ignore_errors=True)
        if d.exists():
            os.replace(d, old)  # 잠긴 파일이 있으면 여기서 실패하고 이전 결과가 그대로 남음
        j.unlink(missing_ok=True)
        if water_info is not None and new_d.is_dir():
            os.replace(new_d, d)
            os.replace(new_j, j)
        shutil.rmtree(old, ignore_errors=True)
    except OSError as e:
        return f"수면 판별 결과를 저장하지 못함 ({e}). 정사 단계에서 수면 처리를 켜면 다시 판별함"
    return None


def _water_for_render(pdir, opts, image_dir, names, kind, out, timings) -> tuple[dict | None, dict]:
    """정사 단계의 수면 입력. 반환: (DSM에 쓸 영상별 마스크 또는 None, 보고서 water 항목).

    저장된 판별 결과가 있으면 다시 판별하지 않는다. 없으면(정렬 때 off, 0.5.0 전 워크스페이스) 지금 판별해 저장한다.
    """
    if opts.water == "off":
        return None, {"mode": "off", "active": False}
    if kind != "sparse":  # 판별해도 쓸 곳이 없음
        return None, {"mode": opts.water, "active": False, "note": "외부·평면 DSM에는 수면 평면을 적용하지 않음"}
    saved = wat.load_detection(*_water_paths(pdir))
    if saved is not None:
        det, source = saved[0], saved[1].get("source", "auto")
    else:
        t0 = time.perf_counter()
        out.stage("water", "수면 판별")
        det = wat.detect(image_dir, names, out)
        wat.save_detection(det, *_water_paths(pdir), source="auto")
        source = "auto"
        timings["water_s"] = time.perf_counter() - t0
    active = wat.decide(opts.water, det)
    info = _water_summary(opts.water, active, det, source)
    return (det.masks if active else None), info


def _check_radiometry_available(mode: str, ms_info: dict) -> None:
    """명시한 반사율 방식에 필요한 자료(DLS·패널 촬영)가 없으면 정렬(SfM) 전에 알린다.

    밴드마다 패널 정보가 다 있는지는 패널 영상을 읽어야 알 수 있어 정사 단계에서 다시 확인한다.
    """
    if mode in ("dls", "dls+panel") and not ms_info.get("has_dls"):
        raise InputError("입사광 센서(DLS) 값이 없는 캡처가 있어 DLS 방식을 쓸 수 없음", "invalid_argument")
    if mode in ("panel", "dls+panel") and not ms_info.get("panel_captures"):
        raise InputError("패널 촬영 영상이 없어 패널 방식을 쓸 수 없음", "invalid_argument")


def ms_dataset_from_meta(meta: dict) -> "msp.MsDataset | None":
    """다중분광 워크스페이스면 원본 밴드 폴더를 다시 읽는다 (정사 단계에서 모든 밴드가 필요)."""
    info = meta.get("multispectral")
    if not info:
        return None
    src = Path(info["source_dir"])
    if not src.is_dir():
        raise InputError(f"다중분광 원본 영상 폴더를 찾을 수 없음: {src}", "image_dir_missing")
    ds = msp.scan_multispectral(src)
    if ds is None:
        raise InputError(f"다중분광 영상을 찾지 못함: {src}", "image_dir_missing")
    return ds


def _version_tuple(v) -> tuple[int, int, int]:
    """"0.2.1", "0.3.0rc1", "0.2.0.dev0", "0.3"에서 (주, 부, 수) 버전을 얻는다. 읽을 수 없으면 (0, 0, 0)."""
    m = re.match(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v))
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())  # type: ignore[return-value]


def saved_render_options(meta: dict) -> tuple[OrthoOptions, str | None]:
    """meta.json에 저장된 정사 모자이크 옵션 (보정 후 재생성용). 반환: (옵션, 바꾼 내용 안내 또는 None)

    0.3.0 미만이 저장한 옵션에는 당시 기본값(GSD 배율 2, 캐시 600 MB)이 그대로 들어 있어 사용자가 고른 값과
    구분되지 않는다. 그 기본값과 같으면 새 기본값(원본 해상도, 캐시 자동)으로 바꾸고, 다른 값은 그대로 쓴다.
    저장한 버전은 render_engine_version(0.3.0부터 기록), 없으면 정렬한 엔진 버전으로 판단한다.
    """
    render = dict(meta.get("render") or {})
    ver = _version_tuple(meta.get("render_engine_version") or meta.get("engine_version", "0"))
    changed = []
    gsd_changed = False
    if render and ver < (0, 3, 0):
        if render.get("gsd_m") is None and render.get("gsd_scale", 2.0) == 2.0:
            render["gsd_scale"] = 1.0
            gsd_changed = True
            changed.append("GSD 배율 2 → 1(원본 해상도)")
        if render.get("cache_budget_mb") == 600:
            render["cache_budget_mb"] = None
            changed.append("영상 캐시 600 MB → 자동")
    note = None
    if changed:
        note = (f"이전 버전({'.'.join(map(str, ver))})이 저장한 정사 옵션의 당시 기본값을 새 기본값으로 바꿔 다시 만듦: "
                f"{', '.join(changed)}")
        if gsd_changed:
            note += ". 이전 해상도로 만들려면 다른 옵션은 그대로 두고 gsd_scale=2로 orthomosaic()을 다시 실행함"
    # 저장된 지수 선택은 긴 처리(번들 조정·정사투영) 끝에 실패하지 않도록 여기서 검사한다. 잘못된 값이면 기본값으로
    # 수면 처리: 0.5.0 전에 저장한 설정에는 없음 → 끔 (그때는 수면 처리 없이 만들었음)
    w = render.get("water")
    if w is None:
        render["water"] = "off"
    else:
        try:
            render["water"] = wat.normalize_mode(w if isinstance(w, str) else None if w is None else str(w))
        except ValueError:
            render["water"] = "off"
            bad = f"저장된 수면 처리 설정({w!r})을 해석할 수 없어 끄고 만듦"
            note = f"{note}. {bad}" if note else bad
    idx = render.get("indices")
    if idx is not None:
        try:
            if isinstance(idx, str):
                raise ValueError(idx)
            render["indices"] = normalize_selection(list(idx))
        except (ValueError, TypeError):
            render["indices"] = None
            bad = f"저장된 지수 선택({idx!r})을 해석할 수 없어 기본값(NDVI·NDRE·GNDVI)으로 만듦"
            note = f"{note}. {bad}" if note else bad
    return OrthoOptions.from_render_dict(render), note


def align_report_from_project(proj: Project) -> dict:
    """정렬 보고서가 없는 프로젝트(정렬 뒤 정사 모자이크 단계에서 실패한 경우)의 보고서를 재구성에서 다시 만든다.

    정렬 단계의 처리 시간·메모리 등 다시 알 수 없는 값은 비워 두고 경고로 알린다.
    """
    rec, frame = proj.load_base()
    meta = proj.meta()
    gps = {k: tuple(v) for k, v in meta.get("gps", {}).items()}
    names = [im.name for im in rec.images.values() if im.has_pose and im.name in gps]
    residual = None
    if names:
        e, n = Projector(frame.epsg).forward([gps[k][0] for k in names], [gps[k][1] for k in names])
        c = np.array([rec.find_image_with_name(k).projection_center() for k in names]) + frame.origin
        residual = float(np.sqrt(np.mean((c[:, 0] - e) ** 2 + (c[:, 1] - n) ** 2)))
    n_input = max(len(gps), rec.num_images())
    return {
        "engine_version": meta.get("engine_version", __version__),
        "input": {"folder": meta.get("image_dir"), "selected": n_input, "with_gps": len(gps)},
        "sfm": {
            "num_input_images": n_input,
            "num_registered": rec.num_reg_images(),
            "num_points3D": rec.num_points3D(),
            "mean_reprojection_error_px": float(rec.compute_mean_reprojection_error()),
            "mapper": "unknown",
        },
        "georef": {
            "epsg": frame.epsg,
            "gps_residual_rms_m": residual,
            "num_aligned": len(names),
            "note": "절대 위치 정확도는 GNSS 수준(수 m)임. 정밀 위치가 필요하면 GCP 필요",
        },
        "timings_s": {},
        "peak_memory_mb": 0.0,
        "report_version": REPORT_VERSION,
        "warnings": ["정렬 결과는 이전 실행에서 가져옴 (정사 모자이크 단계에서 중단된 작업). 정렬 단계 처리 시간은 기록되지 않음"],
    }


def compose_report(
    proj: Project, products: dict, render_timings: dict[str, float], render_peak: int,
    extra_warnings: list[str] | None = None,
) -> dict:
    """정렬 보고서 + 마지막 정사 모자이크 결과 + (보정했으면) 보정 결과를 합쳐 report.json을 쓴다.

    timings_s: 정렬 단계 시간 + 이번 정사 모자이크 단계 시간. total_s는 둘의 합이다.
    peak_memory_mb: 정렬 단계와 이번 정사 모자이크 단계 중 큰 값.
    extra_warnings: 이번 실행에만 해당하는 경고 (저장된 보고서에 남기지 않으므로 다음 실행의 보고서에는 나오지 않음).
    """
    align = proj.align_report()
    if not align:
        # 정렬 보고서가 없는 워크스페이스: 데스크톱 앱 v0.2.0이 정렬 직후 정사 모자이크 단계에서 실패한 경우
        align = align_report_from_project(proj)
        proj.save_align_report(align)
    report = dict(align)
    products = dict(products)
    extra_warnings = list(products.pop("_warnings", [])) + list(extra_warnings or [])  # 이번 정사 단계의 경고
    report.update(products)
    if isinstance(products.get("water"), dict) and isinstance(align.get("water"), dict):
        # 판별 정보(수면 비율, 특징점 제외 영상 수)는 정렬 때 값에 이번 정사 단계의 방식·DSM 통계를 합침
        report["water"] = {**align["water"], **products["water"]}
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
    report["warnings"] = list(dict.fromkeys(warnings + list(extra_warnings or [])))  # 같은 경고는 한 번만
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
    ms_ds: "msp.MsDataset | None" = None,
) -> dict:
    """투영 좌표(절대값) 재구성으로 간이 DSM, 정사 모자이크(COG), 미리보기를 만든다.

    ms_ds가 있으면 다중분광: 정사 모자이크는 밴드별 반사율(float32), 식생지수 GeoTIFF를 함께 만든다.
    반환: 보고서의 dsm, ortho, outputs, preview_corners_lonlat (다중분광이면 multispectral) 항목.
    """
    crs = CRS.from_epsg(epsg)
    t0 = time.perf_counter()
    mode = str(opts.dsm)
    kind = mode if mode in ("sparse", "plane") else "external"
    water_masks, water_info = None, None
    if ms_ds is None:  # 수면 판별 결과 (저장된 것이 없으면 여기서 판별, 'water' 단계)
        names = sorted(im.name for im in rec.images.values() if im.has_pose)
        water_masks, water_info = _water_for_render(Project(out_dir).dir, opts, image_dir, names, kind, out, timings)
        t0 = time.perf_counter()
    out.stage("dsm", {"sparse": "희소 점군으로 간이 DSM 생성", "plane": "수평면 DSM 생성",
                      "external": "외부 DSM 불러오기"}[kind])
    pts = remove_z_outliers(sparse_points(rec))
    if kind == "sparse" and len(pts) < 10:
        raise ProcessingError("DSM 생성 실패: 유효한 3D 점이 너무 적음", "dsm_failed")
    if kind == "plane" and opts.dsm_z is not None:
        ground_z = float(opts.dsm_z)
    elif len(pts):
        ground_z = float(np.median(pts[:, 2]))
    else:
        raise ProcessingError("DSM 생성 실패: 3D 점이 없어 지면 높이를 정할 수 없음 (dsm_z 지정 필요)", "dsm_failed")
    views = build_views(rec, ground_z)
    src_gsd = native_gsd(views, ground_z)
    gsd = opts.gsd_m or src_gsd * opts.gsd_scale
    if ms_ds is not None:
        try:
            factors, radiometry = msp.reflectance_factors(ms_ds, opts.radiometry)
        except ValueError as e:  # 요청한 반사율 방식에 필요한 DLS·패널이 없음
            raise InputError(str(e), "invalid_argument") from e
        sources = build_band_sources(views, ms_ds, factors)
        views = [v for v in views if v.name in sources]
        if len(views) < 1:
            raise InputError("원본 밴드 폴더의 영상이 정렬 결과와 맞지 않음 (정합된 캡처의 밴드 영상을 찾지 못함)",
                             "image_dir_missing")
        bgsd = band_gsd(sources, ground_z)
        gsd = opts.gsd_m or bgsd * opts.gsd_scale  # 출력은 다중분광 밴드의 원본 해상도 기준
    if opts.bounds is not None:
        raw_bounds = tuple(float(v) for v in opts.bounds)
    else:
        vb = np.array([v.bbox for v in views])
        raw_bounds = (vb[:, 0].min(), vb[:, 1].min(), vb[:, 2].max(), vb[:, 3].max())
    bounds = _snap_bounds(raw_bounds, gsd, tuple(opts.grid_origin))
    area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
    dsm_info: dict = {"mode": kind}
    if kind == "sparse":
        # 점 간격의 절반 격자 (0.2.x의 1.5배 격자 + 강한 평활화는 갈대·관목·작은 기복의 높이를 지워 영상 간 어긋남을 키웠음)
        dsm_res = max(0.5, 0.5 * math.sqrt(area / len(pts)))
        dsm = build_dsm(pts, bounds, dsm_res)
        if water_masks:
            # 수면 칸을 물가 높이의 평면으로 덮음 (점은 지우지 않음)
            wmask, level, wstats = wat.ground_water(views, dsm, water_masks, pts)
            if wmask.any():
                dsm = wat.flatten(dsm, wmask, level)
                lv = sorted({round(float(x), 2) for x in np.unique(level[wmask])})
                out.log(f"수면 {wstats['area_m2']:.0f} m²를 평면(높이 {', '.join(f'{x:.2f}' for x in lv)} m)으로 덮음")
            else:
                out.log("수면으로 확정한 곳 없음 (수면 후보 안에 3D 점이 많거나 주변보다 높음)")
            water_info.update(wstats)
    elif kind == "plane":
        dsm_res = max(0.5, math.sqrt(area) / 256)
        dsm = plane_dsm(ground_z, bounds, dsm_res, num_points=len(pts))
        dsm_info["plane_z"] = ground_z
    else:
        dsm_res = max(0.5, gsd, math.sqrt(area) / 4096)
        dsm, offset = external_dsm(Path(mode), bounds, dsm_res, epsg, pts if opts.dsm_vertical_align else None)
        dsm_info.update(source=str(Path(mode).expanduser().resolve()), vertical_offset_m=offset)
    out.progress("dsm", 1, 1)  # 중단 확인 지점
    # 결과물은 임시 파일에 쓰고 마지막에 교체한다. 중단·실패 시 이전 결과물이 섞이지 않게 하기 위해서다.
    dsm_tmp = out_dir / "dsm.tmp.tif"
    tmp = out_dir / "orthomosaic.tmp.tif"
    cog_tmp = out_dir / "orthomosaic.cog.tmp.tif"
    final = out_dir / "orthomosaic.tif"
    if ms_ds is not None:
        res = _render_ms(views, sources, ms_ds, factors, radiometry, rec, dsm, dsm_info, dsm_res, ground_z,
                         bounds, crs, gsd, src_gsd, bgsd, out_dir, opts, out, timings, t0)
        if opts.water == "on":
            res["_warnings"] = list(res.get("_warnings", [])) + [MS_WATER_WARNING]
        return res
    try:
        _write_dsm(dsm, dsm_tmp, crs)
        timings["dsm_s"] = time.perf_counter() - t0

        # 영상 간 밝기·색 보정: 심라인 이음매의 밝기 단차를 없애는 영상별 저주파 이득 맵 (영상 좌표에 묶임)
        gains, color_stats = None, None
        if opts.color_balance and len(views) > 1:
            t0 = time.perf_counter()
            out.stage("color", "영상 간 밝기·색 보정")
            # 결과 범위(bounds)가 아니라 촬영 범위 전체로 구한다. 범위·격자 기준점을 바꿔 만든 결과끼리도
            # 같은 이득을 쓰게 해서, 나눠 만든 결과를 이어 붙이거나 시기별로 비교할 때 밝기가 어긋나지 않게 하기 위해서다
            vb = np.array([v.bbox for v in views])
            coverage = (float(vb[:, 0].min()), float(vb[:, 1].min()), float(vb[:, 2].max()), float(vb[:, 3].max()))
            gains, color_stats = estimate_gains(views, dsm, image_dir, coverage, src_gsd, out)
            if not gains:  # 이득 맵을 만든 영상이 없음
                gains, color_stats = None, None
            timings["color_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("ortho", f"정사 모자이크 생성 (GSD {gsd * 100:.1f} cm)")
        ortho_stats = render_orthomosaic(
            views, dsm, image_dir, tmp, crs, gsd, src_gsd, bounds, out,
            cache_budget_mb=opts.cache_budget_mb, gains=gains,
        )
        if color_stats:
            ortho_stats["color"] = color_stats
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
    extra = {"water": water_info} if water_info is not None else {}
    return {
        **extra,
        "dsm": {**dsm_info, "resolution_m": dsm_res, "num_points": dsm.num_points},
        "ortho": {**ortho_stats, "source_gsd_m": src_gsd},
        "outputs": {
            "orthomosaic": str(final),
            "dsm": str(out_dir / "dsm.tif"),
            "preview": str(out_dir / "preview.png"),
        },
        "preview_corners_lonlat": corners,
    }


def _render_ms(views, sources, ds, factors, radiometry, rec, dsm, dsm_info, dsm_res, ground_z, bounds, crs,
               gsd, src_gsd, bgsd, out_dir, opts, out, timings, t0):
    """다중분광 정사: 밴드 정합 미세 조정 → 캡처 간 반사율 보정 → 반사율 정사 모자이크 → 식생지수·미리보기.

    결과물은 RGB와 같이 임시 파일에 쓰고 마지막에 교체한다 (중단·실패 시 이전 결과 유지).
    """
    dsm_tmp = out_dir / "dsm.tmp.tif"
    tmp = out_dir / "orthomosaic.tmp.tif"
    cog_tmp = out_dir / "orthomosaic.cog.tmp.tif"
    final = out_dir / "orthomosaic.tif"
    names = ds.band_names()
    staged = None
    try:
        _write_dsm(dsm, dsm_tmp, crs)
        timings["dsm_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("bands", "밴드 정합 미세 조정·캡처 간 반사율 보정")
        # rig 미세 조정은 두 번 반복 (첫 추정이 약간 과하게 나오는 경향이 있어 두 번째에 0.1화소 이내로 수렴)
        rig_corr: dict = {}
        rig_stats: dict = {}
        for it in range(2):
            corr, st = refine_rig(views, sources, ds, dsm, ground_z)
            rig_corr = {i: corr[i] @ rig_corr[i] if i in rig_corr else corr[i] for i in corr}
            sources = build_band_sources(views, ds, factors, rig_corr=rig_corr)
            if it == 0:
                rig_stats = {"captures": st["captures"], "initial": st["bands"]}
            out.progress("bands", it + 1, 4)  # 중단 확인 지점
        _, st = refine_rig(views, sources, ds, dsm, ground_z, max_captures=4)
        rig_stats["residual"] = st["bands"]
        radiometry["band_alignment"] = rig_stats
        out.progress("bands", 3, 4)
        if opts.ms_normalize and radiometry["mode"] != "radiance":
            gains, norm_stats = normalize_captures(rec, views, sources, ds)
            for n, srcs in sources.items():
                for k, src in enumerate(srcs):
                    src.factor *= gains[n][k]
            radiometry["capture_normalization"] = norm_stats
        out.progress("bands", 4, 4)
        timings["bands_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("ortho", f"다중분광 정사 모자이크 생성 ({len(names)}밴드, GSD {gsd * 100:.1f} cm)")
        ortho_stats = render_ms_orthomosaic(
            views, sources, names, dsm, ds.root, tmp, crs, gsd, src_gsd, bgsd, bounds, out,
            cache_budget_mb=opts.cache_budget_mb,
        )
        timings["ortho_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        out.stage("finalize", "COG 변환·식생지수·미리보기 생성")
        finalize_cog(tmp, cog_tmp)
        # 지수는 새 정사 모자이크(교체 전 임시 파일)에서 indices.new에 만들어 두고, 정사 모자이크와 함께 바꾼다
        staged = build_indices(cog_tmp, out_dir, names, normalize_selection(opts.indices), finalize_cog)
        out.progress("finalize", 1, 1)  # 중단 확인 지점 (결과물 교체 전)
        os.replace(cog_tmp, final)
        os.replace(dsm_tmp, out_dir / "dsm.tif")
        try:
            staged.commit()
        except BaseException:
            _remove_indices(out_dir)  # 새 정사 모자이크와 맞지 않는 이전 지수를 남기지 않음
            raise
    finally:
        for f in (dsm_tmp, tmp, cog_tmp):
            f.unlink(missing_ok=True)
        if staged is not None:
            staged.discard()
    tifs, pngs, skipped = staged.tifs, staged.pngs, staged.skipped
    preview = write_rgb_preview(final, out_dir / "preview.png", names)
    outputs = {"orthomosaic": str(final), "dsm": str(out_dir / "dsm.tif"), "preview": str(out_dir / "preview.png")}
    if tifs:
        outputs["indices"] = tifs
        outputs["index_previews"] = pngs
    corners = _corners_lonlat(final)
    timings["finalize_s"] = time.perf_counter() - t0
    any_cap = next(iter(ds.captures.values()))
    render_warnings = []
    if radiometry.get("panel_missing_bands"):  # auto에서만 기록됨 (명시한 방식은 사용자가 고른 것)
        missing = ", ".join(radiometry["panel_missing_bands"])
        if radiometry["mode"] == "dls":
            render_warnings.append(
                f"패널 촬영이 있지만 {missing} 밴드에 패널 정보가 없어 패널 보정 없이 DLS만으로 반사율을 구함. "
                "반사율 수준이 크게 어긋날 수 있음 (패널을 모든 밴드에 잡히게 다시 촬영하는 것을 권장)"
            )
        else:
            render_warnings.append(
                f"패널 촬영이 있지만 {missing} 밴드에 패널 정보가 없고 DLS도 없어 반사율로 바꾸지 못함 (방사휘도로 출력)"
            )
    return {
        "_warnings": render_warnings,
        "dsm": {**dsm_info, "resolution_m": dsm_res, "num_points": dsm.num_points},
        "ortho": {**ortho_stats, "color_balance": False, "source_gsd_m": bgsd, "reference_gsd_m": src_gsd},
        "multispectral": {
            "model": ds.model,
            "bands": [{"name": any_cap[i].band, "wavelength_nm": any_cap[i].wavelength} for i in ds.out_indices],
            "reference_band": any_cap[ds.ref_index].band,
            "values": "radiance" if radiometry["mode"] == "radiance" else "reflectance",
            "radiometry": radiometry,
            "indices": list(tifs),
            "index_legend": index_legend(list(tifs)),
            "indices_skipped": skipped,
            **preview,
        },
        "outputs": outputs,
        "preview_corners_lonlat": corners,
    }


def _remove_indices(out_dir: Path) -> None:
    """지수 산출물(0.4.1 indices/ 폴더, 0.4.0 indices.tif·ndvi.png)을 지운다."""
    for d in ("indices", "indices.new", "indices.old"):
        shutil.rmtree(out_dir / d, ignore_errors=True)
    for f in ("indices.tif", "ndvi.png"):
        (out_dir / f).unlink(missing_ok=True)


def make_indices(out_dir: Path, selected: list[str] | None, out: Emitter) -> dict:
    """이미 만든 다중분광 정사 모자이크에서 지수만 다시 만든다 (정렬·정사투영 생략).

    report.json의 지수 항목과 저장된 정사 옵션(meta.json의 render.indices)도 갱신한다.
    selected: None이면 기본 지수(NDVI·NDRE·GNDVI), []이면 지수 파일을 모두 지움.
    """
    from ..errors import ProjectError

    out_dir = Path(out_dir)
    try:
        chosen = normalize_selection(selected)
    except ValueError as e:
        raise InputError(str(e), "invalid_argument") from e
    report_path = out_dir / "report.json"
    final = out_dir / "orthomosaic.tif"
    if not report_path.exists() or not final.exists():
        raise ProjectError("정사 모자이크 결과가 없음. 먼저 orthomosaic() 또는 process()를 실행해야 함", "not_rendered")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ms_info = report.get("multispectral")
    if not ms_info:
        raise ProjectError("다중분광 결과가 아니라 지수를 만들 수 없음", "not_multispectral")
    names = [b["name"] for b in ms_info["bands"]]
    out.stage("finalize", f"지수 생성 ({', '.join(chosen) or '없음'})")
    t0 = time.perf_counter()
    # 마지막 중단 확인 지점은 교체 전 (교체한 뒤 중단되면 파일과 보고서가 어긋남)
    tifs, pngs, skipped = write_indices(final, out_dir, names, chosen, finalize_cog,
                                        before_swap=lambda: out.progress("finalize", 1, 1))
    outputs = {k: v for k, v in report.get("outputs", {}).items() if k not in ("indices", "index_previews", "index_preview")}
    if tifs:
        outputs["indices"] = tifs
        outputs["index_previews"] = pngs
    report["outputs"] = outputs
    ms_info.update(indices=list(tifs), index_legend=index_legend(list(tifs)), indices_skipped=skipped)
    report.setdefault("timings_s", {})["indices_s"] = round(time.perf_counter() - t0, 2)
    tmp = report_path.with_name("report.json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, report_path)
    proj = Project(out_dir)
    if proj.exists():
        meta = proj.meta()
        proj.update_render({**(meta.get("render") or {}), "indices": chosen},
                           meta.get("render_engine_version") or __version__)
    return report


def _alt(im: dict) -> float:
    for key in ("abs_alt", "rel_alt"):
        if im.get(key) is not None:
            return float(im[key])
    return 0.0


def _snap_bounds(
    b: tuple[float, float, float, float], gsd: float, origin: tuple[float, float] = (0.0, 0.0)
) -> tuple[float, float, float, float]:
    """범위를 바깥쪽으로 넓혀 origin에서 GSD 배수인 위치에 맞춘다 (같은 gsd·origin이면 화소 경계가 겹침)."""
    ox, oy = float(origin[0]), float(origin[1])
    eps = 1e-9
    x0 = ox + math.floor((b[0] - ox) / gsd + eps) * gsd
    y0 = oy + math.floor((b[1] - oy) / gsd + eps) * gsd
    x1 = ox + math.ceil((b[2] - ox) / gsd - eps) * gsd
    y1 = oy + math.ceil((b[3] - oy) / gsd - eps) * gsd
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
