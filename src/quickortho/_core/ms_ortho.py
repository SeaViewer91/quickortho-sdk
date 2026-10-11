"""다중분광 정사투영: 기준 밴드로 고른 심라인을 모든 밴드에 같이 쓰고, 밴드마다 자기 카메라로 DSM에 투영한다.

- 영상 선택(심라인)은 기준 밴드 카메라 기준 점수로 화소마다 한 번만 정한다. 밴드마다 다른 영상을 고르면
  지수가 서로 다른 촬영의 값으로 계산되기 때문
- 밴드 카메라 = 기준 밴드 자세 ∘ rig 상대 자세(메타데이터). 밴드를 영상 평면에서 겹치지 않고 각자 투영하므로
  저고도·높은 물체의 시차도 DSM 정확도만큼 맞음
- 값은 반사율(float32). 모든 밴드가 있는 화소만 유효로 두고 나머지는 NaN(nodata)
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from . import multispectral as ms
from .ortho import View, _blend_weights, _project, _select_views, default_cache_budget_mb
from .protocol import Emitter
from .surface import Dsm


@dataclass
class BandSource:
    view: View  # 밴드 카메라 (자세·내부표정)
    band: ms.BandImage
    factor: float  # 방사휘도 → 반사율 계수


def build_band_sources(
    views: list[View],
    ds: ms.MsDataset,
    factors: dict[str, dict[int, float]],
    rig_corr: dict[int, np.ndarray] | None = None,
    use_translation: bool = False,
) -> dict[str, list[BandSource]]:
    """정렬된 기준 밴드 영상마다 출력 밴드들의 카메라를 만든다 (ds.out_indices 순서).

    rig_corr: 비행 데이터로 구한 밴드별 회전 보정 (밴드 카메라 좌표계). refine_rig 참고
    use_translation: 메타데이터의 밴드 렌즈 위치 차(RigTranslations) 사용 여부. RedEdge-P 시험 비행(고도 21m)에서
        적용하면 3~4화소 어긋나고 빼면 0.5화소 이내였음. 제조사 회전값이 시차를 흡수한 것으로 보고 기본은 쓰지 않으며,
        남는 차이는 비행마다 refine_rig로 맞춘다
    """
    cap_of = ds.capture_of_ref_file()
    out: dict[str, list[BandSource]] = {}
    for v in views:
        cap = cap_of.get(ms.ref_file_of_proxy(v.name))
        if cap is None or cap not in factors:
            continue
        ref = ds.ref(cap)
        srcs = []
        for i in ds.out_indices:
            b = ds.captures[cap][i]
            R, c = ms.band_from_ref(b, ref)
            if not use_translation:
                c = np.zeros(3)
            if rig_corr and i in rig_corr:
                R = rig_corr[i] @ R
            Rb = R @ v.R
            tb = R @ (v.t - c)
            bv = View(b.file, ms.colmap_camera(b), Rb, tb, -Rb.T @ tb, v.bbox)
            srcs.append(BandSource(bv, b, factors[cap][i]))
        out[v.name] = srcs
    return out


def _log_grad(img: np.ndarray) -> np.ndarray:
    x = np.log(np.clip(img, 1e-6, None)).astype(np.float32)
    x = cv2.GaussianBlur(x, (0, 0), 1.0)
    return np.hypot(cv2.Sobel(x, cv2.CV_32F, 1, 0), cv2.Sobel(x, cv2.CV_32F, 0, 1))


def _ray_dsm(cam_view: View, px: np.ndarray, dsm: Dsm, z0: float, iters: int = 4) -> np.ndarray:
    """밴드 화소 광선과 DSM의 교점 (높이를 몇 번 고쳐 가며 근사)."""
    xy = cam_view.camera.cam_from_img(px)
    d = np.column_stack([xy, np.ones(len(xy))]) @ cam_view.R  # 카메라 → 월드 방향 (R^T·d)
    C = cam_view.center
    z = np.full(len(px), z0)
    for _ in range(iters):
        t = (z - C[2]) / np.where(np.abs(d[:, 2]) > 1e-9, d[:, 2], -1e-9)
        X = C[None, :] + t[:, None] * d
        z = dsm.sample(X[:, 0], X[:, 1])
    return X


def refine_rig(
    views: list[View],
    sources: dict[str, list[BandSource]],
    ds: ms.MsDataset,
    dsm: Dsm,
    ground_z: float,
    max_captures: int = 10,
    step: int = 8,
) -> tuple[dict[int, np.ndarray], dict]:
    """비행 데이터로 밴드별 rig 회전을 미세 조정한다.

    캡처 몇 개에서 기준 밴드 영상을 현재 모델(rig + DSM)로 각 밴드 영상 좌표에 옮겨 놓고, 두 영상의 경사도
    (밴드마다 밝기가 반대일 수 있어 로그 경사도 크기)를 ECC로 맞춰 남은 이동량을 잰다. 캡처들의 중앙값을
    밴드 카메라의 작은 회전으로 바꿔 모든 캡처에 적용한다 (rig는 한 비행 안에서 고정이므로).
    """
    by_name = {v.name: v for v in views}
    names = [n for n in sorted(sources) if n in by_name]
    names = names[:: max(1, len(names) // max_captures)][:max_captures]
    cap_of = ds.capture_of_ref_file()
    shifts: dict[int, list[tuple[float, float]]] = {i: [] for i in ds.out_indices}
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
    vig: dict[tuple, np.ndarray] = {}

    def rad(b: ms.BandImage) -> np.ndarray:
        key = (b.index, b.width, b.height)
        if key not in vig:
            vig[key] = ms.vignette(b)
        return ms.radiance(b, ms.read_raw(ds.root / b.file), vig[key])

    for name in names:
        v = by_name[name]
        ref = ds.ref(cap_of[ms.ref_file_of_proxy(name)])
        g_ref = _log_grad(rad(ref) + 1e-4)
        for src in sources[name]:
            b = src.band
            # 거친 격자: cv2.resize가 늘일 때 쓰는 화소 중심 위치와 정확히 같게 잡아야 0.5화소 치우침이 없음
            nw, nh = max(2, b.width // step), max(2, b.height // step)
            gx, gy = np.meshgrid((np.arange(nw) + 0.5) * (b.width / nw) - 0.5, (np.arange(nh) + 0.5) * (b.height / nh) - 0.5)
            px = np.column_stack([gx.ravel() + 0.5, gy.ravel() + 0.5]).astype(np.float64)
            X = _ray_dsm(src.view, px, dsm, ground_z)
            u, vv, inside, _ = _project(v, X)
            if inside.mean() < 0.5:
                continue
            mx = cv2.resize((u - 0.5).reshape(gx.shape).astype(np.float32), (b.width, b.height))
            my = cv2.resize((vv - 0.5).reshape(gx.shape).astype(np.float32), (b.width, b.height))
            warped = cv2.remap(g_ref, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            g_b = _log_grad(rad(b) + 1e-4)
            m = (b.height // 8, b.width // 8)
            # 속도를 위해 절반 해상도에서 맞춤 (경사도 영상이라 0.1화소 수준 정밀도는 유지됨)
            tmpl = cv2.resize(g_b[m[0] : -m[0], m[1] : -m[1]], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            img = cv2.resize(warped[m[0] : -m[0], m[1] : -m[1]], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            warp = np.eye(2, 3, dtype=np.float32)
            try:
                _, warp = cv2.findTransformECC(tmpl, img, warp, cv2.MOTION_TRANSLATION, crit, None, 3)
            except cv2.error:
                continue
            shifts[b.index].append((2.0 * float(warp[0, 2]), 2.0 * float(warp[1, 2])))
    corr: dict[int, np.ndarray] = {}
    stats: dict = {"captures": len(names), "bands": {}}
    any_cap = next(iter(ds.captures.values()))
    for i, sh in shifts.items():
        if len(sh) < 3:
            continue
        a = np.array(sh)
        med = np.median(a, axis=0)
        mad = np.median(np.abs(a - med), axis=0)
        b = any_cap[i]
        # 밴드 화소 x 의 실제 대응점은 모델이 x + t 로 보낸 점 → 투영을 −t 만큼 옮기는 작은 회전
        phx, phy = -med[0] / b.fx, med[1] / b.fy
        ry = np.array([[math.cos(phx), 0, math.sin(phx)], [0, 1, 0], [-math.sin(phx), 0, math.cos(phx)]])
        rx = np.array([[1, 0, 0], [0, math.cos(phy), -math.sin(phy)], [0, math.sin(phy), math.cos(phy)]])
        corr[i] = rx @ ry
        stats["bands"][b.band] = {
            "shift_px": [round(float(med[0]), 2), round(float(med[1]), 2)],
            "spread_px": [round(float(mad[0]), 2), round(float(mad[1]), 2)],
            "n": len(sh),
        }
    return corr, stats


def normalize_captures(
    rec,
    views: list[View],
    sources: dict[str, list[BandSource]],
    ds: ms.MsDataset,
    max_points: int = 20000,
    blur_px: float = 2.0,
    prior: float = 2.0,
) -> tuple[dict[str, list[float]], dict]:
    """방사 블록 조정: 같은 지점을 본 캡처들의 반사율이 같아지도록 캡처·밴드별 이득을 구한다.

    SfM 타이포인트(여러 캡처가 본 3D 점)에서 각 캡처의 밴드 반사율(로그)을 읽고, 점별 평균과의 차이를
    캡처 이득으로 설명한다. 반복 재가중(Tukey)으로 건물 옆면·움직이는 물체 같은 이상값을 빼고,
    관측이 적은 캡처는 이득 0(로그) 쪽으로 당긴다(prior). 캡처 이득의 평균을 0으로 고정해
    DLS·패널로 정한 전체 반사율 수준은 바꾸지 않는다. 반환: {영상 이름: 밴드별 배율}, 통계.
    """
    names = [v.name for v in views if v.name in sources]
    name_idx = {n: j for j, n in enumerate(names)}
    id2name = {im.image_id: im.name for im in rec.images.values()}
    pts = [p for p in rec.points3D.values() if p.track.length() >= 2]
    if len(pts) > max_points:
        sel = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts = [pts[k] for k in sel]
    per_image: dict[str, list[int]] = {n: [] for n in names}
    xyz = np.array([p.xyz for p in pts]) if pts else np.zeros((0, 3))
    for pi, p in enumerate(pts):
        for el in p.track.elements:
            n = id2name.get(el.image_id)
            if n in per_image:
                per_image[n].append(pi)
    nb = len(ds.out_indices)
    rows_p, rows_c, rows_k, rows_v = [], [], [], []
    vig: dict[tuple, np.ndarray] = {}
    for n in names:
        idx = np.array(per_image[n], dtype=np.int64)
        if not len(idx):
            continue
        for k, src in enumerate(sources[n]):
            b = src.band
            u, vv, inside, _ = _project(src.view, xyz[idx])
            ok = inside & (u > 4) & (vv > 4) & (u < b.width - 4) & (vv < b.height - 4)
            if not ok.any():
                continue
            key = (b.index, b.width, b.height)
            if key not in vig:
                vig[key] = ms.vignette(b)
            img = cv2.GaussianBlur(ms.radiance(b, ms.read_raw(ds.root / b.file), vig[key]), (0, 0), blur_px)
            val = _sample1(img, u[ok] - 0.5, vv[ok] - 0.5) * src.factor
            good = val > 1e-6
            rows_p.append(idx[ok][good])
            rows_c.append(np.full(good.sum(), name_idx[n]))
            rows_k.append(np.full(good.sum(), k))
            rows_v.append(np.log(val[good]))
    gains = {n: [1.0] * nb for n in names}
    if not rows_p:
        return gains, {"applied": False, "reason": "타이포인트 관측이 없음"}
    P, Cc, K, V = (np.concatenate(a) for a in (rows_p, rows_c, rows_k, rows_v))
    ncap = len(names)
    stats: dict = {"applied": True, "points": int(len(np.unique(P))), "bands": {}}
    any_cap = next(iter(ds.captures.values()))
    for k in range(nb):
        s = K == k
        g, before, after = solve_capture_gains(P[s], Cc[s], V[s], ncap, prior)
        for n, j in name_idx.items():
            gains[n][k] = float(np.exp(g[j]))
        stats["bands"][any_cap[ds.out_indices[k]].band] = {
            "inconsistency_before_pct": round(100 * before, 1),
            "inconsistency_after_pct": round(100 * after, 1),
            "gain_min": round(float(np.exp(g.min())), 3),
            "gain_max": round(float(np.exp(g.max())), 3),
        }
    return gains, stats


def solve_capture_gains(
    p: np.ndarray, c: np.ndarray, v: np.ndarray, ncap: int, prior: float = 2.0, iters: int = 25
) -> tuple[np.ndarray, float, float]:
    """점 p를 캡처 c에서 본 로그 반사율 v에서 캡처별 로그 이득 g를 구한다 (평균 0, Tukey 반복 재가중).

    반환: g, 보정 전·후 불일치(점 평균과의 차이 RMS, 로그 단위 ≈ 비율)
    """
    up, inv = np.unique(p, return_inverse=True)
    multi = np.bincount(inv)[inv] >= 2
    p, c, v = p[multi], c[multi], v[multi]
    g = np.zeros(ncap)
    if not len(v):
        return g, 0.0, 0.0
    up, inv = np.unique(p, return_inverse=True)

    def rms(gv: np.ndarray) -> float:
        cur = v + gv[c]
        m = np.bincount(inv, cur) / np.bincount(inv)
        return float(np.sqrt(np.mean((cur - m[inv]) ** 2)))

    before = rms(g)
    w = np.ones(len(v))
    for _ in range(iters):
        cur = v + g[c]
        m = np.bincount(inv, w * cur, minlength=len(up)) / np.maximum(np.bincount(inv, w, minlength=len(up)), 1e-9)
        r = m[inv] - v
        g = np.bincount(c, w * r, minlength=ncap) / (np.bincount(c, w, minlength=ncap) + prior)
        g -= g.mean()
        d = v + g[c] - m[inv]
        scale = max(0.02, 1.4826 * float(np.median(np.abs(d))))
        z = np.clip(d / (4.685 * scale), -1, 1)
        w = (1 - z**2) ** 2
    return g, before, rms(g)


class BandCache:
    """반사율 밴드 영상을 출력 해상도로 줄여 메모리 예산 안에서 보관한다."""

    def __init__(self, root: Path, gsd: float, budget_bytes: int) -> None:
        self.root = root
        self.gsd = gsd
        self.budget = budget_bytes
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._bytes = 0
        self._vig: dict[tuple, np.ndarray] = {}

    def get(self, src: BandSource, band_gsd: float) -> np.ndarray:
        key = src.band.file
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        b = src.band
        vkey = (b.index, b.width, b.height, tuple(b.vig_center or ()), tuple(b.vig_poly or ()), tuple(b.vig2d or ()))
        if vkey not in self._vig:
            self._vig[vkey] = ms.vignette(b)
        refl = ms.radiance(b, ms.read_raw(self.root / b.file), self._vig[vkey]) * np.float32(src.factor)
        scale = min(1.0, band_gsd / self.gsd)
        if scale < 0.999:
            w = max(1, round(b.width * scale))
            h = max(1, round(b.height * scale))
            refl = cv2.resize(refl, (w, h), interpolation=cv2.INTER_AREA)
        self._items[key] = refl
        self._bytes += refl.nbytes
        while self._bytes > self.budget and len(self._items) > 1:
            _, old = self._items.popitem(last=False)
            self._bytes -= old.nbytes
        return refl


def band_gsd(sources: dict[str, list[BandSource]], ground_z: float) -> float:
    """출력 밴드의 원본 GSD (중앙값): 고도 / 초점거리(화소)."""
    g = [max(s.view.center[2] - ground_z, 1.0) / s.band.fx for srcs in sources.values() for s in srcs]
    return float(np.median(g))


def render_ms_orthomosaic(
    views: list[View],
    sources: dict[str, list[BandSource]],
    band_names: list[str],
    dsm: Dsm,
    root: Path,
    out_path: Path,
    crs,
    gsd: float,
    src_gsd: float,
    bgsd: float,
    bounds: tuple[float, float, float, float],
    out: Emitter,
    tile: int = 512,
    feather_px: float = 6.0,
    cache_budget_mb: int | None = None,
    block: int = 4,
) -> dict:
    """다중밴드 반사율 정사 모자이크(float32, nodata NaN)를 타일 단위로 만든다.

    src_gsd: 기준 밴드(심라인 점수를 매기는 카메라)의 원본 GSD, bgsd: 출력 밴드의 원본 GSD
    """
    views = [v for v in views if v.name in sources]
    nb = len(band_names)
    xmin, ymin, xmax, ymax = bounds
    width = int(math.ceil((xmax - xmin) / gsd))
    height = int(math.ceil((ymax - ymin) / gsd))
    if cache_budget_mb is None:
        cache_budget_mb = default_cache_budget_mb()
    cache = BandCache(root, gsd, cache_budget_mb * 1024 * 1024)
    temp = max(feather_px * (gsd / src_gsd) / 1.5, 1e-3)
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": nb,
        "dtype": "float32",
        "nodata": float("nan"),
        "crs": crs,
        "transform": from_origin(xmin, ymax, gsd, gsd),
        "tiled": True,
        "blockxsize": tile,
        "blockysize": tile,
        "compress": "deflate",
        "predictor": 3,
        "BIGTIFF": "IF_SAFER",
        "sparse_ok": True,
    }
    vb = np.array([v.bbox for v in views])
    n_tx, n_ty = math.ceil(width / tile), math.ceil(height / tile)
    total = n_tx * n_ty
    step = max(1, total // 100)
    order = [
        (ty, tx)
        for by in range(0, n_ty, block)
        for bx in range(0, n_tx, block)
        for ty in range(by, min(by + block, n_ty))
        for tx in range(bx, min(bx + block, n_tx))
    ]
    covered = 0
    done = 0
    with rasterio.open(out_path, "w", **profile) as dst:
        for i, name in enumerate(band_names, 1):
            dst.set_band_description(i, name)
        for ty, tx in order:
            c0, r0 = tx * tile, ty * tile
            tw, th = min(tile, width - c0), min(tile, height - r0)
            bx0 = xmin + c0 * gsd
            by1 = ymax - r0 * gsd
            bx1, by0 = bx0 + tw * gsd, by1 - th * gsd
            cand = np.nonzero((vb[:, 0] < bx1) & (vb[:, 2] > bx0) & (vb[:, 1] < by1) & (vb[:, 3] > by0))[0]
            if len(cand):
                data = _render_tile(
                    [views[k] for k in cand], sources, dsm, cache, bgsd, bx0, by1, gsd, tw, th, temp, nb
                )
                if data is not None:
                    covered += int(np.isfinite(data[0]).sum())
                    dst.write(data, window=Window(c0, r0, tw, th))
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
        "bands": band_names,
    }


def _sample1(img: np.ndarray, x: np.ndarray, y: np.ndarray, width: int = 1024) -> np.ndarray:
    """단일 밴드 영상의 흩어진 좌표를 쌍선형 보간으로 읽는다 (cv2.remap 한 변 32767 제한 때문에 접어서 씀)."""
    n = len(x)
    rows = -(-n // width)
    pad = rows * width - n
    mx = np.pad(x.astype(np.float32), (0, pad)).reshape(rows, width)
    my = np.pad(y.astype(np.float32), (0, pad)).reshape(rows, width)
    col = cv2.remap(img, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return col.reshape(-1)[:n]


def _render_tile(views, sources, dsm, cache, bgsd, x0, y1, gsd, tw, th, temp, nb):
    views = _select_views(views, dsm, x0, y1, gsd, tw, th, temp)
    if not views:
        return None
    xs = x0 + (np.arange(tw) + 0.5) * gsd
    ys = y1 - (np.arange(th) + 0.5) * gsd
    X, Y = np.meshgrid(xs, ys)
    world = np.column_stack([X.ravel(), Y.ravel(), dsm.sample(X, Y).ravel()])
    proj = [_project(v, world) for v in views]
    w_all = _blend_weights(np.stack([p[3] for p in proj]), np.stack([p[2] for p in proj]), temp)
    n = th * tw
    acc = np.zeros((nb, n), np.float32)
    wsum = np.zeros((nb, n), np.float32)
    for v, w in zip(views, w_all):
        idx = np.nonzero(w > 0)[0]
        if not len(idx):
            continue
        wp = world[idx]
        for k, src in enumerate(sources[v.name]):
            u, vv, inside, _ = _project(src.view, wp)
            ii = np.nonzero(inside)[0]
            if not len(ii):
                continue
            img = cache.get(src, bgsd)
            sx = img.shape[1] / src.band.width
            sy = img.shape[0] / src.band.height
            val = _sample1(img, u[ii] * sx - 0.5, vv[ii] * sy - 0.5)
            sel = idx[ii]
            acc[k, sel] += val * w[sel]
            wsum[k, sel] += w[sel]
    valid = (wsum > 0).all(axis=0)
    if not valid.any():
        return None
    data = np.full((nb, n), np.nan, np.float32)
    data[:, valid] = acc[:, valid] / wsum[:, valid]
    return data.reshape(nb, th, tw)


# ── 산출물: 지수, 미리보기 ─────────────────────────────────

def _band_lookup(names: list[str]) -> dict[str, int]:
    return {n.strip().lower(): i for i, n in enumerate(names)}


def write_rgb_preview(refl_path: Path, png_path: Path, names: list[str], max_size: int = 2048) -> dict:
    """반사율 정사 모자이크의 표시용 컬러 미리보기 (R·G·B 밴드, 없으면 NIR·Red·Green 위색)."""
    from PIL import Image

    look = _band_lookup(names)
    if all(k in look for k in ("red", "green", "blue")):
        idx, kind = [look["red"], look["green"], look["blue"]], "true_color"
    elif all(k in look for k in ("nir", "red", "green")):
        idx, kind = [look["nir"], look["red"], look["green"]], "false_color"
    else:
        idx, kind = [0, 0, 0], "gray"
    with rasterio.open(refl_path) as ds:
        s = min(1.0, max_size / max(ds.width, ds.height))
        w, h = max(1, int(ds.width * s)), max(1, int(ds.height * s))
        data = ds.read([i + 1 for i in idx], out_shape=(3, h, w),
                       resampling=rasterio.enums.Resampling.average).astype(np.float32)
    valid = np.isfinite(data).all(axis=0)
    rgba = np.zeros((h, w, 4), np.uint8)
    if valid.any():
        vals = data[:, valid]
        lo = np.percentile(vals, 1, axis=1)[:, None]
        hi = np.percentile(vals, 99, axis=1)[:, None]
        x = np.clip((vals - lo) / np.maximum(hi - lo, 1e-6), 0, 1) ** (1 / 1.6)
        rgba[valid, :3] = (x.T * 255 + 0.5).astype(np.uint8)
        rgba[valid, 3] = 255
    Image.fromarray(rgba, "RGBA").save(png_path)
    return {"preview": kind}
