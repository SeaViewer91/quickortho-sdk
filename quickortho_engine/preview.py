"""빠른 미리보기: SfM 없이 EXIF/XMP만으로 촬영 범위·중복도·누락 구역·간이 모자이크를 만든다.

현장에서 철수하기 전에 재촬영 여부를 판단하는 용도다. 지면을 이륙 지점 높이의 평면으로 가정하므로
위치 정확도는 GNSS와 짐벌 자세 정확도 수준(수 m)이다.

자세 규약 (DJI XMP 기준)
- GimbalYawDegree: 진북 기준 시계 방향 각도, 영상 위쪽이 가리키는 방향
- GimbalPitchDegree: -90이 연직 하방, 0이 수평
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

from .geo import UtmProjector
from .protocol import Emitter
from .scan import scan_folder

DIAG_35MM = math.hypot(36.0, 24.0)  # 43.27 mm
MAX_RANGE_FACTOR = 4.0  # 경사 촬영 시 footprint를 비행고도의 4배까지로 제한


@dataclass
class Pose:
    name: str
    e: float
    n: float
    h: float  # 지면(이륙 지점) 기준 높이
    yaw_deg: float
    pitch_deg: float
    width: int
    height: int
    focal_px: float
    yaw_estimated: bool = False
    focal_estimated: bool = False


DEFAULT_FOCAL_35MM = 24.0  # 매핑용 드론 카메라의 일반적인 화각 (DJI 광각 카메라 대부분 24 mm 환산)


def focal_px(rec: dict) -> tuple[float, bool]:
    """35mm 환산 초점거리로 화소 단위 초점거리를 구한다. 없으면 24 mm 환산으로 가정한다."""
    diag_px = math.hypot(rec["width"], rec["height"])
    if rec.get("focal_35mm"):
        return rec["focal_35mm"] / DIAG_35MM * diag_px, False
    return DEFAULT_FOCAL_35MM / DIAG_35MM * diag_px, True


def camera_axes(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """영상 x(오른쪽), y(아래쪽), z(광축) 방향을 ENU 좌표로 반환한다 (3×3, 행이 축)."""
    t = math.radians(pitch_deg + 90.0)  # 연직 하방에서 전방으로 기운 각도
    x = np.array([1.0, 0.0, 0.0])
    y = np.array([0.0, -math.cos(t), -math.sin(t)])
    z = np.array([0.0, math.sin(t), -math.cos(t)])
    p = math.radians(yaw_deg)
    # 진북 기준 시계 방향 회전
    rz = np.array([[math.cos(p), math.sin(p), 0.0], [-math.sin(p), math.cos(p), 0.0], [0.0, 0.0, 1.0]])
    return np.stack([rz @ x, rz @ y, rz @ z])


def ground_points(pose: Pose, uv: np.ndarray) -> np.ndarray:
    """영상 좌표(u, v)를 지면(z=0) 좌표(E, N)로 투영한다. 지평선 위 광선은 거리 제한."""
    axes = camera_axes(pose.yaw_deg, pose.pitch_deg)
    cx, cy = pose.width / 2.0, pose.height / 2.0
    d = (
        ((uv[:, 0] - cx) / pose.focal_px)[:, None] * axes[0]
        + ((uv[:, 1] - cy) / pose.focal_px)[:, None] * axes[1]
        + axes[2][None, :]
    )
    dz = np.minimum(d[:, 2], -1e-3)
    t = pose.h / -dz
    xy = t[:, None] * d[:, :2]
    lim = MAX_RANGE_FACTOR * pose.h
    r = np.linalg.norm(xy, axis=1, keepdims=True)
    xy = xy * np.minimum(1.0, lim / np.maximum(r, 1e-9))
    return xy + np.array([pose.e, pose.n])


def footprint(pose: Pose, samples: int = 5) -> Polygon:
    s = np.linspace(0, 1, samples)
    w, h = pose.width, pose.height
    border = np.concatenate(
        [
            np.column_stack([s * w, np.zeros_like(s)]),
            np.column_stack([np.full_like(s, w), s * h]),
            np.column_stack([s[::-1] * w, np.full_like(s, h)]),
            np.column_stack([np.zeros_like(s), s[::-1] * h]),
        ]
    )
    return Polygon(ground_points(pose, border)).buffer(0)


def _estimate_track_yaw(e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """자세 정보가 없을 때 연속 촬영 위치로 진행 방향을 추정한다."""
    k = len(e)
    yaw = np.zeros(k)
    for i in range(k):
        j0, j1 = max(0, i - 1), min(k - 1, i + 1)
        de, dn = e[j1] - e[j0], n[j1] - n[j0]
        yaw[i] = math.degrees(math.atan2(de, dn)) if (de or dn) else 0.0
    return yaw


def _wrap(a: np.ndarray) -> np.ndarray:
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def gimbal_yaw_offset(images: list[dict], min_offset: float = 5.0, max_spread: float = 5.0) -> float | None:
    """짐벌 yaw가 기체 yaw와 일정한 차이로 어긋나 있으면 그 차이를 반환한다.

    매핑 비행에서는 짐벌이 기체 방향을 따라가므로 두 값은 거의 같아야 한다.
    Mavic 2 샘플에서 짐벌 yaw만 약 31° 일정하게 어긋난 사례가 확인됐다(SfM 결과는 기체 yaw와 일치).
    선회 중인 영상은 기체 yaw가 순간적으로 튀므로, 차이의 중앙값과 사분위 범위로 판단한다.
    """
    d = [
        r["gimbal_yaw"] - r["flight_yaw"]
        for r in images
        if r.get("gimbal_yaw") is not None and r.get("flight_yaw") is not None
    ]
    if len(d) < 3:
        return None
    d = _wrap(np.array(d))
    med = float(np.median(d))
    q1, q3 = np.percentile(_wrap(d - med), [25, 75])
    if abs(med) >= min_offset and (q3 - q1) <= max_spread:
        return med
    return None


def build_poses(images: list[dict], proj: UtmProjector) -> tuple[list[Pose], list[str]]:
    warnings: list[str] = []
    imgs = sorted(images, key=lambda r: (r.get("datetime") or "", r["file"]))
    lon = np.array([r["lon"] for r in imgs])
    lat = np.array([r["lat"] for r in imgs])
    e, n = proj.forward(lon, lat)

    # 지면 기준 높이: 상대고도 우선, 없으면 절대고도에서 최저값을 뺀 값(대략값)
    rel = [r.get("rel_alt") for r in imgs]
    if all(v is not None for v in rel):
        h = np.array(rel, dtype=float)
    else:
        absa = np.array([r.get("abs_alt") or 0.0 for r in imgs], dtype=float)
        h = absa - absa.min() + 50.0
        warnings.append("상대고도(XMP)가 없어 비행고도를 대략값으로 가정함: 범위 크기가 부정확할 수 있음")
    if np.any(h < 5):
        warnings.append("비행고도가 5 m 미만인 영상이 있음: 범위 계산이 부정확할 수 있음")
    h = np.maximum(h, 5.0)

    track = _estimate_track_yaw(e, n)
    offset = gimbal_yaw_offset(imgs)
    if offset is not None:
        warnings.append(
            f"짐벌 방향이 기체 방향과 일정하게 {offset:+.0f}° 어긋나 있어 보정함 (DJI 짐벌 yaw 오프셋)"
        )
    poses: list[Pose] = []
    n_yaw_est = n_focal_est = 0
    for i, r in enumerate(imgs):
        yaw = r.get("gimbal_yaw")
        if yaw is not None and offset is not None:
            yaw = yaw - offset
        yaw_est = yaw is None
        if yaw_est:
            yaw = track[i]
            n_yaw_est += 1
        pitch = r.get("gimbal_pitch")
        if pitch is None:
            pitch = -90.0
        f, f_est = focal_px(r)
        n_focal_est += f_est
        poses.append(
            Pose(r["file"], float(e[i]), float(n[i]), float(h[i]), float(yaw), float(pitch),
                 r["width"], r["height"], f, yaw_est, f_est)
        )
    if n_yaw_est:
        warnings.append(f"짐벌 방향 정보가 없는 영상 {n_yaw_est}장: 진행 방향으로 추정함")
    if n_focal_est:
        warnings.append(f"35mm 환산 초점거리가 없는 영상 {n_focal_est}장: 화각을 대략값으로 가정함")
    return poses, warnings


def coverage_grid(
    polys: list[Polygon], bounds: tuple[float, float, float, float], cell: float
) -> np.ndarray:
    xmin, ymin, xmax, ymax = bounds
    cols = int(math.ceil((xmax - xmin) / cell))
    rows = int(math.ceil((ymax - ymin) / cell))
    grid = np.zeros((rows, cols), np.uint16)
    tmp = np.zeros((rows, cols), np.uint8)
    for p in polys:
        tmp[:] = 0
        pts = np.asarray(p.exterior.coords)
        px = np.column_stack([(pts[:, 0] - xmin) / cell, (ymax - pts[:, 1]) / cell])
        cv2.fillPoly(tmp, [np.round(px * 4).astype(np.int32)], 1, shift=2)
        grid += tmp
    return grid


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros(mask.shape, np.uint8)
    cv2.drawContours(out, contours, -1, 1, thickness=cv2.FILLED)
    return out


def survey_area(covered: np.ndarray, bridge_px: float) -> np.ndarray:
    """조사 영역 = 촬영 범위(내부 구멍 포함) + 서로 떨어진 촬영 범위 사이의 틈.

    - 내부 구멍: 사방이 촬영 범위로 둘러싸인 미촬영 구역
    - 틈: 한 줄 비행에서 사진이 여러 장 빠지면 범위가 두 덩어리로 끊어진다.
      bridge_px 반경의 닫힘 연산으로 새로 채워진 영역 중 두 개 이상의 덩어리에 닿는 것만 틈으로 본다.
    - 한 덩어리 외곽의 계단 모양 오목부(선회 구간 등)는 의도된 형태로 보고 제외한다.
    """
    cov = covered.astype(np.uint8)
    area = _fill_holes(cov)
    n, labels = cv2.connectedComponents(cov, connectivity=8)
    if n <= 2:  # 배경 + 덩어리 1개
        return area
    dist_out = cv2.distanceTransform(1 - cov, cv2.DIST_L2, 5)
    dilated = (dist_out <= bridge_px).astype(np.uint8)
    dist_in = cv2.distanceTransform(dilated, cv2.DIST_L2, 5)
    closed = ((dist_in > bridge_px) | (cov == 1)).astype(np.uint8)
    added = (closed == 1) & (area == 0)
    m, add_labels = cv2.connectedComponents(added.astype(np.uint8), connectivity=8)
    ring = np.ones((3, 3), np.uint8)
    for k in range(1, m):
        region = (add_labels == k).astype(np.uint8)
        touch = cv2.dilate(region, ring) & (cov == 1)
        if len(np.unique(labels[touch == 1])) >= 2:
            area[region == 1] = 1
    return _fill_holes(area)


def analyze_coverage(
    grid: np.ndarray, cell: float, border_m: float, min_overlap: int = 2, close_m: float | None = None
) -> dict[str, Any]:
    """조사 영역 안에서 중복도 통계와 누락 구역을 구한다."""
    close_m = 2 * border_m if close_m is None else close_m
    area_mask = survey_area(grid > 0, close_m / cell)
    # 조사 영역 가장자리 띠(영상 반 장 폭)는 원래 중복이 적으므로 저중복 판정에서 제외
    dist = cv2.distanceTransform(area_mask, cv2.DIST_L2, 5)
    interior = (dist > border_m / cell).astype(np.uint8)

    gaps = (area_mask == 1) & (grid == 0)
    low = (interior == 1) & (grid > 0) & (grid < min_overlap)
    cell_area = cell * cell
    counts = grid[area_mask == 1]
    return {
        "survey_area_m2": float(area_mask.sum() * cell_area),
        "gap_area_m2": float(gaps.sum() * cell_area),
        "low_overlap_area_m2": float(low.sum() * cell_area),
        "overlap_median": float(np.median(counts)) if counts.size else 0.0,
        "overlap_p10": float(np.percentile(counts, 10)) if counts.size else 0.0,
        "_gaps_mask": gaps,
        "_low_mask": low,
    }


def mask_to_polygons(mask: np.ndarray, bounds, cell: float, min_area_m2: float) -> list[Polygon]:
    xmin, _, _, ymax = bounds
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        if len(c) < 3:
            continue
        pts = c[:, 0, :].astype(float)
        poly = Polygon(np.column_stack([xmin + pts[:, 0] * cell, ymax - pts[:, 1] * cell])).buffer(0)
        if poly.area >= min_area_m2:
            out.append(poly)
    return out


def forward_overlap(polys: list[Polygon]) -> list[float]:
    """촬영 순서상 연속한 두 영상의 겹침 비율."""
    out = []
    for a, b in zip(polys[:-1], polys[1:]):
        if a.area > 0:
            out.append(a.intersection(b).area / a.area)
    return out


def render_quicklook(
    poses: list[Pose], image_dir: Path, bounds, gsd: float, out: Emitter, power: float = 4.0
) -> np.ndarray:
    """1/8 축소 디코딩한 영상을 평면 가정 호모그래피로 배치해 중심 가중 블렌딩한다."""
    xmin, ymin, xmax, ymax = bounds
    W = int(math.ceil((xmax - xmin) / gsd))
    H = int(math.ceil((ymax - ymin) / gsd))
    acc = np.zeros((H, W, 3), np.float32)
    wsum = np.zeros((H, W), np.float32)
    total = len(poses)

    def load(p: Pose) -> np.ndarray:
        with Image.open(image_dir / p.name) as im:
            im.draft("RGB", (max(1, p.width // 8), max(1, p.height // 8)))  # JPEG 1/8 축소 디코딩
            return np.asarray(im.convert("RGB"))

    # 디코딩은 GIL을 놓으므로 스레드로 병렬 처리하고, 합성은 순서대로 한다
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, (p, small) in enumerate(zip(poses, pool.map(load, poses)), 1):
            _blend_one(p, small, acc, wsum, xmin, ymax, gsd, W, H, power)
            out.progress("quicklook", i, total)
    rgba = np.zeros((H, W, 4), np.uint8)
    valid = wsum > 0
    rgba[valid, :3] = np.clip(acc[valid] / wsum[valid, None] + 0.5, 0, 255).astype(np.uint8)
    rgba[valid, 3] = 255
    return rgba


def _blend_one(p: Pose, small: np.ndarray, acc, wsum, xmin, ymax, gsd, W, H, power) -> None:
    sh, sw = small.shape[:2]
    # 영상 격자(5×5)를 지면으로 투영해 호모그래피를 추정 (경사 촬영 제한 포함)
    g = np.linspace(0, 1, 5)
    gu, gv = np.meshgrid(g * p.width, g * p.height)
    uv = np.column_stack([gu.ravel(), gv.ravel()])
    en = ground_points(p, uv)
    dst = np.column_stack([(en[:, 0] - xmin) / gsd, (ymax - en[:, 1]) / gsd]).astype(np.float32)
    src = (uv * np.array([sw / p.width, sh / p.height])).astype(np.float32)
    Hm, _ = cv2.findHomography(src, dst, 0)
    if Hm is None:
        return
    # 중심 가중치 (영상 경계로 갈수록 0)
    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    edge = np.minimum(np.minimum(xx, sw - 1 - xx), np.minimum(yy, sh - 1 - yy)) / (0.5 * min(sw, sh))
    wimg = np.clip(edge, 0, 1) ** power + 1e-6
    x0, y0, x1, y1 = _dst_bbox(dst, W, H)
    if x1 <= x0 or y1 <= y0:
        return
    T = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], np.float64) @ Hm
    size = (x1 - x0, y1 - y0)
    warped = cv2.warpPerspective(small, T, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    ww = cv2.warpPerspective(wimg, T, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    acc[y0:y1, x0:x1] += warped.astype(np.float32) * ww[..., None]
    wsum[y0:y1, x0:x1] += ww


def _dst_bbox(dst: np.ndarray, W: int, H: int) -> tuple[int, int, int, int]:
    x0 = max(0, int(math.floor(dst[:, 0].min())))
    y0 = max(0, int(math.floor(dst[:, 1].min())))
    x1 = min(W, int(math.ceil(dst[:, 0].max())) + 1)
    y1 = min(H, int(math.ceil(dst[:, 1].max())) + 1)
    return x0, y0, x1, y1


def colorize_coverage(grid: np.ndarray, gaps: np.ndarray) -> np.ndarray:
    """중복 매수를 색으로 표시한다: 1장 빨강, 2장 주황, 3~4장 노랑, 5장 이상 초록, 누락 보라."""
    rgba = np.zeros(grid.shape + (4,), np.uint8)
    palette = [
        (grid == 1, (220, 50, 47)),
        (grid == 2, (240, 140, 30)),
        ((grid >= 3) & (grid <= 4), (230, 200, 40)),
        (grid >= 5, (60, 170, 90)),
        (gaps, (140, 60, 200)),
    ]
    for m, c in palette:
        rgba[m, :3] = c
        rgba[m, 3] = 170
    return rgba


def run_preview(
    folder: Path, out_dir: Path, out: Emitter, max_size: int = 2048, quicklook: bool = True
) -> dict[str, Any]:
    """quicklook=False이면 영상 디코딩 없이 EXIF 기반 촬영 범위·중복도·누락 구역만 계산한다 (데이터 불러오기)."""
    t0 = time.perf_counter()
    folder, out_dir = Path(folder), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scan = scan_folder(folder, emitter=out)
    images = [r for r in scan["images"] if r["selected"] and r["lat"] is not None and r["lon"] is not None]
    if not images:
        raise RuntimeError("GPS 정보가 있는 영상이 없어 미리보기를 만들 수 없음")

    out.stage("footprints", "촬영 범위 계산")
    proj = UtmProjector(float(np.median([r["lon"] for r in images])), float(np.median([r["lat"] for r in images])))
    poses, warnings = build_poses(images, proj)
    polys = [footprint(p) for p in poses]
    union = unary_union(polys)
    xmin, ymin, xmax, ymax = union.bounds

    # 격자: 출력 긴 변이 max_size가 되도록 셀 크기 결정
    cell = max((xmax - xmin), (ymax - ymin)) / max_size
    bounds = (xmin, ymin, xmin + math.ceil((xmax - xmin) / cell) * cell, ymax)
    grid = coverage_grid(polys, bounds, cell)
    short_sides = []
    for poly in polys:
        r = poly.minimum_rotated_rectangle
        xs, ys = r.exterior.coords.xy
        e1 = math.hypot(xs[1] - xs[0], ys[1] - ys[0])
        e2 = math.hypot(xs[2] - xs[1], ys[2] - ys[1])
        short_sides.append(min(e1, e2))
    cov = analyze_coverage(grid, cell, border_m=0.5 * float(np.median(short_sides)))
    gaps_mask = cov["_gaps_mask"]
    gap_polys = mask_to_polygons(cov.pop("_gaps_mask"), bounds, cell, min_area_m2=4 * cell * cell)
    low_polys = mask_to_polygons(cov.pop("_low_mask"), bounds, cell, min_area_m2=4 * cell * cell)
    fwd = forward_overlap(polys)

    quicklook_path: Path | None = None
    if quicklook:
        out.stage("quicklook", "간이 모자이크 생성")
        quick = render_quicklook(poses, folder, bounds, cell, out)
        quicklook_path = out_dir / "quicklook.png"
        Image.fromarray(quick, "RGBA").save(quicklook_path, compress_level=1)
    Image.fromarray(colorize_coverage(grid, gaps_mask), "RGBA").save(out_dir / "coverage.png", compress_level=1)

    # WGS84 변환 (GeoJSON, 지도 오버레이용 네 모서리)
    inv = Transformer.from_crs(proj.epsg, 4326, always_xy=True)

    def to_wgs(geom):
        return affinity_transform(geom, inv)

    features = []
    for p, poly in zip(poses, polys):
        features.append({
            "type": "Feature",
            "properties": {"kind": "footprint", "file": p.name, "yaw_estimated": p.yaw_estimated},
            "geometry": mapping(to_wgs(poly)),
        })
    for poly in gap_polys:
        features.append({"type": "Feature", "properties": {"kind": "gap", "area_m2": poly.area},
                         "geometry": mapping(to_wgs(poly))})
    for poly in low_polys:
        features.append({"type": "Feature", "properties": {"kind": "low_overlap", "area_m2": poly.area},
                         "geometry": mapping(to_wgs(poly))})
    cams = [{"file": p.name, "lon": r_lon, "lat": r_lat} for p, (r_lon, r_lat) in
            zip(poses, zip(*inv.transform([p.e for p in poses], [p.n for p in poses])))]
    for c in cams:
        features.append({"type": "Feature", "properties": {"kind": "camera", "file": c["file"]},
                         "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]}})
    geojson_path = out_dir / "preview.geojson"
    geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
                            encoding="utf-8")

    bx0, by0, bx1, by1 = bounds
    corners_en = [(bx0, by1), (bx1, by1), (bx1, by0), (bx0, by0)]  # 좌상, 우상, 우하, 좌하
    lons, lats = inv.transform([c[0] for c in corners_en], [c[1] for c in corners_en])
    if gap_polys:
        warnings.append(f"촬영 누락 구역 {len(gap_polys)}곳 (총 {sum(p.area for p in gap_polys):.0f} m²)")

    result = {
        "folder": str(folder),
        "num_images": len(poses),
        "epsg": proj.epsg,
        "corners_lonlat": [[float(a), float(b)] for a, b in zip(lons, lats)],
        "cell_m": cell,
        "coverage": {k: round(v, 2) for k, v in cov.items()},
        "forward_overlap_median": round(float(np.median(fwd)), 3) if fwd else None,
        "flight_height_m": {"min": min(p.h for p in poses), "max": max(p.h for p in poses)},
        "gaps": len(gap_polys),
        "outputs": {
            "quicklook": str(quicklook_path) if quicklook_path else None,
            "coverage": str(out_dir / "coverage.png"),
            "geojson": str(geojson_path),
        },
        "warnings": warnings + (["롤링 셔터 카메라 포함"] if scan["summary"]["rolling_shutter_warning"] else []),
        "time_s": round(time.perf_counter() - t0, 2),
    }
    (out_dir / "preview.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def affinity_transform(geom, transformer: Transformer):
    from shapely.ops import transform

    return transform(lambda x, y, z=None: transformer.transform(x, y), geom)
