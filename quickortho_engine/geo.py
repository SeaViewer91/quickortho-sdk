"""좌표계 유틸리티: WGS84 ↔ 투영 좌표계(UTM, 한국 TM 등) 변환."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer


def utm_epsg(lon: float, lat: float) -> int:
    """경위도가 속한 UTM 구역의 EPSG 코드 (WGS84 기준)."""
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=64)
def _transformer(src: int, dst: int) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


@lru_cache(maxsize=64)
def crs_info(epsg: int) -> dict:
    crs = CRS.from_epsg(epsg)
    return {"epsg": epsg, "name": crs.name, "geographic": bool(crs.is_geographic)}


def is_geographic(epsg: int) -> bool:
    return crs_info(epsg)["geographic"]


def transform_xy(x, y, src_epsg: int, dst_epsg: int) -> tuple[np.ndarray, np.ndarray]:
    """평면(수평) 좌표만 변환한다. 경위도 좌표계는 (경도, 위도) 순서."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if src_epsg == dst_epsg:
        return x.copy(), y.copy()
    a, b = _transformer(src_epsg, dst_epsg).transform(x, y)
    return np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)


class Projector:
    """WGS84 경위도를 지정한 투영 좌표계(m)로 변환한다."""

    def __init__(self, epsg: int) -> None:
        self.epsg = int(epsg)
        self.crs = CRS.from_epsg(self.epsg)

    def forward(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        return transform_xy(lon, lat, 4326, self.epsg)

    def inverse(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        return transform_xy(x, y, self.epsg, 4326)


class UtmProjector(Projector):
    """경위도 기준점이 속한 UTM 구역으로 변환한다."""

    def __init__(self, lon0: float, lat0: float) -> None:
        super().__init__(utm_epsg(lon0, lat0))
