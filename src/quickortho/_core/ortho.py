"""정사투영: 출력 타일 단위로 각 영상을 DSM에 역투영하고 중심 가중 블렌딩한다.

메모리 상한(M1 8GB 기준)을 지키기 위해
- 출력은 타일(기본 512px) 단위로 계산해 바로 파일에 쓰고,
- 원본 영상은 출력 해상도에 맞게 축소해 읽으며(JPEG DCT 축소 활용),
- 축소 영상은 메모리 예산 안에서만 LRU 캐시에 둔다.
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
    weight_power: float = 4.0,
    cache_budget_mb: int = 600,
) -> dict:
    xmin, ymin, xmax, ymax = bounds
    width = int(math.ceil((xmax - xmin) / gsd))
    height = int(math.ceil((ymax - ymin) / gsd))
    transform = from_origin(xmin, ymax, gsd, gsd)
    cache = ImageCache(image_dir, src_gsd / gsd, cache_budget_mb * 1024 * 1024)

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

    with rasterio.open(out_path, "w", **profile) as dst:
        for ty in range(n_ty):
            for tx in range(n_tx):
                c0, r0 = tx * tile, ty * tile
                tw, th = min(tile, width - c0), min(tile, height - r0)
                bx0 = xmin + c0 * gsd
                by1 = ymax - r0 * gsd
                bx1, by0 = bx0 + tw * gsd, by1 - th * gsd
                cand = np.nonzero(
                    (vb[:, 0] < bx1) & (vb[:, 2] > bx0) & (vb[:, 1] < by1) & (vb[:, 3] > by0)
                )[0]
                if len(cand):
                    rgba = _render_tile(
                        [views[i] for i in cand], dsm, cache, bx0, by1, gsd, tw, th, weight_power
                    )
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
    }


def _project_weights(
    v: View, world: np.ndarray, power: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """월드 좌표를 영상에 투영해 (u, v, 가중치)를 구한다. 영상 밖은 가중치 0."""
    pc = world @ v.R.T + v.t
    front = pc[:, 2] > 0
    uv = np.full((len(pc), 2), -1.0)
    if front.any():
        uv[front] = v.camera.img_from_cam(pc[front], check_cheirality=False)
    W, H = v.camera.width, v.camera.height
    u, vv = uv[:, 0], uv[:, 1]
    inside = front & (u >= 0) & (u < W) & (vv >= 0) & (vv < H)
    # 영상 중심에 가까울수록 큰 가중치 (연직에 가까운 화소 우선, 경계는 부드럽게)
    edge = np.minimum(np.minimum(u, W - u), np.minimum(vv, H - vv)) / (0.5 * min(W, H))
    w = np.where(inside, np.clip(edge, 0, 1) ** power + 1e-6, 0.0).astype(np.float32)
    return u, vv, w


def _select_views(
    views: list[View], dsm: Dsm, x0: float, y1: float, gsd: float, tw: int, th: int, power: float,
    grid: int = 16, min_share: float = 0.02,
) -> list[View]:
    """거친 격자로 각 영상의 기여 비율을 미리 계산해, 어느 지점에서도 기여가 작은 영상은 뺀다.

    영상 개수로 자르면 인접 타일끼리 사용 영상이 달라져 타일 경계에 이음새가 생긴다.
    기여 비율 기준으로 빼면 불연속이 min_share 이하로 제한된다.
    """
    if len(views) <= 2:
        return views
    gx = x0 + (np.linspace(0, tw, grid)) * gsd
    gy = y1 - (np.linspace(0, th, grid)) * gsd
    X, Y = np.meshgrid(gx, gy)
    world = np.column_stack([X.ravel(), Y.ravel(), dsm.sample(X, Y).ravel()])
    W = np.stack([_project_weights(v, world, power)[2] for v in views])  # (views, points)
    total = W.sum(axis=0)
    share = np.divide(W, total, out=np.zeros_like(W), where=total > 0)
    keep = share.max(axis=1) >= min_share
    return [v for v, k in zip(views, keep) if k]


def _render_tile(
    views: list[View],
    dsm: Dsm,
    cache: ImageCache,
    x0: float,
    y1: float,
    gsd: float,
    tw: int,
    th: int,
    power: float,
) -> np.ndarray | None:
    views = _select_views(views, dsm, x0, y1, gsd, tw, th, power)
    xs = x0 + (np.arange(tw) + 0.5) * gsd
    ys = y1 - (np.arange(th) + 0.5) * gsd
    X, Y = np.meshgrid(xs, ys)
    Z = dsm.sample(X, Y)
    world = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])

    acc = np.zeros((th * tw, 3), dtype=np.float32)
    wsum = np.zeros(th * tw, dtype=np.float32)
    for v in views:
        u, vv, w = _project_weights(v, world, power)
        if not (w > 0).any():
            continue
        W, H = v.camera.width, v.camera.height
        img = cache.get(v.name, W, H)
        sx = img.shape[1] / W
        sy = img.shape[0] / H
        mapx = (u * sx - 0.5).astype(np.float32).reshape(th, tw)
        mapy = (vv * sy - 0.5).astype(np.float32).reshape(th, tw)
        col = cv2.remap(img, mapx, mapy, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        acc += col.reshape(-1, 3).astype(np.float32) * w[:, None]
        wsum += w

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
