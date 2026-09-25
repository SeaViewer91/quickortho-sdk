"""빠른 미리보기 테스트 (합성 데이터)."""

import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import box

from quickortho_engine.preview import (
    Pose,
    analyze_coverage,
    camera_axes,
    coverage_grid,
    footprint,
    gimbal_yaw_offset,
    run_preview,
)
from quickortho_engine.protocol import Emitter

from test_scan import _xmp, make_jpeg


def _pose(yaw=0.0, pitch=-90.0, h=100.0, w=400, hgt=300, f=400.0) -> Pose:
    return Pose("x", 0.0, 0.0, h, yaw, pitch, w, hgt, f)


def test_camera_axes_nadir_north_up():
    ax = camera_axes(0.0, -90.0)
    assert np.allclose(ax[0], [1, 0, 0], atol=1e-9)  # 영상 오른쪽 = 동
    assert np.allclose(ax[1], [0, -1, 0], atol=1e-9)  # 영상 아래 = 남
    assert np.allclose(ax[2], [0, 0, -1], atol=1e-9)  # 광축 = 연직 하방


def test_camera_axes_yaw_90_top_points_east():
    ax = camera_axes(90.0, -90.0)
    assert np.allclose(-ax[1], [1, 0, 0], atol=1e-9)  # 영상 위쪽 = 동
    assert np.allclose(ax[0], [0, -1, 0], atol=1e-9)  # 영상 오른쪽 = 남


def test_footprint_size_and_rotation():
    # f=400px, 400×300px, 고도 100 m → 지면 100 m × 75 m
    fp = footprint(_pose())
    xmin, ymin, xmax, ymax = fp.bounds
    assert math.isclose(xmax - xmin, 100, rel_tol=1e-6) and math.isclose(ymax - ymin, 75, rel_tol=1e-6)
    fp90 = footprint(_pose(yaw=90.0))
    xmin, ymin, xmax, ymax = fp90.bounds
    assert math.isclose(xmax - xmin, 75, rel_tol=1e-6) and math.isclose(ymax - ymin, 100, rel_tol=1e-6)


def test_oblique_footprint_is_limited():
    fp = footprint(_pose(pitch=-10.0))  # 거의 수평
    xmin, ymin, xmax, ymax = fp.bounds
    assert max(abs(xmin), abs(xmax), abs(ymin), abs(ymax)) <= 400 + 1e-6


def test_gimbal_yaw_offset_detects_constant_bias():
    recs = [{"gimbal_yaw": -120.5 + d, "flight_yaw": -89.8} for d in (0, 0.2, -0.3, 0.1)]
    recs += [{"gimbal_yaw": 58.6, "flight_yaw": 89.5}, {"gimbal_yaw": -120.9, "flight_yaw": -49.4}]  # 선회
    off = gimbal_yaw_offset(recs)
    assert off is not None and abs(off + 31) < 1.5
    ok = [{"gimbal_yaw": 156.3, "flight_yaw": 155.0}, {"gimbal_yaw": -22.7, "flight_yaw": -24.0},
          {"gimbal_yaw": 150.2, "flight_yaw": 155.1}]
    assert gimbal_yaw_offset(ok) is None


def test_coverage_gap_detection():
    # 3×3 배치에서 가운데 한 장이 빠진 경우 → 가운데가 누락 구역
    polys = [box(i * 90, j * 90, i * 90 + 100, j * 90 + 100) for i in range(3) for j in range(3) if (i, j) != (1, 1)]
    bounds = (0, 0, 280, 280)
    grid = coverage_grid(polys, bounds, cell=1.0)
    cov = analyze_coverage(grid, 1.0, border_m=20)
    assert 60 * 60 * 0.8 < cov["gap_area_m2"] < 80 * 80 * 1.2
    full = coverage_grid(polys + [box(90, 90, 190, 190)], bounds, cell=1.0)
    assert analyze_coverage(full, 1.0, border_m=20)["gap_area_m2"] == 0


def test_run_preview_end_to_end(tmp_path: Path):
    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    # 동서 방향 한 줄 비행 6장, 고도 100 m. 3번째 영상 누락 → 누락 구역이 생겨야 함
    # FC6310S(24 mm 환산), 160×120 → 지면 폭 약 150 m × 112 m
    lon0, lat0 = 129.0, 35.1
    m_per_deg_lon = 111_320 * math.cos(math.radians(lat0))
    for i in [0, 1, 3, 4, 5]:
        make_jpeg(
            img_dir / f"DJI_{i:04d}.JPG", model="FC6310S", focal=8.8, focal35=24, size=(160, 120),
            lat=lat0, lon=lon0 + i * 60 / m_per_deg_lon, color=(40 * i, 100, 200),
            xmp=_xmp(RelativeAltitude="+100.0", GimbalPitchDegree="-90.0",
                     GimbalYawDegree="+90.0", FlightYawDegree="+90.0"),
        )
    # 간격 180 m 구간(1→3)은 폭 약 112 m 영상으로 덮이지 않음
    out = tmp_path / "out"
    res = run_preview(img_dir, out, Emitter(__import__("io").StringIO()), max_size=512)
    assert res["num_images"] == 5
    assert res["gaps"] >= 1 and res["coverage"]["gap_area_m2"] > 0
    for key in ("quicklook", "coverage", "geojson"):
        assert Path(res["outputs"][key]).exists()
    gj = json.loads(Path(res["outputs"]["geojson"]).read_text(encoding="utf-8"))
    kinds = {f["properties"]["kind"] for f in gj["features"]}
    assert {"footprint", "gap", "camera"} <= kinds
    # 네 모서리는 좌상→우상→우하→좌하 순서이고 경위도 범위 안
    (lo0, la0), (lo1, _), (_, la2), _ = res["corners_lonlat"]
    assert lo0 < lo1 and la2 < la0
