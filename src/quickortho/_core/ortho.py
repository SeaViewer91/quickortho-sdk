"""정사투영: 출력 타일 단위로 각 영상을 DSM에 역투영하고, 화소마다 영상 하나를 골라 칠한다 (심라인 합성).

메모리 상한(M1 8GB 기준)을 지키기 위해
- 출력은 타일(기본 512px) 단위로 계산해 바로 파일에 쓰고,
- 원본 영상은 출력 해상도에 맞게 읽으며(출력이 원본보다 거칠면 JPEG DCT 축소 활용),
- 읽은 영상은 메모리 예산 안에서만 LRU 캐시에 둔다.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import rasterio
import rasterio.shutil
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.windows import Window

from .protocol import Emitter
from .surface import Dsm


@dataclass
class View:
    name: str
    camera: pycolmap.Camera
    R: np.ndarray  # world → cam 회전
    t: np.ndarray  # world → cam 이동
    center: np.ndarray
    bbox: tuple[float, float, float, float]  # 지면 footprint 경계 (xmin, ymin, xmax, ymax)


def _footprint(cam: pycolmap.Camera, R: np.ndarray, center: np.ndarray, z0: float) -> np.ndarray | None:
    """영상 테두리를 따라 광선을 쏘아 z=z0 평면과의 교점을 구한다."""
    w, h = cam.width, cam.height
    s = np.linspace(0, 1, 9)
    border = np.concatenate(
        [
            np.column_stack([s * w, np.zeros_like(s)]),
            np.column_stack([np.full_like(s, w), s * h]),
            np.column_stack([s * w, np.full_like(s, h)]),
            np.column_stack([np.zeros_like(s), s * h]),
        ]
    )
    xy = cam.cam_from_img(border)
    rays = np.column_stack([xy, np.ones(len(xy))]) @ R  # cam → world 방향 (R^T · d)
    dz = rays[:, 2]
    ok = dz < -1e-6  # 아래를 향하는 광선만
    if ok.sum() < 3:
        return None
    tt = (z0 - center[2]) / dz[ok]
    pts = center[None, :2] + tt[:, None] * rays[ok, :2]
    return pts


def make_view(
    name: str, cam: pycolmap.Camera, R: np.ndarray, t: np.ndarray, ground_z: float,
    max_range_factor: float = 4.0,
) -> View | None:
    center = -R.T @ t
    fp = _footprint(cam, R, center, ground_z)
    if fp is None:
        return None
    # 경사 촬영으로 지평선 쪽 광선이 멀리 뻗는 경우를 비행고도 기준으로 제한
    h = max(center[2] - ground_z, 1.0)
    d = fp - center[None, :2]
    dist = np.linalg.norm(d, axis=1, keepdims=True)
    lim = max_range_factor * h
    fp = center[None, :2] + d * np.minimum(1.0, lim / np.maximum(dist, 1e-9))
    bbox = (fp[:, 0].min(), fp[:, 1].min(), fp[:, 0].max(), fp[:, 1].max())
    return View(name, cam, R, t, center, bbox)


def build_views(rec: pycolmap.Reconstruction, ground_z: float, max_range_factor: float = 4.0) -> list[View]:
    views: list[View] = []
    for img in rec.images.values():
        if not img.has_pose:
            continue
        cfw = img.cam_from_world()
        v = make_view(
            img.name,
            rec.cameras[img.camera_id],
            cfw.rotation.matrix(),
            np.asarray(cfw.translation, dtype=np.float64),
            ground_z,
            max_range_factor,
        )
        if v is not None:
            views.append(v)
    return views


def native_gsd(views: list[View], ground_z: float) -> float:
    g = [max(v.center[2] - ground_z, 1.0) / v.camera.mean_focal_length() for v in views]
    return float(np.median(g))


class ImageCache:
    """출력 해상도에 맞게 축소한 영상을 메모리 예산 안에서 보관한다."""

    def __init__(self, image_dir: Path, scale: float, budget_bytes: int) -> None:
        self.image_dir = image_dir
        self.scale = min(1.0, scale)
        self.budget = budget_bytes
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._bytes = 0

    def get(self, name: str, full_w: int, full_h: int) -> np.ndarray:
        if name in self._items:
            self._items.move_to_end(name)
            return self._items[name]
        tw = max(1, round(full_w * self.scale))
        th = max(1, round(full_h * self.scale))
        with Image.open(self.image_dir / name) as im:
            im.draft("RGB", (tw, th))  # JPEG은 디코딩 단계에서 1/2·1/4·1/8 축소
            im = im.convert("RGB")
            if im.size != (tw, th):
                im = im.resize((tw, th), Image.Resampling.BILINEAR)
            arr = np.asarray(im)
        self._items[name] = arr
        self._bytes += arr.nbytes
        while self._bytes > self.budget and len(self._items) > 1:
            _, old = self._items.popitem(last=False)
            self._bytes -= old.nbytes
        return arr


def render_orthomosaic(
    views: list[View],
    dsm: Dsm,
    image_dir: Path,
    out_path: Path,
    crs,
    gsd: float,
    src_gsd: float,
    bounds: tuple[float, float, float, float],
    out: Emitter,
    tile: int = 512,
    feather_px: float = 6.0,
    cache_budget_mb: int | None = None,
    block: int = 4,
) -> dict:
    """화소마다 연직에 가장 가까운 영상 하나로 칠하고, 영상 사이 경계에서만 좁게 섞는다 (seamline 방식).

    여러 영상을 가중 평균하면 DSM 높이 오차·움직이는 물체 때문에 영상끼리 조금만 어긋나도 상이 겹치고 흐려진다.
    영상 하나를 고르면 어긋남은 경계선의 작은 단차로만 남는다.

    - 점수: 화소가 영상 중심에서 떨어진 거리(원본 화소). 가까울수록 연직에 가깝고 렌즈 왜곡·경사가 작다
    - 경계: 점수 차가 작은 곳(경계선 양쪽 약 feather_px 출력 화소)만 지수 가중으로 섞는다
    - 화소마다 같은 규칙으로 정하므로 타일 경계에서도 결과가 이어진다
    - 타일은 block×block 묶음 단위로 처리해 같은 영상을 연달아 쓰게 한다 (원본 해상도 영상 캐시 재사용)
    """
    xmin, ymin, xmax, ymax = bounds
    width = int(math.ceil((xmax - xmin) / gsd))
    height = int(math.ceil((ymax - ymin) / gsd))
    transform = from_origin(xmin, ymax, gsd, gsd)
    if cache_budget_mb is None:
        cache_budget_mb = default_cache_budget_mb()
    cache = ImageCache(image_dir, src_gsd / gsd, cache_budget_mb * 1024 * 1024)
    # 점수(원본 화소 단위)의 온도. 경계선을 가로지르면 두 영상의 점수 차가 출력 1화소당 최대 약 2·gsd/src_gsd 변함
    temp = max(feather_px * (gsd / src_gsd) / 1.5, 1e-3)

    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 4,
        "dtype": "uint8",
        "crs": crs,
        "transform": transform,
        "tiled": True,
        "blockxsize": tile,
        "blockysize": tile,
        "compress": "deflate",
        "predictor": 2,
        "photometric": "RGB",
        "alpha": "unassociated",
        "BIGTIFF": "IF_SAFER",
        "sparse_ok": True,
    }
    vb = np.array([v.bbox for v in views])
    n_tx = math.ceil(width / tile)
    n_ty = math.ceil(height / tile)
    total = n_tx * n_ty
    done = 0
    step = max(1, total // 100)
    covered = 0
    order = [
        (ty, tx)
        for by in range(0, n_ty, block)
        for bx in range(0, n_tx, block)
        for ty in range(by, min(by + block, n_ty))
        for tx in range(bx, min(bx + block, n_tx))
    ]

    with rasterio.open(out_path, "w", **profile) as dst:
        for ty, tx in order:
            c0, r0 = tx * tile, ty * tile
            tw, th = min(tile, width - c0), min(tile, height - r0)
            bx0 = xmin + c0 * gsd
            by1 = ymax - r0 * gsd
            bx1, by0 = bx0 + tw * gsd, by1 - th * gsd
            cand = np.nonzero(
                (vb[:, 0] < bx1) & (vb[:, 2] > bx0) & (vb[:, 1] < by1) & (vb[:, 3] > by0)
            )[0]
            if len(cand):
                rgba = _render_tile([views[i] for i in cand], dsm, cache, bx0, by1, gsd, tw, th, temp)
                if rgba is not None:
                    covered += int((rgba[3] > 0).sum())
                    dst.write(rgba, window=Window(c0, r0, tw, th))
            done += 1
            if done % step == 0 or done == total:
                out.progress("ortho", done, total)

    return {
        "width": width,
        "height": height,
        "gsd_m": gsd,
        "covered_area_m2": covered * gsd * gsd,
        "tiles": total,
        "blend": "seamline",
    }


def default_cache_budget_mb() -> int:
    """원본 해상도 영상 캐시 크기: 사용 가능한 메모리의 약 30% (최소 600 MB, 최대 3 GB)."""
    try:
        import psutil

        avail = psutil.virtual_memory().available / 1024 / 1024
    except Exception:  # noqa: BLE001
        return 600
    return int(min(3072, max(600, avail * 0.3)))


def _project(v: View, world: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """월드 좌표를 영상에 투영한다. 반환: (u, v, 영상 안 여부, 영상 중심까지 거리(원본 화소))"""
    pc = world @ v.R.T + v.t
    front = pc[:, 2] > 0
    uv = np.full((len(pc), 2), -1.0)
    if front.any():
        uv[front] = v.camera.img_from_cam(pc[front], check_cheirality=False)
    W, H = v.camera.width, v.camera.height
    u, vv = uv[:, 0], uv[:, 1]
    inside = front & (u >= 0.5) & (u < W - 0.5) & (vv >= 0.5) & (vv < H - 0.5)
    r = np.hypot(u - W / 2, vv - H / 2)
    return u, vv, inside, r


def _blend_weights(r: np.ndarray, inside: np.ndarray, temp: float) -> np.ndarray:
    """(영상, 점) 거리 → 정규화하지 않은 가중치. 가장 가까운 영상이 1, 경계 근처만 0보다 큰 값을 가진다."""
    rr = np.where(inside, r, np.inf)
    best = rr.min(axis=0)
    best = np.where(np.isfinite(best), best, 0.0)  # 어느 영상에도 없는 점
    w = np.exp(-(rr - best) / temp)
    w[~inside] = 0.0
    w[w < 1e-3] = 0.0
    return w.astype(np.float32)


def _select_views(
    views: list[View], dsm: Dsm, x0: float, y1: float, gsd: float, tw: int, th: int, temp: float,
    grid: int = 33,
) -> list[View]:
    """거친 격자에서 가중치가 있는 영상만 남긴다 (영상 하나로 칠하므로 타일마다 보통 1~4장)."""
    if len(views) <= 1:
        return views
    gx = x0 + np.linspace(0, tw, grid) * gsd
    gy = y1 - np.linspace(0, th, grid) * gsd
    X, Y = np.meshgrid(gx, gy)
    world = np.column_stack([X.ravel(), Y.ravel(), dsm.sample(X, Y).ravel()])
    proj = [_project(v, world) for v in views]
    r = np.stack([p[3] for p in proj])
    inside = np.stack([p[2] for p in proj])
    # 격자 사이에서 경계가 지나갈 수 있으므로 온도를 넉넉히 잡아 이웃 영상도 남긴다
    w = _blend_weights(r, inside, temp * 4)
    keep = (w > 0).any(axis=1)
    return [v for v, k in zip(views, keep) if k]


def _sample(img: np.ndarray, x: np.ndarray, y: np.ndarray, width: int = 1024) -> np.ndarray:
    """흩어진 좌표를 쌍선형 보간으로 샘플링한다. cv2.remap은 한 변이 32767 미만이어야 하므로 2차원으로 접어서 쓴다."""
    n = len(x)
    rows = -(-n // width)
    pad = rows * width - n
    mx = np.pad(x.astype(np.float32), (0, pad)).reshape(rows, width)
    my = np.pad(y.astype(np.float32), (0, pad)).reshape(rows, width)
    col = cv2.remap(img, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return col.reshape(-1, 3)[:n].astype(np.float32)


def _render_tile(
    views: list[View],
    dsm: Dsm,
    cache: ImageCache,
    x0: float,
    y1: float,
    gsd: float,
    tw: int,
    th: int,
    temp: float,
) -> np.ndarray | None:
    views = _select_views(views, dsm, x0, y1, gsd, tw, th, temp)
    if not views:
        return None
    xs = x0 + (np.arange(tw) + 0.5) * gsd
    ys = y1 - (np.arange(th) + 0.5) * gsd
    X, Y = np.meshgrid(xs, ys)
    Z = dsm.sample(X, Y)
    world = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])

    proj = [_project(v, world) for v in views]
    w_all = _blend_weights(np.stack([p[3] for p in proj]), np.stack([p[2] for p in proj]), temp)
    acc = np.zeros((th * tw, 3), dtype=np.float32)
    wsum = np.zeros(th * tw, dtype=np.float32)
    for v, (u, vv, _, _), w in zip(views, proj, w_all):
        idx = np.nonzero(w > 0)[0]
        if not len(idx):
            continue
        W, H = v.camera.width, v.camera.height
        img = cache.get(v.name, W, H)
        sx = img.shape[1] / W
        sy = img.shape[0] / H
        # 이 영상이 쓰이는 화소만 다시 샘플링 (영상 하나로 칠하므로 대부분의 화소는 한 영상에서만 옴)
        col = _sample(img, u[idx] * sx - 0.5, vv[idx] * sy - 0.5)
        acc[idx] += col * w[idx, None]
        wsum[idx] += w[idx]

    valid = wsum > 0
    if not valid.any():
        return None
    rgb = np.zeros_like(acc)
    rgb[valid] = acc[valid] / wsum[valid, None]
    rgba = np.empty((4, th, tw), dtype=np.uint8)
    rgba[:3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8).T.reshape(3, th, tw)
    rgba[3] = np.where(valid, 255, 0).reshape(th, tw)
    return rgba


def finalize_cog(src_path: Path, cog_path: Path) -> None:
    """오버뷰를 만들고 Cloud Optimized GeoTIFF로 변환한다."""
    with rasterio.open(src_path, "r+") as ds:
        factors = []
        f = 2
        while max(ds.width, ds.height) / f >= 256:
            factors.append(f)
            f *= 2
        if factors:
            ds.build_overviews(factors, Resampling.average)
    rasterio.shutil.copy(
        src_path,
        cog_path,
        driver="COG",
        compress="deflate",
        predictor=2,
        blocksize=512,
        overviews="force_use_existing",
        BIGTIFF="IF_SAFER",
    )
