"""다중분광 파생 산출물: 식생·수분 지수.

반사율 정사 모자이크(밴드 설명에 밴드 이름)에서 사용자가 고른 지수만 계산해 지수마다 GeoTIFF(COG)와
표시용 PNG를 만든다. 정사 모자이크를 다시 만들지 않고 지수만 다시 만들 수도 있다(make_indices).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio

# 색 띠: (위치 0~1, R, G, B)
_VEG = [(0.0, 140, 81, 10), (0.35, 216, 179, 101), (0.55, 246, 232, 195), (0.7, 145, 207, 96), (1.0, 26, 152, 80)]
_WATER = [(0.0, 140, 81, 10), (0.45, 246, 232, 195), (0.6, 199, 234, 229), (1.0, 33, 102, 172)]


@dataclass(frozen=True)
class IndexDef:
    name: str
    label: str  # 화면 표시용 설명
    bands: tuple[str, ...]  # 필요한 밴드 (소문자 이름)
    fn: Callable[..., np.ndarray]
    vmin: float  # 미리보기 색 범위
    vmax: float
    palette: str = "vegetation"


def _nd(a, b):
    s = a + b
    return np.where(np.abs(s) > 1e-6, (a - b) / s, np.nan)


def _ratio_minus1(a, b):
    return np.where(np.abs(b) > 1e-6, a / b - 1.0, np.nan)


INDEX_DEFS: dict[str, IndexDef] = {
    d.name: d
    for d in [
        IndexDef("NDVI", "정규식생지수", ("nir", "red"), lambda nir, red: _nd(nir, red), -0.2, 0.9),
        IndexDef("NDRE", "적색경계 정규지수 (생육 후기·질소)", ("nir", "red edge"), lambda nir, re: _nd(nir, re), -0.1, 0.6),
        IndexDef("GNDVI", "녹색 정규식생지수 (엽록소)", ("nir", "green"), lambda nir, g: _nd(nir, g), -0.2, 0.9),
        IndexDef("NDWI", "정규수분지수 (수면, McFeeters)", ("green", "nir"), lambda g, nir: _nd(g, nir), -0.6, 0.6, "water"),
        IndexDef(
            "OSAVI", "토양 보정 식생지수 (최적화)", ("nir", "red"),
            lambda nir, red: np.where(np.abs(nir + red + 0.16) > 1e-6, (nir - red) / (nir + red + 0.16), np.nan),
            -0.2, 0.8,
        ),
        IndexDef(
            "SAVI", "토양 보정 식생지수 (L=0.5)", ("nir", "red"),
            lambda nir, red: np.where(np.abs(nir + red + 0.5) > 1e-6, 1.5 * (nir - red) / (nir + red + 0.5), np.nan),
            -0.2, 0.8,
        ),
        IndexDef(
            "EVI", "향상 식생지수", ("nir", "red", "blue"),
            lambda nir, red, blue: np.where(
                np.abs(nir + 6 * red - 7.5 * blue + 1) > 1e-6, 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1), np.nan
            ),
            -0.2, 0.9,
        ),
        IndexDef("CIre", "적색경계 엽록소지수", ("nir", "red edge"), lambda nir, re: _ratio_minus1(nir, re), 0.0, 4.0),
        IndexDef("CIg", "녹색 엽록소지수", ("nir", "green"), lambda nir, g: _ratio_minus1(nir, g), 0.0, 8.0),
    ]
}

DEFAULT_INDICES = ["NDVI", "NDRE", "GNDVI"]


def available_indices(band_names: list[str]) -> list[str]:
    have = {n.strip().lower() for n in band_names}
    return [k for k, d in INDEX_DEFS.items() if all(b in have for b in d.bands)]


def index_catalog(band_names: list[str]) -> list[dict]:
    """앱 설정 화면용: 지수 목록(이름, 설명, 필요한 밴드, 이 카메라로 만들 수 있는지)."""
    avail = set(available_indices(band_names))
    return [
        {"name": k, "label": d.label, "bands": list(d.bands), "available": k in avail, "default": k in DEFAULT_INDICES}
        for k, d in INDEX_DEFS.items()
    ]


def normalize_selection(selected: list[str] | None) -> list[str]:
    """지수 이름 목록을 검사하고 등록 순서로 정리한다 (대소문자 무시). None이면 기본값."""
    if selected is None:
        return list(DEFAULT_INDICES)
    lower = {k.lower(): k for k in INDEX_DEFS}
    out = []
    for s in selected:
        k = lower.get(str(s).strip().lower())
        if k is None:
            raise ValueError(f"알 수 없는 지수: {s} (가능: {', '.join(INDEX_DEFS)})")
        if k not in out:
            out.append(k)
    return [k for k in INDEX_DEFS if k in out]


def compute_index(index: "str | IndexDef", bands: dict[str, np.ndarray]) -> np.ndarray:
    """밴드 배열로 지수 하나를 계산한다.

    index: 등록된 지수 이름(대소문자 무관) 또는 직접 만든 :class:`IndexDef`.
    bands: 밴드 이름(대소문자 무관) → 반사율 배열. 지수에 필요한 밴드가 있어야 함 (없으면 KeyError).
    분모가 0에 가까운(|x| ≤ 1e-6) 곳과 입력이 NaN인 곳은 NaN.
    """
    d = index if isinstance(index, IndexDef) else INDEX_DEFS[normalize_selection([index])[0]]
    look = {k.strip().lower(): v for k, v in bands.items()}
    need = [b.strip().lower() for b in d.bands]  # 직접 만든 IndexDef의 밴드 이름도 대소문자 무관
    missing = [b for b in need if b not in look]
    if missing:
        raise KeyError(f"{d.name}에 필요한 밴드가 없음: {', '.join(missing)}")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(d.fn(*(np.asarray(look[b], dtype=np.float32) for b in need)), dtype=np.float32)


@dataclass
class StagedIndices:
    """indices.new에 만들어 둔 지수. :meth:`commit`으로 indices와 바꾸고, :meth:`discard`로 남은 것을 지운다."""

    out_dir: Path
    tifs: dict[str, str]  # 바꾼 뒤의 경로 (indices/<이름>.tif)
    pngs: dict[str, str]
    skipped: list[str]

    @property
    def new_dir(self) -> Path:
        return self.out_dir / "indices.new"

    def commit(self) -> None:
        """이전 지수(indices/, v0.4.0 indices.tif·ndvi.png)를 새 지수로 바꾼다.

        이전 indices는 먼저 indices.old로 옮긴다. 다른 프로그램이 파일을 열고 있어(Windows) 옮길 수 없으면
        아무것도 바꾸지 않고 오류를 낸다 (지우다 만 상태가 되지 않게).
        """
        final_dir, old_dir = self.out_dir / "indices", self.out_dir / "indices.old"
        shutil.rmtree(old_dir, ignore_errors=True)
        if final_dir.exists():
            os.replace(final_dir, old_dir)
        try:
            if self.tifs:
                os.replace(self.new_dir, final_dir)
        except BaseException:
            if old_dir.exists() and not final_dir.exists():
                os.replace(old_dir, final_dir)
            raise
        for old in (self.out_dir / "indices.tif", self.out_dir / "ndvi.png"):  # v0.4.0 형식
            old.unlink(missing_ok=True)
        shutil.rmtree(old_dir, ignore_errors=True)

    def discard(self) -> None:
        shutil.rmtree(self.new_dir, ignore_errors=True)


def build_indices(
    refl_path: Path,
    out_dir: Path,
    band_names: list[str],
    selected: list[str],
    finalize: Callable[[Path, Path], None],
    max_preview: int = 2048,
) -> StagedIndices:
    """고른 지수를 out_dir/indices.new/<이름>.tif(COG)와 <이름>.png로 만든다 (아직 바꾸지 않음).

    SDK: 앱은 이전 지수를 먼저 지우고 바로 indices/에 만든다. SDK는 다 만든 뒤 바꾸므로 만드는 도중 실패·중단하면 이전 지수가 남는다.
    실패하면 만들던 것만 지운다.
    """
    look = {n.strip().lower(): i for i, n in enumerate(band_names)}
    avail = set(available_indices(band_names))
    todo = [k for k in selected if k in avail]
    staged = StagedIndices(out_dir, {}, {}, [k for k in selected if k not in avail])
    staged.discard()
    if not todo:
        return staged
    idx_dir = staged.new_dir
    final_dir = out_dir / "indices"
    idx_dir.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(refl_path) as src:
            prof = src.profile.copy()
            prof.update(count=1, dtype="float32", nodata=float("nan"), predictor=3, compress="deflate",
                        tiled=True, blockxsize=512, blockysize=512)
            prof.pop("photometric", None)
            tmps = {k: idx_dir / f"{k}.tmp.tif" for k in todo}
            dsts = {k: rasterio.open(tmps[k], "w", **prof) for k in todo}
            try:
                for k in todo:
                    dsts[k].set_band_description(1, k)
                for _, win in src.block_windows(1):
                    data = src.read(window=win).astype(np.float32)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        for k in todo:
                            d = INDEX_DEFS[k]
                            val = d.fn(*(data[look[b]] for b in d.bands)).astype(np.float32)
                            dsts[k].write(val[None], window=win)
            finally:
                for ds in dsts.values():
                    ds.close()
        for k in todo:
            final = idx_dir / f"{k}.tif"
            finalize(tmps[k], final)
            tmps[k].unlink(missing_ok=True)
            write_index_preview(final, idx_dir / f"{k}.png", INDEX_DEFS[k], max_preview)
            staged.tifs[k], staged.pngs[k] = str(final_dir / f"{k}.tif"), str(final_dir / f"{k}.png")
    except BaseException:
        staged.discard()
        raise
    return staged


def write_indices(
    refl_path: Path,
    out_dir: Path,
    band_names: list[str],
    selected: list[str],
    finalize: Callable[[Path, Path], None],
    max_preview: int = 2048,
    before_swap: Callable[[], None] | None = None,
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """고른 지수를 out_dir/indices/<이름>.tif(COG)와 <이름>.png로 만든다. 이전 지수 파일은 지운다.

    before_swap: 다 만든 뒤 바꾸기 직전에 부름 (중단 확인 지점. 여기서 예외가 나면 이전 지수가 그대로 남음).
    반환: {이름: tif 경로}, {이름: png 경로}, 밴드가 없어 건너뛴 지수
    """
    staged = build_indices(refl_path, out_dir, band_names, selected, finalize, max_preview)
    try:
        if before_swap is not None:
            before_swap()
        staged.commit()
    finally:
        staged.discard()
    return staged.tifs, staged.pngs, staged.skipped


def write_index_preview(path: Path, png_path: Path, d: IndexDef, max_size: int = 2048) -> None:
    from PIL import Image

    with rasterio.open(path) as ds:
        s = min(1.0, max_size / max(ds.width, ds.height))
        w, h = max(1, int(ds.width * s)), max(1, int(ds.height * s))
        v = ds.read(1, out_shape=(h, w), resampling=rasterio.enums.Resampling.average)
    valid = np.isfinite(v)
    t = np.clip((np.nan_to_num(v, nan=d.vmin) - d.vmin) / (d.vmax - d.vmin), 0, 1)
    stops = np.array(_WATER if d.palette == "water" else _VEG, dtype=np.float64)
    rgba = np.zeros((h, w, 4), np.uint8)
    for c in range(3):
        rgba[..., c] = np.where(valid, np.interp(t, stops[:, 0], stops[:, c + 1]), 0).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0)
    Image.fromarray(rgba, "RGBA").save(png_path)


def legend(selected: list[str]) -> dict[str, dict]:
    """앱 범례용: 지수별 색 범위와 색 띠."""
    out = {}
    for k in selected:
        d = INDEX_DEFS[k]
        stops = _WATER if d.palette == "water" else _VEG
        out[k] = {
            "label": d.label,
            "min": d.vmin,
            "max": d.vmax,
            "colors": ["#%02x%02x%02x" % s[1:] for s in (stops[0], stops[len(stops) // 2], stops[-1])],
        }
    return out
