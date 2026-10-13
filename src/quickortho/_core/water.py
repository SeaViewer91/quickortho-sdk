"""수면 처리: 영상별 수면 판별, 특징점 제외 마스크, DSM 수면 평면.

데스크톱 앱 v0.5.0(quick-ortho 87130ca)의 engine/quickortho_engine/water.py를 옮긴 것.
SDK 변경: detect의 paths 인자·summarize, ground_water의 공개 함수용 투영, 판별 정보 저장(save_detection/load_detection),
쓰지 않는 대체 높이 분기 제거.

바다·호수는 파도·반사가 영상마다 달라 SfM 점이 생기지 않는다. 그래서 간이 DSM이 수면 위를 주변 육지 높이(방파제,
절벽, 건물)로 채우고, 높이가 틀린 만큼 영상마다 투영 위치가 달라져 수면에서 어긋남이 생긴다.
파도 무늬에 생긴 특징점은 영상당 특징점 상한을 차지해 해안선 쪽 특징점이 모자라게 만들기도 한다.

처리
1. 판별(detect): 영상마다 축소 영상에서 '세밀한 질감이 없고(고주파 에너지 낮음) 푸르거나 청록인' 넓은 영역을 찾는다.
   핵심 수면에 붙은 어두운 물(해초·그늘)까지 넓힌다. 잔디·그늘(풀 질감), 도로(무채색)는 대부분 빠진다.
   결과는 영상별 저해상도 마스크(0 육지, 1 수면 후보, 2 확실한 수면)로 project/water/에 저장한다.
2. 특징점 제외(write_feature_masks): '확실한 수면'(넓고 두꺼운 영역)만 안쪽으로 줄여 COLMAP 마스크로 쓴다.
   도로·지붕처럼 길쭉하거나 작은 오판정은 여기서 빠진다.
3. DSM 수면 평면(ground_water, flatten): DSM 격자를 각 영상에 투영해 수면 표를 모으고, 수면 영역 안에
   믿을 만한 3D 점이 거의 없을 때만 수면으로 확정한다(청록 지붕처럼 색은 비슷하지만 점이 있는 곳은 제외).
   수면 높이는 물가 점들의 낮은 쪽 분위수로 잡아 수면 칸을 평면으로 덮는다. 점 자체는 지우지 않는다
   (물가 칸에 걸친 갯바위 점을 지우면 물가 육지 높이가 나빠짐. 수면 칸은 어차피 평면으로 덮임).
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .protocol import Emitter
from .surface import Dsm

MODES = ("auto", "on", "off")
AUTO_MIN_FRACTION = 0.05  # 자동: 비행 전체 영상 면적 중 수면 비율이 이 이상이면 켬

WORK_PX = 1600  # 질감을 재는 해상도 (특징점 추출 해상도 2000px와 비슷한 규모의 세밀한 질감)
MASK_PX = 480  # 저장·판정용 마스크 해상도 (긴 변)

LAND, CANDIDATE, CONFIDENT = 0, 1, 2


@dataclass
class Detection:
    masks: dict[str, np.ndarray]  # 이름 → (h, w) uint8 {0, 1, 2}
    fraction: float  # 영상 면적 중 수면 비율 평균
    images_with_water: int
    stats: dict = field(default_factory=dict)


def normalize_mode(mode: str | None) -> str:
    m = (mode or "auto").strip().lower()
    if m not in MODES:
        raise ValueError(f"알 수 없는 수면 처리 방식: {mode} (가능: {', '.join(MODES)})")
    return m


# ── 1) 영상별 판별 ──


def _load(path: Path, long_side: int) -> np.ndarray:
    with Image.open(path) as im:
        im.draft("RGB", (long_side * 2, long_side * 2))  # JPEG은 DCT 축소로 빠르게 읽음
        im = im.convert("RGB")
        w, h = im.size
        s = long_side / max(w, h)
        if s < 1:
            im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.BOX)
        return np.asarray(im)


def classify(
    rgb: np.ndarray,
    out_long: int = MASK_PX,
    hp_max: float = 0.8,
    min_frac: float = 0.01,
    confident_frac: float = 0.02,
    confident_radius: float = 0.04,
) -> np.ndarray:
    """RGB 영상(질감용 해상도) → 수면 마스크 (긴 변 out_long, 값 0/1/2).

    hp_max: 고주파 에너지(Lab L, σ1.5 차) 상한. 잔잔한 수면은 0.3~0.6, 잔디·그늘은 2 이상
    confident: 면적이 영상의 confident_frac 이상이고 내접원 반지름이 짧은 변의 confident_radius 이상인 덩어리
    """
    f = rgb.astype(np.float32) / 255.0
    L = cv2.cvtColor(f, cv2.COLOR_RGB2LAB)[..., 0]
    hp = np.abs(L - cv2.GaussianBlur(L, (0, 0), 1.5))
    h, w = L.shape
    s = out_long / max(h, w)
    oh, ow = max(8, round(h * s)), max(8, round(w * s))
    k = max(1.0, 1.0 / s)
    hp = cv2.resize(cv2.GaussianBlur(hp, (0, 0), k), (ow, oh), interpolation=cv2.INTER_AREA)
    small = cv2.resize(f, (ow, oh), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB)
    Ls, a, b = (cv2.GaussianBlur(lab[..., i], (0, 0), 2) for i in range(3))

    # 핵심 수면: 매끈하고, 노란 기가 없고(b* < 3), 청록(a* < -3)이거나 파랑(b* < -5), 너무 밝지 않음(반사·흰 지붕 제외)
    core = (hp < hp_max) & (b < 3) & ((a < -3) | (b < -5)) & (Ls < 90)
    core = cv2.morphologyEx(core.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab_c, st, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = st[1:, cv2.CC_STAT_AREA] >= min_frac * core.size
    seed = keep[lab_c]
    # 색이 다른 수면(해초 띠, 그늘진 물, 탁한 물)은 핵심 수면에 이어져 있을 때만 포함
    relaxed = (hp < hp_max * 1.5) & (b < 6) & (Ls < 90)
    n2, lab_r = cv2.connectedComponents((relaxed | seed).astype(np.uint8), connectivity=8)
    ids = np.unique(lab_r[seed])
    water = np.isin(lab_r, ids[ids > 0])
    water = cv2.morphologyEx(water.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    out = water.astype(np.uint8)  # 1: 후보
    n3, lab_w, st3, _ = cv2.connectedComponentsWithStats(water, connectivity=8)
    if n3 > 1:
        dist = cv2.distanceTransform(water, cv2.DIST_L2, 5)
        short = min(oh, ow)
        for i in range(1, n3):
            comp = lab_w == i
            if st3[i, cv2.CC_STAT_AREA] >= confident_frac * water.size and dist[comp].max() >= confident_radius * short:
                out[comp] = CONFIDENT
    return out


def detect(image_dir: Path | None, names: list[str], out: Emitter, threads: int = -1,
           paths: dict[str, Path] | None = None) -> Detection:
    """영상마다 수면 마스크를 만든다. 진행률은 'water' 단계로 보낸다 (SDK: 진행률 이벤트가 중단 확인 지점).

    paths: (SDK) 이름 → 파일 경로. 없으면 image_dir / 이름.
    """
    n = len(names)
    masks: dict[str, np.ndarray] = {}
    workers = max(1, min(4, (threads if threads > 0 else (cv2.getNumberOfCPUs() or 2))))

    failed: list[str] = []

    def one(name: str) -> tuple[str, np.ndarray]:
        path = paths[name] if paths is not None else Path(image_dir) / name
        try:
            return name, classify(_load(path, WORK_PX))
        except Exception:  # noqa: BLE001  SDK: 읽지 못한 영상(잘린 JPEG 등)은 육지로 봄 (SfM도 그 영상을 건너뜀)
            failed.append(name)
            return name, np.zeros((MASK_PX * 3 // 4, MASK_PX), np.uint8)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, m in ex.map(one, names):
            masks[name] = m
            done += 1
            if done % max(1, n // 50) == 0 or done == n:
                out.progress("water", done, n)
    if failed:
        out.log(f"수면 판별에서 읽지 못한 영상 {len(failed)}장 (육지로 봄): {', '.join(sorted(failed)[:3])}", "warn")
    return summarize(masks)


def summarize(masks: dict[str, np.ndarray]) -> Detection:
    """마스크 묶음의 수면 비율 요약 (SDK: 사용자가 넣은 마스크에도 같은 기준을 씀)."""
    fr = [float((m > 0).mean()) for m in masks.values()]
    conf = [float((m == CONFIDENT).mean()) for m in masks.values()]
    return Detection(
        masks=masks,
        fraction=float(np.mean(fr)) if fr else 0.0,
        images_with_water=int(sum(f > 0.01 for f in fr)),
        stats={"confident_fraction": round(float(np.mean(conf)) if conf else 0.0, 4)},
    )


def decide(mode: str, det: Detection | None) -> bool:
    if mode == "off" or det is None:
        return False
    if mode == "on":
        return True
    return det.fraction >= AUTO_MIN_FRACTION


def save_detection(det: Detection, d: Path, info_path: Path, source: str = "auto") -> None:
    """(SDK) 마스크를 d/<영상>.png로, 판별 요약을 info_path(JSON)로 저장한다. 이전 저장분은 바꾼다.

    임시 폴더에 다 쓴 뒤 바꾸므로 도중에 실패하면 이전 저장분이 그대로 남는다. 이전 폴더는 먼저 옆으로 옮긴다
    (Windows에서 파일이 잠겨 옮길 수 없으면 아무것도 바꾸지 않고 오류).
    """
    import json
    import os
    import shutil

    tmp_d, tmp_j = d.with_name(d.name + ".tmp"), info_path.with_name(info_path.name + ".tmp")
    old_d = d.with_name(d.name + ".old")
    try:
        save_masks(det.masks, tmp_d)
        info = {"source": source, "image_fraction": round(det.fraction, 4),
                "images_with_water": det.images_with_water, **det.stats}
        tmp_j.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.rmtree(old_d, ignore_errors=True)
        if d.exists():
            os.replace(d, old_d)
        try:
            os.replace(tmp_d, d)
        except BaseException:
            if old_d.exists() and not d.exists():
                os.replace(old_d, d)
            raise
        os.replace(tmp_j, info_path)
        shutil.rmtree(old_d, ignore_errors=True)
    finally:
        shutil.rmtree(tmp_d, ignore_errors=True)
        tmp_j.unlink(missing_ok=True)


def load_detection(d: Path, info_path: Path) -> tuple[Detection, dict] | None:
    """(SDK) 저장된 판별 결과. 없으면 None. 반환: (Detection, 저장된 요약)."""
    import json

    masks = load_masks(d)
    if not masks:
        return None
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    det = summarize(masks)
    return det, {"source": info.get("source", "auto"), **{k: v for k, v in info.items() if k != "source"}}


def save_masks(masks: dict[str, np.ndarray], d: Path) -> None:
    import shutil

    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for name, m in masks.items():
        _imwrite(d / f"{name}.png", m * 100)  # 0/100/200: 사람이 봐도 구분되게


def load_masks(d: Path) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*.png")):  # 하위 폴더의 영상은 같은 하위 경로로 저장됨
        m = _imread(p)
        if m is not None:
            out[p.relative_to(d).as_posix()[:-4]] = (m // 100).astype(np.uint8)
    return out


def _imwrite(path: Path, img: np.ndarray, params: list[int] | None = None) -> None:
    """한글 등 비ASCII 경로에서도 동작하도록 imencode로 쓴다 (Windows의 cv2.imwrite는 비ASCII 경로를 못 씀)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", img, params or [])
    if not ok:
        raise RuntimeError(f"PNG 저장 실패: {path}")
    path.write_bytes(buf.tobytes())


def _imread(path: Path) -> np.ndarray | None:
    data = np.frombuffer(path.read_bytes(), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


# ── 2) 특징점 제외 마스크 ──


def write_feature_masks(
    masks: dict[str, np.ndarray], image_sizes: dict[str, tuple[int, int]], d: Path, shrink: float = 0.02
) -> int:
    """확실한 수면을 안쪽으로 줄여(짧은 변 × shrink) COLMAP 마스크(검정=특징점 제외)로 쓴다. 반환: 마스크를 쓴 영상 수.

    물가(갯바위·방파제 발치)의 특징점은 정합에 중요하므로 경계 쪽은 남긴다.
    """
    if not any((m == CONFIDENT).any() for m in masks.values()):
        return 0
    d.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, (W, H) in image_sizes.items():
        m = masks.get(name)
        conf = (m == CONFIDENT).astype(np.uint8) if m is not None else None
        if conf is not None and conf.any():
            r = max(1, int(round(shrink * min(conf.shape))))
            conf = cv2.erode(conf, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
        if conf is not None and conf.any():
            full = cv2.resize(((1 - conf) * 255).astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
            count += 1
        else:
            # 마스크 파일이 없으면 COLMAP이 영상마다 오류 로그를 남기므로 전부 흰(제외 없음) 마스크를 둠
            full = np.full((H, W), 255, np.uint8)
        _imwrite(d / f"{name}.png", full, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return count


# ── 3) DSM 수면 평면 ──


def ground_water(
    views: list,
    dsm: Dsm,
    masks: dict[str, np.ndarray],
    pts: np.ndarray,
    vote_min: float = 0.5,
    empty_ratio: float = 0.15,
    ring_m: float = 4.0,
    level_q: float = 25.0,
    raised_max: float = 1.5,
    drop_max: float = 3.0,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """DSM 격자에서 수면 칸(bool)과 칸별 수면 높이(float, 수면 아닌 칸은 NaN), 통계를 구한다.

    vote_min: 그 칸을 본 영상 중 수면으로 판정한 비율의 하한
    empty_ratio: 수면 덩어리 안쪽의 점 밀도가 육지 평균 밀도의 이 비율보다 작아야 수면으로 확정
    ring_m: 수면 높이를 잡는 물가 띠 폭
    level_q: 물가 점 높이의 분위수 (방파제 위·모래사장 경사보다 물가에 가까운 낮은 쪽)
    raised_max: 덩어리 안 점의 중앙값이 물가 점 중앙값보다 이만큼(m) 넘게 높으면 수면이 아님 (청록 지붕 등)
    drop_max: 수면 높이가 물가 점의 5백분위보다 이만큼(m) 넘게 높으면 수면이 아님 (가장자리 점만 남은 지붕)
    """
    from .ortho import _project as _ortho_project  # 순환 import 방지

    def _project(v, world):
        # SDK: 공개 함수(quickortho.water.flatten_dsm)는 View 대신 project_world()가 있는 카메라 묶음을 넘김
        return v.project_world(world) if hasattr(v, "project_world") else _ortho_project(v, world)

    rows, cols = dsm.z.shape
    votes = np.zeros(rows * cols, np.float32)
    seen = np.zeros(rows * cols, np.float32)
    gx = dsm.x0 + np.arange(cols) * dsm.res
    gy = dsm.y0 - np.arange(rows) * dsm.res
    for v in views:
        m = masks.get(v.name)
        if m is None:
            continue
        bx0, by0, bx1, by1 = v.bbox
        c0 = max(0, int((bx0 - dsm.x0) / dsm.res))
        c1 = min(cols, int(math.ceil((bx1 - dsm.x0) / dsm.res)) + 1)
        r0 = max(0, int((dsm.y0 - by1) / dsm.res))
        r1 = min(rows, int(math.ceil((dsm.y0 - by0) / dsm.res)) + 1)
        if c1 <= c0 or r1 <= r0:
            continue
        X, Y = np.meshgrid(gx[c0:c1], gy[r0:r1])
        world = np.column_stack([X.ravel(), Y.ravel(), dsm.z[r0:r1, c0:c1].ravel()])
        u, vv, inside, _ = _project(v, world)
        idx = (np.arange(r0, r1)[:, None] * cols + np.arange(c0, c1)[None, :]).ravel()[inside]
        mh, mw = m.shape
        mu = np.clip((u[inside] * mw / v.camera.width).astype(np.int64), 0, mw - 1)
        mv = np.clip((vv[inside] * mh / v.camera.height).astype(np.int64), 0, mh - 1)
        votes[idx] += (m[mv, mu] > 0)
        seen[idx] += 1
    frac = np.divide(votes, seen, out=np.zeros_like(votes), where=seen > 0).reshape(rows, cols)
    cand = (frac >= vote_min) & (seen.reshape(rows, cols) > 0)
    stats: dict = {"candidate_cells": int(cand.sum())}
    level = np.full((rows, cols), np.nan, np.float32)
    if not cand.any() or len(pts) == 0:
        return np.zeros((rows, cols), bool), level, {**stats, "components": 0}

    # 점이 놓인 칸
    pc = np.clip(((pts[:, 0] - dsm.x0) / dsm.res).round().astype(np.int64), 0, cols - 1)
    pr = np.clip(((dsm.y0 - pts[:, 1]) / dsm.res).round().astype(np.int64), 0, rows - 1)
    count = np.zeros((rows, cols), np.float32)
    np.add.at(count, (pr, pc), 1.0)
    land = ~cand & (seen.reshape(rows, cols) > 0)
    land_density = count[land].sum() / max(int(land.sum()), 1)

    cand_u8 = cand.astype(np.uint8)
    n, lab = cv2.connectedComponents(cand_u8, connectivity=8)
    edge = max(1, int(round(2.0 / dsm.res)))  # 경계 2 m는 혼합 칸이라 밀도 계산에서 뺌
    inner = cv2.erode(cand_u8, np.ones((2 * edge + 1, 2 * edge + 1), np.uint8)).astype(bool)
    ring_px = max(1, int(round(ring_m / dsm.res)))
    water = np.zeros((rows, cols), bool)
    comps = []
    min_cells = max(4, int(round(100.0 / (dsm.res * dsm.res))))  # 100 m² 미만은 무시
    for i in range(1, n):
        comp = lab == i
        ncell = int(comp.sum())
        if ncell < min_cells:
            continue
        core = comp & inner
        if not core.any():
            core = comp
        density = count[core].sum() / core.sum()
        ratio = density / land_density if land_density > 0 else 0.0
        if ratio >= empty_ratio:
            comps.append({"area_m2": round(ncell * dsm.res**2, 1), "point_ratio": round(float(ratio), 3), "water": False})
            continue
        dil = cv2.dilate(comp.astype(np.uint8), np.ones((2 * ring_px + 1, 2 * ring_px + 1), np.uint8)).astype(bool)
        ring = dil & ~comp
        zs = pts[ring[pr, pc], 2]
        lv = float(np.percentile(zs, level_q)) if len(zs) >= 5 else None
        # 수면은 물가에서 가장 낮은 면이다. 물가 띠에 수면 높이보다 한참 낮은 점이 많으면(지붕 가장자리 밖 지면)
        # 높은 평면(지붕)을 잘못 잡은 것
        drop = float(lv - np.percentile(zs, 5)) if lv is not None else None
        # 수면은 주변보다 낮다. 덩어리 안(가장자리 포함)의 점이 물가보다 뚜렷이 높으면 지붕 같은 높은 평면임
        zin = pts[comp[pr, pc], 2]
        raised = None
        if lv is not None and len(zin) >= max(10, 0.2 * len(zs)):
            raised = float(np.median(zin) - np.median(zs))
        info = {
            "area_m2": round(ncell * dsm.res**2, 1),
            "point_ratio": round(float(ratio), 3),
            "shore_points": int(len(zs)),
            "inner_points": int(len(zin)),
            "raised_m": None if raised is None else round(raised, 2),
            "shore_drop_m": None if drop is None else round(drop, 2),
            "level_m": None if lv is None else round(lv, 2),
        }
        if lv is None or (raised is not None and raised > raised_max) or (drop is not None and drop > drop_max):
            # 물가 점이 없으면(촬영 범위 가장자리 등) 높이를 정할 근거가 없으므로 손대지 않음
            comps.append({**info, "water": False})
            continue
        water |= comp
        comps.append({**info, "water": True})
        level[comp] = lv  # 앱의 '면적 가중 중앙값' 대체 높이 분기는 lv가 있어야 수면이 되므로 실행되지 않아 뺐음
    stats.update(
        levels_m=sorted({c["level_m"] for c in comps if c["water"]}),  # SDK: 모든 수면 덩어리의 높이 (detail은 10개까지)
        components=len(comps),
        water_components=int(sum(c["water"] for c in comps)),
        area_m2=round(float(water.sum()) * dsm.res**2, 1),
        land_point_density=round(float(land_density), 4),
        detail=sorted(comps, key=lambda c: -c["area_m2"])[:10],
    )
    return water, level, stats


def flatten(dsm: Dsm, water: np.ndarray, level: np.ndarray) -> Dsm:
    z = dsm.z.copy()
    z[water] = level[water]
    return Dsm(z=z.astype(np.float32), x0=dsm.x0, y0=dsm.y0, res=dsm.res, num_points=dsm.num_points)
