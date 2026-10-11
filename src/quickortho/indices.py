"""다중분광 식생·수분 지수 (0.4.1).

다중분광 정사 처리는 :attr:`quickortho.OrthoOptions.indices`로 고른 지수를 자동으로 만들므로 이 모듈이 필요 없음.
지수 목록을 보거나, 반사율 GeoTIFF·배열에 직접 지수를 계산하거나, 자기 지수식을 써 보고 싶을 때 씀.

예::

    from quickortho import indices as qi

    qi.names()                                   # ['NDVI', 'NDRE', 'GNDVI', 'NDWI', 'OSAVI', 'SAVI', 'EVI', 'CIre', 'CIg']
    qi.available(qo.band_names("workspace/"))    # 이 카메라 밴드로 만들 수 있는 지수
    ndvi, tf, crs = qi.compute_raster("workspace/", "ndvi")   # 반사율 정사 모자이크에서 바로 계산

    # 자기 지수식 (등록 없이 바로 계산)
    rdvi = qi.IndexDef("RDVI", "내 지수", ("nir", "red"),
                       lambda nir, red: (nir - red) / np.sqrt(np.maximum(nir + red, 1e-6)), -0.2, 0.8)
    v = qi.compute(rdvi, {"NIR": nir, "Red": red})
    v, tf, crs = qi.compute_raster("workspace/", rdvi)

지수식과 밴드 이름 규칙은 문서 ``docs/concepts.md``의 "다중분광 처리" 절 참고.

설계 메모: 사용자 지수를 등록하는 함수는 두지 않음. 고른 지수 이름은 프로젝트(meta.json)에 저장되어
``refine()``·CLI·다른 프로세스에서 다시 쓰이는데, 실행 중에만 등록된 지수는 그때 찾을 수 없기 때문임.
자기 지수는 :class:`IndexDef`를 만들어 :func:`compute`·:func:`compute_raster`에 직접 넘김.
이 모듈은 1.0 전까지 이름·반환 형식이 바뀔 수 있음.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Union

import numpy as np

from ._core import indices as _ix
from ._core.indices import IndexDef
from .errors import InputError

#: 등록된 지수 (이름 → :class:`IndexDef`). 읽기 전용.
INDEX_DEFS: Mapping[str, IndexDef] = MappingProxyType(_ix.INDEX_DEFS)
#: ``OrthoOptions.indices=None``일 때 만드는 지수 (0.4.0과 같은 결과)
DEFAULT_INDICES: tuple[str, ...] = tuple(_ix.DEFAULT_INDICES)

IndexLike = Union[str, IndexDef]
Bounds = tuple[float, float, float, float]


def names() -> list[str]:
    """등록된 지수 이름 (등록 순서)."""
    return list(_ix.INDEX_DEFS)


def get(name: str) -> IndexDef:
    """이름(대소문자 무관)으로 지수 정의를 찾음. 모르는 이름이면 :class:`~quickortho.InputError`."""
    return _ix.INDEX_DEFS[normalize([name])[0]]


def normalize(selected: Optional[Sequence[str]]) -> list[str]:
    """지수 이름 목록을 검사해 등록 순서로 정리 (대소문자 무시, 중복 제거). ``None``이면 기본 선택.

    모르는 이름이면 :class:`~quickortho.InputError` (code ``invalid_argument``).
    """
    if isinstance(selected, str):
        raise InputError("지수는 이름 목록으로 넘겨야 함 (예: ['NDVI', 'NDRE'])", "invalid_argument")
    try:
        return _ix.normalize_selection(None if selected is None else list(selected))
    except ValueError as e:
        raise InputError(str(e), "invalid_argument") from None


def available(band_names: Sequence[str]) -> list[str]:
    """밴드 이름 목록(대소문자 무관)으로 계산할 수 있는 지수 (등록 순서)."""
    return _ix.available_indices(list(band_names))


def catalog(band_names: Sequence[str]) -> list[dict]:
    """지수 목록: ``{"name", "label", "bands", "available", "default"}``. 폴더 미리보기의 ``index_catalog``와 같음."""
    return _ix.index_catalog(list(band_names))


def legend(selected: Sequence[str]) -> dict[str, dict]:
    """지수별 미리보기 색 범위와 색 띠 ``{이름: {"label", "min", "max", "colors"}}``. 보고서의 ``index_legend``와 같음."""
    return _ix.legend(normalize(selected))


def _resolve(index: IndexLike) -> IndexDef:
    if isinstance(index, IndexDef):
        return index
    return get(index)


def compute(index: IndexLike, bands: Mapping[str, np.ndarray]) -> np.ndarray:
    """밴드 배열로 지수 하나를 계산해 float32 배열을 돌려줌.

    Args:
        index: 등록된 지수 이름(대소문자 무관) 또는 직접 만든 :class:`IndexDef`.
        bands: 밴드 이름(대소문자 무관, 예: ``"NIR"``, ``"Red edge"``) → 반사율 배열. 배열 모양은 서로 같아야 함.

    분모가 0에 가까운(|x| ≤ 1e-6) 곳과 입력이 NaN인 곳은 NaN. 필요한 밴드가 없으면 :class:`~quickortho.InputError`.
    """
    d = _resolve(index)
    try:
        return _ix.compute_index(d, dict(bands))
    except KeyError as e:
        raise InputError(str(e.args[0]) if e.args else str(e), "invalid_argument") from None


def compute_raster(
    source: Union[str, Path],
    index: IndexLike,
    *,
    scale: float = 1.0,
    bounds: Optional[Bounds] = None,
) -> tuple[np.ndarray, "object", "object"]:
    """반사율 GeoTIFF(밴드 설명 = 밴드 이름)에서 지수를 계산해 ``(array, transform, crs)``로 돌려줌.

    Args:
        source: 다중분광 워크스페이스 폴더(``orthomosaic.tif``) 또는 반사율 GeoTIFF 경로.
        index: 지수 이름 또는 :class:`IndexDef`.
        scale, bounds: :func:`quickortho.read_raster`와 같음.

    필요한 밴드만 읽음. 정사 모자이크 밖(NaN)은 NaN.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window, from_bounds
    from affine import Affine
    from pyproj import CRS

    from .geodata import _resolve_raster

    d = _resolve(index)
    if not 0 < scale <= 1:
        raise InputError("scale은 0보다 크고 1 이하여야 함", "invalid_argument")
    p = Path(source).expanduser()
    if p.is_dir():
        p = _resolve_raster(p, "orthomosaic.tif")
    elif not p.exists():
        raise InputError(f"파일이 없음: {p}", "file_not_found")
    with rasterio.open(p) as ds:
        look = {(desc or "").strip().lower(): i + 1 for i, desc in enumerate(ds.descriptions)}
        need = [b.strip().lower() for b in d.bands]
        missing = [b for b in need if b not in look]
        if missing:
            have = ", ".join(x for x in (ds.descriptions or ()) if x) or "없음"
            raise InputError(f"{d.name}에 필요한 밴드가 없음: {', '.join(missing)} (파일 밴드: {have})",
                             "invalid_argument")
        win = (from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
               if bounds is not None else Window(0, 0, ds.width, ds.height))
        out_h = max(1, int(round(win.height * scale)))
        out_w = max(1, int(round(win.width * scale)))
        idx = [look[b] for b in need]
        fill = ds.nodata if ds.nodata is not None else np.nan
        data = ds.read(idx, window=win, out_shape=(len(idx), out_h, out_w), boundless=bounds is not None,
                       fill_value=fill, resampling=Resampling.average if scale < 1 else Resampling.nearest)
        tf = ds.window_transform(win) * Affine.scale(win.width / out_w, win.height / out_h)
        crs = CRS.from_user_input(ds.crs.to_wkt())
        nodata = ds.nodata
    data = data.astype(np.float32)
    if nodata is not None and not np.isnan(nodata):
        data[data == nodata] = np.nan
    arr = _ix.compute_index(d, dict(zip(d.bands, data)))
    return arr, tf, crs


__all__ = [
    "INDEX_DEFS",
    "DEFAULT_INDICES",
    "IndexDef",
    "names",
    "get",
    "normalize",
    "available",
    "catalog",
    "legend",
    "compute",
    "compute_raster",
]
