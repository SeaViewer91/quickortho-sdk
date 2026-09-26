"""출력 좌표계, 출력 격자 고정, 평면·외부 DSM 시험."""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

import quickortho as qo
from conftest import FAST_SFM, GPS_BIAS


def test_output_epsg(scene, tmp_path):
    p = qo.Project.create(scene.folder, tmp_path / "ws")
    a = p.align(qo.OrthoOptions(sfm=FAST_SFM, epsg=5187))  # GRS80 동부원점
    assert a.epsg == 5187
    cams = p.cameras()
    # 카메라 위치를 경위도로 바꾸면 참값(+GPS 오차) 근처여야 함
    to_ll = Transformer.from_crs(5187, 4326, always_xy=True)
    utm_to_ll = Transformer.from_crs(32652, 4326, always_xy=True)
    name = cams.names[0]
    e, n, _ = scene.cameras[name]
    lon_t, lat_t = utm_to_ll.transform(scene.origin[0] + e + GPS_BIAS[0], scene.origin[1] + n + GPS_BIAS[1])
    lon, lat = to_ll.transform(*cams[name].center[:2])
    assert abs(lon - lon_t) < 1e-5 and abs(lat - lat_t) < 1e-5
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4))
    assert r.epsg == 5187 and r.crs.to_epsg() == 5187


def test_output_epsg_rejects_geographic(scene, tmp_path):
    with pytest.raises(qo.InputError) as ei:
        qo.Project.create(scene.folder, tmp_path / "ws").align(qo.OrthoOptions(epsg=4326))
    assert ei.value.code == "invalid_argument"


def test_fixed_grid(workspace):
    p = qo.Project.open(workspace)
    res = p.result()
    b = res.bounds
    fixed = (b[0] + 5.3, b[1] + 7.1, b[2] - 4.2, b[3] - 3.3)
    opts = qo.OrthoOptions(gsd_m=0.5, bounds=fixed, grid_origin=(0.25, 0.25))
    r1 = p.orthomosaic(opts)
    r2 = p.orthomosaic(opts)
    assert r1.transform == r2.transform and (r1.width, r1.height) == (r2.width, r2.height)
    x0, y1 = r1.transform * (0, 0)
    # 경계가 기준점에서 GSD 배수 위치이고, 요청 범위를 덮음
    assert ((x0 - 0.25) / 0.5) == pytest.approx(round((x0 - 0.25) / 0.5), abs=1e-6)
    assert ((y1 - 0.25) / 0.5) == pytest.approx(round((y1 - 0.25) / 0.5), abs=1e-6)
    x1, y0 = r1.transform * (r1.width, r1.height)
    assert x0 <= fixed[0] < x0 + 0.5 and x1 - 0.5 < fixed[2] <= x1
    assert y0 <= fixed[1] < y0 + 0.5 and y1 - 0.5 < fixed[3] <= y1
    # 옵션이 저장되어 보정 후 재생성에도 같은 격자를 씀
    rr = p.refine()
    assert rr.ortho.transform == r1.transform and rr.ortho.width == r1.width
    meta = json.loads((workspace / "project" / "meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["bounds"] == list(fixed)


def test_plane_dsm(workspace):
    p = qo.Project.open(workspace)
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, dsm="plane", dsm_z=10.0))
    z, _, _ = r.read_dsm()
    assert np.allclose(z, 10.0) and r.report["dsm"]["mode"] == "plane" and r.report["dsm"]["plane_z"] == 10.0
    r2 = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, dsm="plane"))  # 높이는 희소 점군 중앙값
    z2, _, _ = r2.read_dsm()
    assert np.ptp(z2) < 1e-4 and 0 < float(z2.mean()) < 30


def test_external_dsm_with_datum_offset(workspace, scene, tmp_path):
    # 외부 DEM: 참 지형 + 25 m (기준면 차이를 흉내), GRS80 동부원점, 2 m 격자
    to5187 = Transformer.from_crs(32652, 5187, always_xy=True)
    from5187 = Transformer.from_crs(5187, 32652, always_xy=True)
    cx, cy = to5187.transform(scene.origin[0] + 40, scene.origin[1] + 40)
    res, half = 2.0, 150
    xs = cx - half + (np.arange(150) + 0.5) * res
    ys = cy + half - (np.arange(150) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    E, N = from5187.transform(X, Y)
    Z = scene.height(E - scene.origin[0], N - scene.origin[1]) + 25.0
    dem = tmp_path / "dem.tif"
    with rasterio.open(dem, "w", driver="GTiff", width=150, height=150, count=1, dtype="float32",
                       crs="EPSG:5187", transform=from_origin(cx - half, cy + half, res, res), nodata=-9999) as ds:
        ds.write(Z.astype(np.float32), 1)

    p = qo.Project.open(workspace)
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, dsm=str(dem)))
    info = r.report["dsm"]
    assert info["mode"] == "external" and info["source"].endswith("dem.tif")
    # 재구성 높이 = 참값 + GPS 고도 오차(4 m) → 보정량 ≈ 4 - 25 = -21 m
    assert info["vertical_offset_m"] == pytest.approx(GPS_BIAS[2] - 25.0, abs=1.0)
    r_raw = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, dsm=str(dem), dsm_vertical_align=False))
    assert r_raw.report["dsm"]["vertical_offset_m"] == 0.0

    with pytest.raises(qo.InputError):
        p.orthomosaic(qo.OrthoOptions(dsm=str(tmp_path / "없음.tif")))


def test_refine_crs_change_drops_old_grid(workspace, scene):
    from test_api import GCP_XY, _synthetic_gcps

    p = qo.Project.open(workspace)
    b = p.result().bounds
    p.orthomosaic(qo.OrthoOptions(gsd_scale=4, bounds=(b[0] + 5, b[1] + 5, b[2] - 5, b[3] - 5)))
    cam_before = p.cameras()[0].center.copy()
    p.ground_to_image(p.cameras()[0].name, cam_before[None] + [0, 0, -50])  # 카메라 캐시 채움
    gcps = _synthetic_gcps(scene, GCP_XY, check=("C1", "C2"))
    tr = Transformer.from_crs(32652, 5187, always_xy=True)
    for g in gcps:
        g.x, g.y = tr.transform(g.x, g.y)
        g.epsg = 5187
    p.set_edits(gcps=gcps)
    rr = p.refine()
    assert rr.ortho.epsg == 5187 and any("bounds" in w for w in rr.warnings)
    assert rr.check["rmse_xy"] < 0.15
    # 카메라 캐시가 보정 결과를 따라감 (좌표계가 바뀌었으므로 위치 값이 달라짐)
    assert p.ground_to_image(p.cameras()[0].name, p.cameras()[0].center[None] + [0, 0, -50]).shape == (1, 2)
    assert np.abs(p._camera(p.cameras()[0].name).center - cam_before).max() > 1000


def test_cli_arg_error_exit_code(workspace, capsys):
    from quickortho.cli import main

    assert main(["export", str(workspace)]) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["code"] == "invalid_argument"
