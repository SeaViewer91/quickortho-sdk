"""산출물을 다른 라이브러리에 바로 넘길 수 있는 데이터 형식.

- 래스터: ``(array, transform, crs)`` 세 값. array는 numpy, transform은 ``affine.Affine``, crs는 ``pyproj.CRS``
- 카메라: :class:`CameraPose` (OpenCV 형식의 K·dist·R·t와 사진측량 형식의 ω·φ·κ)
- 점군: :class:`PointCloud` (xyz, rgb, 오차 배열)

좌표 규약
    지도 좌표는 결과 좌표계(``crs``)의 투영 좌표 (x 동쪽, y 북쪽, z 높이, 단위 m).
    SDK의 사진 좌표(``Mark``, ``CameraPose.project`` 등)는 좌상단 화소의 **왼쪽 위 모서리**가 원점이고
    좌상단 화소 중심이 (0.5, 0.5)임 (COLMAP 규약). OpenCV는 좌상단 화소 **중심**이 (0, 0)이므로,
    ``CameraPose.K``는 OpenCV 규약으로 맞춰 둠 (cx, cy에서 0.5를 뺌). OpenCV 함수 결과에 0.5를 더하면 SDK 좌표가 됨.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Iterator, Literal, Optional, Sequence

import numpy as np
from affine import Affine
from pyproj import CRS

from .errors import InputError, ProjectError

ChannelOrder = Literal["RGBA", "RGB", "BGRA", "BGR"]
Bounds = tuple[float, float, float, float]


# ───────────────────────── 래스터 ─────────────────────────


def _reorder(arr: np.ndarray, order: str) -> np.ndarray:
    """(H, W, 4) RGBA → 요청한 채널 순서."""
    order = order.upper()
    idx = {"RGBA": [0, 1, 2, 3], "RGB": [0, 1, 2], "BGRA": [2, 1, 0, 3], "BGR": [2, 1, 0]}.get(order)
    if idx is None:
        raise InputError(f"지원하지 않는 채널 순서: {order} (RGBA, RGB, BGRA, BGR 중 하나)", "invalid_argument")
    return np.ascontiguousarray(arr[..., idx])


def read_raster(
    path: str | Path,
    *,
    scale: float = 1.0,
    bounds: Optional[Bounds] = None,
    channels_last: bool = True,
) -> tuple[np.ndarray, Affine, CRS]:
    """GeoTIFF를 읽어 ``(array, transform, crs)``를 돌려줌.

    Args:
        path: GeoTIFF 경로.
        scale: 읽을 배율 (0 < scale ≤ 1). 0.25면 가로세로 1/4. COG 오버뷰를 쓰므로 빠름.
        bounds: 지도 좌표 범위 ``(xmin, ymin, xmax, ymax)``만 읽음. 결과 범위를 벗어난 부분은 0으로 채움.
        channels_last: ``True``면 ``(H, W, 밴드)``, ``False``면 rasterio처럼 ``(밴드, H, W)``. 밴드가 1개면 항상 ``(H, W)``.

    Returns:
        array, transform(읽은 배열 기준), crs
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    if not 0 < scale <= 1:
        raise InputError("scale은 0보다 크고 1 이하여야 함", "invalid_argument")
    p = Path(path).expanduser()
    if not p.exists():
        raise InputError(f"파일이 없음: {p}", "folder_not_found")
    with rasterio.open(p) as ds:
        if bounds is not None:
            win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
        else:
            win = rasterio.windows.Window(0, 0, ds.width, ds.height)
        out_h = max(1, int(round(win.height * scale)))
        out_w = max(1, int(round(win.width * scale)))
        data = ds.read(window=win, out_shape=(ds.count, out_h, out_w), boundless=bounds is not None,
                       fill_value=0, resampling=Resampling.average if scale < 1 else Resampling.nearest)
        tf = ds.window_transform(win) * Affine.scale(win.width / out_w, win.height / out_h)
        crs = CRS.from_user_input(ds.crs.to_wkt())
    if data.shape[0] == 1:
        arr = data[0]
    else:
        arr = np.moveaxis(data, 0, -1) if channels_last else data
    return np.ascontiguousarray(arr), tf, crs


def _resolve_raster(target: str | Path, name: str) -> Path:
    p = Path(target).expanduser()
    if p.is_dir():
        p = p / name
    if not p.exists():
        raise ProjectError(f"{name}이 없음: {p}. orthomosaic() 또는 process()를 먼저 실행해야 함", "not_rendered")
    return p


def read_orthomosaic(
    target: str | Path,
    *,
    scale: float = 1.0,
    bounds: Optional[Bounds] = None,
    order: ChannelOrder = "RGBA",
) -> tuple[np.ndarray, Affine, CRS]:
    """정사 모자이크를 ``(image, transform, crs)``로 읽음.

    Args:
        target: 워크스페이스 폴더 또는 ``orthomosaic.tif`` 경로.
        scale: 읽을 배율 (0 < scale ≤ 1).
        bounds: 지도 좌표 범위만 읽음 ``(xmin, ymin, xmax, ymax)``.
        order: 채널 순서. OpenCV에 넘길 때는 ``"BGR"`` 또는 ``"BGRA"``.

    Returns:
        image: ``(H, W, C)`` uint8. RGBA면 알파 0이 자료 없음.
        transform: 화소(열, 행) → 지도(x, y) 변환. ``x, y = transform * (col, row)``
        crs: 좌표계
    """
    arr, tf, crs = read_raster(_resolve_raster(target, "orthomosaic.tif"), scale=scale, bounds=bounds)
    return _reorder(arr, order), tf, crs


def read_dsm(
    target: str | Path, *, scale: float = 1.0, bounds: Optional[Bounds] = None
) -> tuple[np.ndarray, Affine, CRS]:
    """간이 DSM을 ``(z, transform, crs)``로 읽음. z는 ``(H, W)`` float32 (m)."""
    arr, tf, crs = read_raster(_resolve_raster(target, "dsm.tif"), scale=scale, bounds=bounds)
    return arr.astype(np.float32, copy=False), tf, crs


# ───────────────────────── 카메라 ─────────────────────────


def _opencv_intrinsics(model: str, params: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """COLMAP 카메라 모델 → OpenCV (K, dist). K는 OpenCV 화소 규약(중심이 정수)."""
    p = [float(v) for v in params]
    if model in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"):
        fx = fy = p[0]
        cx, cy = p[1], p[2]
        k = p[3:]
        dist = np.array([k[0] if len(k) > 0 else 0.0, k[1] if len(k) > 1 else 0.0, 0.0, 0.0])
    elif model in ("PINHOLE", "OPENCV", "FULL_OPENCV"):
        fx, fy, cx, cy = p[:4]
        rest = p[4:]
        if model == "PINHOLE":
            dist = np.zeros(4)
        elif model == "OPENCV":
            dist = np.array(rest[:4])  # k1, k2, p1, p2
        else:  # FULL_OPENCV: k1, k2, p1, p2, k3, k4, k5, k6
            dist = np.array(rest[:8])
    else:
        raise InputError(f"OpenCV 형식으로 바꿀 수 없는 카메라 모델: {model}", "invalid_argument")
    K = np.array([[fx, 0.0, cx - 0.5], [0.0, fy, cy - 0.5], [0.0, 0.0, 1.0]])
    return K, dist


def _opk_from_R(R: np.ndarray) -> np.ndarray:
    """COLMAP R(세계→카메라, 카메라 x 오른쪽·y 아래·z 앞) → ω, φ, κ (도).

    사진측량 표준(Wolf) 정의: M = Mκ·Mφ·Mω (세계→사진). 사진 좌표계는 x 오른쪽, y 위, z 뒤(투영 중심 쪽).
    """
    M = np.diag([1.0, -1.0, -1.0]) @ R
    phi = np.arcsin(np.clip(M[2, 0], -1.0, 1.0))
    omega = np.arctan2(-M[2, 1], M[2, 2])
    kappa = np.arctan2(-M[1, 0], M[0, 0])
    return np.degrees([omega, phi, kappa])


def rotation_from_opk(omega: float, phi: float, kappa: float) -> np.ndarray:
    """ω, φ, κ(도) → COLMAP·OpenCV 형식 R(세계→카메라). :attr:`CameraPose.opk`의 역변환."""
    w, p, k = np.radians([omega, phi, kappa])
    Mw = np.array([[1, 0, 0], [0, np.cos(w), np.sin(w)], [0, -np.sin(w), np.cos(w)]])
    Mp = np.array([[np.cos(p), 0, -np.sin(p)], [0, 1, 0], [np.sin(p), 0, np.cos(p)]])
    Mk = np.array([[np.cos(k), np.sin(k), 0], [-np.sin(k), np.cos(k), 0], [0, 0, 1]])
    return np.diag([1.0, -1.0, -1.0]) @ (Mk @ Mp @ Mw)


@dataclass(frozen=True)
class CameraPose:
    """정합된 영상 한 장의 카메라 내부·외부표정.

    OpenCV 함수에 바로 넣을 수 있도록 ``K``, ``dist``, ``rvec``, ``t``를 제공함.
    좌표는 결과 좌표계(``epsg``)의 지도 좌표임.

    예::

        uv, _ = cv2.projectPoints(xyz, cam.rvec, cam.t, cam.K, cam.dist)   # OpenCV 화소 규약
        und = cv2.undistort(img, cam.K, cam.dist)

    Attributes:
        name: 사진 파일 이름.
        width, height: 사진 크기(px).
        model: COLMAP 카메라 모델 이름 (보통 ``"OPENCV"``).
        params: COLMAP 카메라 파라미터 원본 (COLMAP 화소 규약).
        K: 3×3 카메라 행렬 (OpenCV 화소 규약).
        dist: OpenCV 왜곡 계수 ``(k1, k2, p1, p2[, k3, k4, k5, k6])``.
        R: 3×3 회전 (지도 → 카메라). 카메라 좌표는 x 오른쪽, y 아래, z 시선 방향.
        t: 이동 (지도 → 카메라). ``X_cam = R @ X + t``.
        center: 카메라 위치 (지도 좌표, 3,).
        epsg: 좌표계 EPSG.
    """

    name: str
    width: int
    height: int
    model: str
    params: np.ndarray = field(repr=False)
    K: np.ndarray = field(repr=False)
    dist: np.ndarray = field(repr=False)
    R: np.ndarray = field(repr=False)
    t: np.ndarray = field(repr=False)
    center: np.ndarray
    epsg: int

    @property
    def rvec(self) -> np.ndarray:
        """OpenCV 회전 벡터 (Rodrigues, 3,)."""
        import cv2

        return cv2.Rodrigues(self.R)[0].ravel()

    @property
    def opk(self) -> np.ndarray:
        """사진측량 자세각 ω, φ, κ (도). 연직·영상 위쪽이 북쪽이면 (0, 0, 0)."""
        return _opk_from_R(self.R)

    @property
    def P(self) -> np.ndarray:
        """3×4 투영 행렬 ``K @ [R | t]`` (왜곡 제외, OpenCV 화소 규약)."""
        return self.K @ np.column_stack([self.R, self.t])

    def to_camera(self, xyz: np.ndarray) -> np.ndarray:
        """지도 좌표 (N, 3) → 카메라 좌표 (N, 3)."""
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        return xyz @ self.R.T + self.t

    def project(self, xyz: np.ndarray) -> np.ndarray:
        """지도 좌표 (N, 3) → 사진 좌표 (N, 2), SDK 화소 규약 (왜곡 포함).

        카메라 뒤쪽 점은 NaN. 사진 범위 밖 좌표도 그대로 돌려주므로 필요하면 ``in_image()``로 거름.
        """
        import cv2

        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        if len(xyz) == 0:
            return np.empty((0, 2))
        uv, _ = cv2.projectPoints(xyz.reshape(-1, 1, 3), self.rvec, self.t, self.K, self.dist)
        uv = uv.reshape(-1, 2) + 0.5
        uv[self.to_camera(xyz)[:, 2] <= 0] = np.nan
        return uv

    def in_image(self, uv: np.ndarray) -> np.ndarray:
        """사진 좌표 (N, 2)가 사진 안에 있는지 (N,) bool."""
        uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2)
        return (uv[:, 0] >= 0) & (uv[:, 0] < self.width) & (uv[:, 1] >= 0) & (uv[:, 1] < self.height)

    def rays(self, uv: np.ndarray) -> np.ndarray:
        """사진 좌표 (N, 2, SDK 규약) → 지도 좌표계의 단위 광선 방향 (N, 3). 왜곡을 풀어서 계산함."""
        import cv2

        uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2) - 0.5
        if len(uv) == 0:
            return np.empty((0, 3))
        n = cv2.undistortPoints(uv.reshape(-1, 1, 2), self.K, self.dist).reshape(-1, 2)
        d = np.column_stack([n, np.ones(len(n))]) @ self.R  # R^T · d
        return d / np.linalg.norm(d, axis=1, keepdims=True)

    def to_dict(self) -> dict:
        """JSON으로 저장할 수 있는 dict."""
        return {
            "name": self.name, "width": self.width, "height": self.height, "model": self.model,
            "params": self.params.tolist(), "K": self.K.tolist(), "dist": self.dist.tolist(),
            "R": self.R.tolist(), "t": self.t.tolist(), "center": self.center.tolist(),
            "opk_deg": self.opk.tolist(), "epsg": self.epsg,
        }


class Cameras(list):
    """:class:`CameraPose` 목록. 이름으로도 찾을 수 있음 (``cameras["DJI_0013.JPG"]``)."""

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            for c in self:
                if c.name == key:
                    return c
            raise KeyError(key)
        return super().__getitem__(key)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self]

    @property
    def centers(self) -> np.ndarray:
        """카메라 위치 (N, 3)."""
        return np.array([c.center for c in self]).reshape(-1, 3)

    @property
    def opk(self) -> np.ndarray:
        """자세각 (N, 3) [ω, φ, κ] (도)."""
        return np.array([c.opk for c in self]).reshape(-1, 3)

    def to_csv(self, path: str | Path) -> Path:
        """영상별 위치·자세를 CSV로 저장 (name, x, y, z, omega, phi, kappa). 사진측량 SW 입력용."""
        path = Path(path)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("name,x,y,z,omega,phi,kappa\n")
            for c in self:
                o, p, k = c.opk
                f.write(f"{c.name},{c.center[0]:.4f},{c.center[1]:.4f},{c.center[2]:.4f},{o:.6f},{p:.6f},{k:.6f}\n")
        return path


# ───────────────────────── 점군 ─────────────────────────


@dataclass(frozen=True)
class PointCloud:
    """희소 점군 (SfM 타이포인트).

    풀어서 받으면 ``xyz, rgb = points``.

    Attributes:
        xyz: (N, 3) float64 지도 좌표.
        rgb: (N, 3) uint8 색.
        error: (N,) float32 재투영 오차(px).
        track_length: (N,) int32 관측 사진 수.
        ids: (N,) int64 점 ID (``set_edits(deleted_points=...)``에 쓰는 값).
        epsg: 좌표계 EPSG.
    """

    xyz: np.ndarray = field(repr=False)
    rgb: np.ndarray = field(repr=False)
    error: np.ndarray = field(repr=False)
    track_length: np.ndarray = field(repr=False)
    ids: np.ndarray = field(repr=False)
    epsg: int = 0

    def __len__(self) -> int:
        return len(self.xyz)

    def __iter__(self) -> Iterator[np.ndarray]:
        yield self.xyz
        yield self.rgb

    def __repr__(self) -> str:
        return f"PointCloud(n={len(self)}, epsg={self.epsg})"

    def filter(self, max_error_px: Optional[float] = None, min_track_length: Optional[int] = None) -> "PointCloud":
        """조건에 맞는 점만 남긴 새 점군."""
        keep = np.ones(len(self), bool)
        if max_error_px is not None:
            keep &= self.error <= max_error_px
        if min_track_length is not None:
            keep &= self.track_length >= min_track_length
        return PointCloud(self.xyz[keep], self.rgb[keep], self.error[keep], self.track_length[keep],
                          self.ids[keep], self.epsg)

    def to_ply(self, path: str | Path) -> Path:
        """이진 PLY로 저장 (x, y, z, red, green, blue). CloudCompare·MeshLab·Open3D에서 열 수 있음.

        좌표값이 커서(UTM) 일부 뷰어에서 정밀도가 떨어질 수 있으므로 double로 저장함.
        """
        path = Path(path)
        n = len(self)
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            f"comment epsg {self.epsg}\nelement vertex {n}\n"
            "property double x\nproperty double y\nproperty double z\n"
            "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
        )
        rec = np.empty(n, dtype=[("x", "<f8"), ("y", "<f8"), ("z", "<f8"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
        rec["x"], rec["y"], rec["z"] = self.xyz[:, 0], self.xyz[:, 1], self.xyz[:, 2]
        rec["r"], rec["g"], rec["b"] = self.rgb[:, 0], self.rgb[:, 1], self.rgb[:, 2]
        with open(path, "wb") as f:
            f.write(header.encode("ascii"))
            f.write(rec.tobytes())
        return path


# ───────────────────────── 재구성 → 카메라·점군 ─────────────────────────


def cameras_from_reconstruction(rec, origin: Sequence[float], epsg: int) -> Cameras:
    """지역 좌표계 pycolmap 재구성 → 지도 좌표 CameraPose 목록 (이름순)."""
    o = np.asarray(origin, dtype=np.float64)
    out = Cameras()
    for iid in sorted(rec.reg_image_ids(), key=lambda i: rec.image(i).name):
        im = rec.image(iid)
        cam = rec.cameras[im.camera_id]
        cfw = im.cam_from_world()
        R = np.asarray(cfw.rotation.matrix(), dtype=np.float64)
        t_local = np.asarray(cfw.translation, dtype=np.float64)
        t = t_local - R @ o  # X_local = X - o
        K, dist = _opencv_intrinsics(cam.model.name, np.asarray(cam.params))
        out.append(CameraPose(
            name=im.name, width=int(cam.width), height=int(cam.height), model=cam.model.name,
            params=np.asarray(cam.params, dtype=np.float64).copy(), K=K, dist=dist, R=R, t=t,
            center=np.asarray(im.projection_center(), dtype=np.float64) + o, epsg=int(epsg),
        ))
    return out


def points_from_reconstruction(rec, origin: Sequence[float], epsg: int) -> PointCloud:
    o = np.asarray(origin, dtype=np.float64)
    ids = np.fromiter(rec.points3D.keys(), dtype=np.int64, count=len(rec.points3D))
    ids.sort()
    n = len(ids)
    xyz = np.empty((n, 3))
    rgb = np.empty((n, 3), np.uint8)
    err = np.empty(n, np.float32)
    trk = np.empty(n, np.int32)
    for i, pid in enumerate(ids):
        p = rec.point3D(int(pid))
        xyz[i] = p.xyz
        rgb[i] = p.color
        err[i] = p.error
        trk[i] = p.track.length()
    return PointCloud(xyz + o, rgb, err, trk, ids, int(epsg))


__all__ = [
    "read_raster", "read_orthomosaic", "read_dsm",
    "CameraPose", "Cameras", "PointCloud", "rotation_from_opk",
]
