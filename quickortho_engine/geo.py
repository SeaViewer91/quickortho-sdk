"""좌표계 유틸리티: WGS84 → UTM 변환."""

from __future__ import annotations

import numpy as np
from pyproj import CRS, Transformer


def utm_epsg(lon: float, lat: float) -> int:
    """경위도가 속한 UTM 구역의 EPSG 코드 (WGS84 기준)."""
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


class UtmProjector:
    """WGS84 경위도를 해당 지역 UTM(m) 좌표로 변환한다."""

    def __init__(self, lon0: float, lat0: float) -> None:
        self.epsg = utm_epsg(lon0, lat0)
        self.crs = CRS.from_epsg(self.epsg)
        self._fwd = Transformer.from_crs(4326, self.epsg, always_xy=True)

    def forward(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        e, n = self._fwd.transform(np.asarray(lon, float), np.asarray(lat, float))
        return np.asarray(e), np.asarray(n)
