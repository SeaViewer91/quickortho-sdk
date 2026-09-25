"""좌표 정렬(SfM → UTM)과 희소 점군 기반 간이 DSM."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pycolmap
from scipy import ndimage
from scipy.interpolate import griddata
from scipy.spatial import cKDTree

from .geo import UtmProjector


@dataclass
class GeoResult:
    projector: UtmProjector
    gps_residual_m: float  # 정렬 후 카메라 위치와 GPS의 RMS 수평 차이
    num_aligned: int


def georeference(
    rec: pycolmap.Reconstruction,
    gps: dict[str, tuple[float, float, float]],
    ransac_max_error_m: float = 10.0,
) -> GeoResult:
    """카메라 중심을 GPS 위치(UTM)에 맞추는 닮음변환(Sim3)을 RANSAC으로 추정해 적용한다.

    gps: 영상 이름 → (lon, lat, alt). RTK가 없는 일반 GNSS는 수 m 오차가 있으므로
    RANSAC 임계값을 넉넉하게 둔다. 모자이크 내부 정합은 SfM이 담당한다.
    """
    names = [img.name for img in rec.images.values() if img.name in gps and img.has_pose]
    if len(names) < 3:
        raise RuntimeError("좌표 정렬 실패: GPS가 있는 정합 영상이 3장 미만임")
    lon = np.array([gps[n][0] for n in names])
    lat = np.array([gps[n][1] for n in names])
    alt = np.array([gps[n][2] for n in names])
    proj = UtmProjector(float(np.median(lon)), float(np.median(lat)))
    e, n_ = proj.forward(lon, lat)
    tgt = np.column_stack([e, n_, alt])

    # UTM 좌표는 값이 커서 수치 안정성을 위해 원점 이동 후 정렬하고, 이후 다시 더한다
    origin = np.array([np.median(e), np.median(n_), 0.0])
    ransac = pycolmap.RANSACOptions()
    ransac.max_error = ransac_max_error_m
    sim3 = pycolmap.align_reconstruction_to_locations(rec, names, tgt - origin, 3, ransac)
    if sim3 is None:
        raise RuntimeError("좌표 정렬 실패: GPS와 SfM 카메라 배치가 일치하지 않음")
    rec.transform(sim3)
    rec.transform(pycolmap.Sim3d(1.0, pycolmap.Rotation3d(), origin))

    centers = np.array([rec.find_image_with_name(n).projection_center() for n in names])
    residual = float(np.sqrt(np.mean(np.sum((centers[:, :2] - tgt[:, :2]) ** 2, axis=1))))
    return GeoResult(projector=proj, gps_residual_m=residual, num_aligned=len(names))


@dataclass
class Dsm:
    z: np.ndarray  # (rows, cols) 표고, 북쪽이 위
    x0: float  # 좌상단 격자점 x
    y0: float  # 좌상단 격자점 y
    res: float  # 격자 간격(m)
    num_points: int

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """쌍선형 보간으로 표고를 샘플링한다 (범위 밖은 가장자리 값)."""
        col = (x - self.x0) / self.res
        row = (self.y0 - y) / self.res
        return ndimage.map_coordinates(self.z, [row.ravel(), col.ravel()], order=1, mode="nearest").reshape(
            np.shape(x)
        )


def sparse_points(rec: pycolmap.Reconstruction, max_error_px: float = 2.0, min_track: int = 3) -> np.ndarray:
    pts = [
        p.xyz
        for p in rec.points3D.values()
        if p.error <= max_error_px and p.track.length() >= min_track
    ]
    return np.asarray(pts, dtype=np.float64).reshape(-1, 3)


def remove_z_outliers(pts: np.ndarray, k: int = 16, n_mad: float = 3.0) -> np.ndarray:
    """주변 k개 점의 표고 중앙값에서 크게 벗어난 점(날아간 점)을 제거한다."""
    if len(pts) <= k:
        return pts
    tree = cKDTree(pts[:, :2])
    _, idx = tree.query(pts[:, :2], k=k + 1)
    local = pts[idx[:, 1:], 2]
    med = np.median(local, axis=1)
    mad = np.median(np.abs(local - med[:, None]), axis=1) * 1.4826 + 0.05
    keep = np.abs(pts[:, 2] - med) <= n_mad * mad
    return pts[keep]


def build_dsm(
    pts: np.ndarray,
    bounds: tuple[float, float, float, float],
    res: float,
    smooth_px: int = 5,
) -> Dsm:
    """희소 점군을 격자로 보간해 간이 DSM을 만든다.

    bounds: (xmin, ymin, xmax, ymax). 점군 볼록껍질 밖은 최근접 값으로 채운다.
    정사영상 생성용 저해상도 지형면이므로 중앙값 필터로 뾰족한 잡음을 누른다.
    """
    xmin, ymin, xmax, ymax = bounds
    cols = max(2, int(np.ceil((xmax - xmin) / res)) + 1)
    rows = max(2, int(np.ceil((ymax - ymin) / res)) + 1)
    gx = xmin + np.arange(cols) * res
    gy = ymax - np.arange(rows) * res
    X, Y = np.meshgrid(gx, gy)
    if len(pts) < 3:
        z0 = float(np.median(pts[:, 2])) if len(pts) else 0.0
        z = np.full((rows, cols), z0)
    else:
        z = griddata(pts[:, :2], pts[:, 2], (X, Y), method="linear")
        if smooth_px > 1:
            known = ~np.isnan(z)
            z_med = ndimage.median_filter(np.where(known, z, np.nanmedian(z)), size=smooth_px, mode="nearest")
            z = np.where(known, z_med, np.nan)
        z = _fill_smooth(z)
        lo, hi = np.percentile(pts[:, 2], [1, 99])
        z = np.clip(z, lo, hi)
    # 급격한 높이 변화는 정사영상에 번짐을 만들므로 한 번 더 부드럽게 만든다
    z = ndimage.gaussian_filter(z, sigma=1.0, mode="nearest")
    return Dsm(z=z.astype(np.float32), x0=float(gx[0]), y0=float(gy[0]), res=res, num_points=len(pts))


def _fill_smooth(z: np.ndarray) -> np.ndarray:
    """점군 볼록껍질 밖(NaN)을 정규화 가우시안 합성곱으로 점점 넓게 부드럽게 채운다.

    최근접 값으로 채우면 쐐기 모양의 높이 단차가 생겨 정사영상에 소용돌이 번짐이 생긴다.
    """
    known = ~np.isnan(z)
    if known.all():
        return z
    if not known.any():
        return np.zeros_like(z)
    out = z.copy()
    vals = np.where(known, z, 0.0)
    w = known.astype(np.float64)
    sigma = 2.0
    while np.isnan(out).any():
        num = ndimage.gaussian_filter(vals, sigma, mode="nearest")
        den = ndimage.gaussian_filter(w, sigma, mode="nearest")
        est = np.divide(num, den, out=np.full_like(num, np.nan), where=den > 1e-6)
        hole = np.isnan(out) & ~np.isnan(est)
        out[hole] = est[hole]
        sigma *= 2.0
        if sigma > 4 * max(z.shape):
            out[np.isnan(out)] = np.nanmedian(z)
            break
    return out
