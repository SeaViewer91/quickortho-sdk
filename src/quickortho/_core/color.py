"""영상 간 밝기·색 차이 보정 (저주파 이득 맵).

심라인 합성은 화소마다 영상 하나를 쓰므로, 영상끼리 노출이나 지면 반사(태양 방향, 모래·잔디의 이방성 반사)가
다르면 이음매에서 밝기 단차가 선명하게 보인다. 이 단차는 위치 어긋남이 아니라 저주파 밝기 차이이므로,
다중 대역 블렌딩처럼 저주파만 넓게 섞고 세부(고주파)는 고른 영상 하나에서 가져온다.

방법
1. 거친 지면 격자(셀 수 상한 있음)에서 각 영상의 저주파 밝기 L_i를 샘플링한다 (크게 축소·블러한 영상)
2. 심라인 점수(영상 중심 거리)로 넓은 지수 가중을 주어 L_i를 로그 공간에서 섞어 목표 밝기 M을 만든다
3. 영상마다 이득 g_i = M / L_i를 영상 좌표계의 작은 격자(기본 64×48)에 모아 둔다
4. 정사투영 때 영상 i에서 읽은 색에 g_i(u, v)를 곱한다

영상 하나만 덮는 곳은 M = L_i라서 이득이 1이고, 이음매 양쪽에서는 두 영상이 같은 목표 밝기로 맞춰진다.
이득 맵은 영상 좌표에 묶여 있으므로 타일 경계와 무관하게 결과가 이어진다.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .protocol import Emitter
from .surface import Dsm

EPS = 4.0  # 0~255 밝기에서 어두운 곳의 이득이 튀지 않게 더하는 값
GAIN_MIN, GAIN_MAX = 0.5, 2.0


def estimate_gains(
    views: list,
    dsm: Dsm,
    image_dir: Path,
    bounds: tuple[float, float, float, float],
    src_gsd: float,
    out: Emitter,
    max_cells: int = 2_000_000,
    max_fit: int = 6000,
    min_cell_px: float = 16.0,
    seam_emphasis: float = 4.0,
    grid: tuple[int, int] = (64, 48),
    wide_frac: float = 0.06,
    degree: int = 2,
) -> tuple[dict[str, np.ndarray], dict]:
    """영상별 이득 맵 {이름: (gh, gw, 3) float32}과 통계를 반환한다.

    wide_frac: 저주파를 섞는 폭(지수 가중 온도)을 영상 대각선 대비 비율로 지정 (0.06이면 20MP에서 약 400px)
    max_fit: 영상별 이득 곡면을 맞출 때 쓰는 최대 표본 수 (메모리 상한: 영상 수 × max_fit × 약 30바이트)
    seam_emphasis: 이음매 근처 표본의 추가 가중 (0이면 영상이 쓰이는 정도만 반영)
    degree: 영상별 이득 곡면의 차수. 노출(상수)·주변 감광·태양 방향 반사(완만한 기울기)만 담고,
        건물 시차·움직이는 물체처럼 영상마다 내용이 다른 국소 차이는 따라가지 않게 한다
    """
    from .ortho import _project  # 순환 import 방지

    xmin, ymin, xmax, ymax = bounds
    area = (xmax - xmin) * (ymax - ymin)
    cell = max(min_cell_px * src_gsd, math.sqrt(area / max_cells))
    nx = max(2, int(math.ceil((xmax - xmin) / cell)))
    ny = max(2, int(math.ceil((ymax - ymin) / cell)))
    xs = xmin + (np.arange(nx) + 0.5) * cell
    ys = ymax - (np.arange(ny) + 0.5) * cell

    def cells_of(v):
        bx0, by0, bx1, by1 = v.bbox
        c0 = max(0, int((bx0 - xmin) / cell))
        c1 = min(nx, int(math.ceil((bx1 - xmin) / cell)))
        r0 = max(0, int((ymax - by1) / cell))
        r1 = min(ny, int(math.ceil((ymax - by0) / cell)))
        if c1 <= c0 or r1 <= r0:
            return None
        X, Y = np.meshgrid(xs[c0:c1], ys[r0:r1])
        world = np.column_stack([X.ravel(), Y.ravel(), dsm.sample(X, Y).ravel()])
        flat = (np.arange(r0, r1)[:, None] * nx + np.arange(c0, c1)[None, :]).ravel()
        return flat, world

    def project_cells(v):
        cw = cells_of(v)
        if cw is None:
            return None
        flat, world = cw
        u, vv, inside, r = _project(v, world)
        keep = np.nonzero(inside)[0]
        if not len(keep):
            return None
        return flat[keep].astype(np.int64), u[keep], vv[keep], r[keep].astype(np.float32)

    # 1) 셀마다 가장 작은 심라인 점수 (투영은 다시 계산하는 편이 저장보다 메모리에 유리함)
    # 진행률은 두 번의 통과(영상 수 × 2)로 나눠 올린다 (첫 통과도 중단 확인 지점이 되게)
    total = len(views)
    steps = 2 * total
    rmin = np.full(nx * ny, np.inf, np.float32)
    for k, v in enumerate(views):
        g = project_cells(v)
        if g is not None:
            flat, _, _, r = g
            rmin[flat] = np.minimum(rmin[flat], r)  # 한 영상 안에서 셀 번호는 겹치지 않음
        _progress(out, k + 1, steps)

    # 2) 넓은 가중으로 로그 밝기를 섞어 목표 밝기를 만든다. 곡면 맞춤용 표본은 영상당 max_fit개만 남긴다
    acc = np.zeros((nx * ny, 3), np.float32)
    wsum = np.zeros(nx * ny, np.float32)
    samples: list[tuple | None] = []
    rng = np.random.default_rng(0)
    for k, v in enumerate(views):
        g = project_cells(v)
        if g is None:
            samples.append(None)
            _progress(out, total + k + 1, steps)
            continue
        flat, u, vv, r = g
        L = _low_sample(image_dir / v.name, v.camera.width, v.camera.height, cell / src_gsd, u, vv)
        logL = np.log(L + EPS).astype(np.float32)
        temp = wide_frac * math.hypot(v.camera.width, v.camera.height)
        w = np.exp(-(r - rmin[flat]) / temp).astype(np.float32)
        w[w < 1e-3] = 0.0
        acc[flat] += logL * w[:, None]
        wsum[flat] += w
        pick = np.nonzero(w > 0)[0]
        if len(pick) > max_fit:
            pick = np.sort(rng.choice(pick, max_fit, replace=False))
        samples.append((flat[pick], (u[pick] / v.camera.width).astype(np.float32),
                        (vv[pick] / v.camera.height).astype(np.float32), logL[pick], w[pick]))
        _progress(out, total + k + 1, steps)
    target = np.zeros_like(acc)
    ok = wsum > 0
    target[ok] = acc[ok] / wsum[ok, None]
    del acc

    # 3) 영상 좌표 격자에 이득을 모은다
    gw, gh = grid
    gains: dict[str, np.ndarray] = {}
    all_logs = []
    gx = (np.arange(gw) + 0.5) / gw
    gy = (np.arange(gh) + 0.5) / gh
    GX, GY = np.meshgrid(gx, gy)
    basis_grid = _poly_basis(GX.ravel(), GY.ravel(), degree)
    for v, smp in zip(views, samples):
        if smp is None:
            continue
        flat, un, vn, logL, w = smp
        lg = np.clip(target[flat] - logL, math.log(GAIN_MIN), math.log(GAIN_MAX)).astype(np.float64)
        # 이음매(다른 영상과 가중이 비슷한 곳)에서 맞추는 것이 중요하므로 그쪽 표본에 무게를 더 준다
        share = w / np.maximum(wsum[flat], 1e-6)
        fw = w * (1.0 + seam_emphasis * 4.0 * share * (1.0 - share))
        coef = _robust_fit(_poly_basis(un.astype(np.float64), vn.astype(np.float64), degree), lg, fw.astype(np.float64))
        gmap = np.clip(basis_grid @ coef, math.log(GAIN_MIN), math.log(GAIN_MAX)).reshape(gh, gw, 3)
        gains[v.name] = np.exp(gmap).astype(np.float32)
        all_logs.append(gmap.reshape(-1, 3))

    stats: dict = {"cell_m": cell, "grid": [gw, gh], "images": len(gains)}
    if all_logs:
        a = np.exp(np.concatenate(all_logs).mean(axis=1))
        stats.update(
            gain_p5=float(np.percentile(a, 5)),
            gain_p95=float(np.percentile(a, 95)),
        )
    return gains, stats


def _progress(out: Emitter, done: int, total: int) -> None:
    if done % max(1, total // 100) == 0 or done == total:
        out.progress("color", done, total)  # 중단 확인 지점


def _low_sample(path: Path, W: int, H: int, factor: float, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """영상을 셀 크기 정도로 축소·블러해 (u, v) 위치의 저주파 색을 읽는다 (원본 화소 좌표)."""
    tw = max(8, int(round(W / max(factor / 2.0, 1.0))))
    th = max(8, int(round(H * tw / W)))
    with Image.open(path) as im:
        im.draft("RGB", (tw, th))
        im = im.convert("RGB")
        if im.size != (tw, th):
            im = im.resize((tw, th), Image.Resampling.BOX)
        arr = np.asarray(im, dtype=np.float32)
    arr = cv2.GaussianBlur(arr, (0, 0), 2.0, borderType=cv2.BORDER_REPLICATE)
    sx, sy = tw / W, th / H
    mx = (u * sx - 0.5).astype(np.float32).reshape(1, -1)
    my = (v * sy - 0.5).astype(np.float32).reshape(1, -1)
    out = []
    for a in range(0, mx.shape[1], 30000):  # cv2.remap 한 변 32767 제한
        out.append(cv2.remap(arr, mx[:, a:a + 30000], my[:, a:a + 30000], cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)[0])
    return np.concatenate(out).astype(np.float64)


def _poly_basis(x: np.ndarray, y: np.ndarray, degree: int) -> np.ndarray:
    """정규화 영상 좌표(0~1)의 2차원 다항식 기저. 중심을 0으로 옮겨 조건수를 낮춘다."""
    x = 2.0 * x - 1.0
    y = 2.0 * y - 1.0
    cols = [x ** i * y ** j for i in range(degree + 1) for j in range(degree + 1 - i)]
    return np.column_stack(cols)


def _robust_fit(A: np.ndarray, b: np.ndarray, w: np.ndarray, iters: int = 6, c: float = 0.05) -> np.ndarray:
    """가중 최소제곱 + Tukey 반복 재가중. b: (n, 3) 로그 이득, w: 표본 가중치(이 영상이 실제로 쓰이는 정도).

    c는 로그 이득 잔차 기준(0.05 ≈ 5%). 영상끼리 내용이 다른 곳(건물 옆면, 파도)은 잔차가 커서 빠진다.
    """
    n, m = A.shape
    coef = np.zeros((m, b.shape[1]))
    base = np.maximum(w, 0.0) + 1e-3
    if n < m * 3:
        return coef
    rw = np.ones((n, b.shape[1]))
    reg = 1e-3 * np.eye(m)
    for _ in range(iters):
        for ch in range(b.shape[1]):
            ww = base * rw[:, ch]
            Aw = A * ww[:, None]
            coef[:, ch] = np.linalg.solve(A.T @ Aw + reg * ww.sum(), Aw.T @ b[:, ch])
        res = b - A @ coef
        scale = max(c, 1.4826 * float(np.median(np.abs(res))))
        z = np.clip(res / (4.685 * scale), -1.0, 1.0)
        rw = (1.0 - z ** 2) ** 2
    return coef
