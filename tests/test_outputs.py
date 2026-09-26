"""산출물을 변수로 받는 API 시험: 결과 풀기, 래스터 읽기, 카메라·점군, 사진 ↔ 지상 좌표 변환."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import quickortho as qo
from quickortho.geodata import _opk_from_R


def test_unpack_orthomosaic(processed):
    ws, _, res = processed
    image, transform, crs = res
    assert image.shape == (res.height, res.width, 4) and image.dtype == np.uint8
    assert crs.to_epsg() == 32652 and transform == res.transform
    x0, y0 = transform * (0, 0)
    assert (x0, y0) == pytest.approx((res.bounds[0], res.bounds[3]))
    assert (image[..., 3] > 0).mean() > 0.5  # 알파: 자료 있는 곳

    # 워크스페이스만으로 읽기
    img2, tf2, crs2 = qo.read_orthomosaic(ws)
    assert np.array_equal(img2, image) and tf2 == transform


def test_read_options(processed):
    ws, _, res = processed
    rgba, _, _ = res.read()
    bgr, _, _ = res.read(order="BGR")
    assert bgr.shape[2] == 3 and np.array_equal(bgr[..., 0], rgba[..., 2])
    small, tf_s, _ = res.read(scale=0.5)
    assert abs(small.shape[0] - res.height / 2) <= 1 and tf_s.a == pytest.approx(res.gsd_m * res.width / small.shape[1], rel=1e-6)
    b = res.bounds
    sub = (b[0] + 10, b[1] + 10, b[0] + 30, b[1] + 25)
    part, tf_p, _ = res.read(bounds=sub)
    assert abs(part.shape[1] - 20 / res.gsd_m) <= 1 and abs(part.shape[0] - 15 / res.gsd_m) <= 1
    assert tf_p * (0, 0) == pytest.approx((sub[0], sub[3]), abs=res.gsd_m)
    z, tf_z, crs_z = res.read_dsm()
    assert z.dtype == np.float32 and z.ndim == 2 and crs_z.to_epsg() == 32652
    with pytest.raises(qo.InputError):
        res.read(order="XYZ")


def test_read_before_render(tmp_path):
    with pytest.raises(qo.ProjectError) as ei:
        qo.read_orthomosaic(tmp_path)
    assert ei.value.code == "not_rendered"


def test_cameras_match_colmap_and_truth(processed, scene):
    ws, _, _ = processed
    p = qo.Project.open(ws)
    cams = p.cameras()
    assert len(cams) == 12 and cams.names == sorted(scene.cameras)
    # 투영: SDK 투영 = pycolmap 투영, OpenCV 투영 + 0.5 = SDK 투영
    import cv2

    from quickortho._core.project import Project as _Store

    rec, frame, _ = _Store(ws).load_current()
    pid = max(rec.points3D, key=lambda i: rec.point3D(i).track.length())
    X = rec.point3D(pid).xyz
    for el in rec.point3D(pid).track.elements[:3]:
        im = rec.image(el.image_id)
        ref = im.camera.img_from_cam(np.atleast_2d(im.cam_from_world() * X))[0]
        cam = cams[im.name]
        assert np.abs(cam.project(X + frame.origin)[0] - ref).max() < 1e-6
        uv_cv, _ = cv2.projectPoints((X + frame.origin).reshape(1, 1, 3), cam.rvec, cam.t, cam.K, cam.dist)
        assert np.abs(uv_cv.reshape(2) + 0.5 - ref).max() < 1e-6
        # 광선 방향
        d = cam.rays(ref)[0]
        v = (X + frame.origin) - cam.center
        assert np.degrees(np.arccos(np.clip(d @ v / np.linalg.norm(v), -1, 1))) < 1e-4
    # 자세각: 합성 영상의 참값과 비교 (GPS 오차는 평행 이동이라 회전은 같아야 함)
    for cam in cams:
        truth = _opk_from_R(scene.rotations[cam.name].T)
        diff = (cam.opk - truth + 180) % 360 - 180
        assert np.abs(diff).max() < 0.5, (cam.name, cam.opk, truth)
        assert np.abs(qo.rotation_from_opk(*cam.opk) - cam.R).max() < 1e-9
    assert np.allclose(_opk_from_R(np.diag([1.0, -1.0, -1.0])), 0)  # 연직·북쪽 위 → (0, 0, 0)
    # 카메라 위치: GPS 오차(3, -2, 4) m만큼 어긋나 있음
    true_c = np.array([scene.cameras[n] for n in cams.names]) + [scene.origin[0], scene.origin[1], 0]
    off = (cams.centers - true_c).mean(axis=0)
    assert off == pytest.approx([3.0, -2.0, 4.0], abs=0.3)


def test_image_ground_roundtrip(processed):
    ws, _, _ = processed
    p = qo.Project.open(ws)
    cam = p.cameras()[5]
    pts = p.points().filter(max_error_px=0.5, min_track_length=4)
    uv = p.ground_to_image(cam.name, pts.xyz)
    m = cam.in_image(uv) & ~np.isnan(uv).any(axis=1)
    X, uvm = pts.xyz[m][:300], uv[m][:300]
    assert len(X) > 50
    G = p.image_to_ground(cam.name, uvm)                       # DSM과 교차
    assert np.median(np.linalg.norm((G - X)[:, :2], axis=1)) < 0.5
    Gz = p.image_to_ground(cam.name, uvm[:1], z=float(X[0, 2]))  # 수평면과 교차: 정확히 돌아옴
    assert np.abs(Gz[0] - X[0]).max() < 1e-6
    assert p.image_to_ground(cam.name, np.empty((0, 2))).shape == (0, 3)
    with pytest.raises(KeyError):
        p.ground_to_image("없는사진.JPG", X[:1])


def test_point_cloud_and_exports(processed, tmp_path):
    ws, _, _ = processed
    p = qo.Project.open(ws)
    pts = p.points()
    xyz, rgb = pts
    assert xyz.shape == (len(pts), 3) and rgb.dtype == np.uint8 and len(pts.ids) == len(pts)
    good = pts.filter(max_error_px=0.5)
    assert 0 < len(good) <= len(pts) and good.error.max() <= 0.5
    ply = pts.to_ply(tmp_path / "p.ply").read_bytes()
    assert f"element vertex {len(pts)}".encode() in ply[:300]
    csv = p.cameras().to_csv(tmp_path / "c.csv").read_text(encoding="utf-8").splitlines()
    assert csv[0] == "name,x,y,z,omega,phi,kappa" and len(csv) == 13
    d = p.cameras()[0].to_dict()
    json.dumps(d)
    assert set(d) >= {"K", "dist", "R", "t", "center", "opk_deg"}


def test_unpack_scan_and_preview(scene, tmp_path):
    images, summary = qo.scan(scene.folder)
    assert len(images) == 12 and summary["selected"] == 12
    assert qo.scan(scene.folder).positions().shape == (12, 3)

    pv = qo.preview(scene.folder, tmp_path / "pv", qo.PreviewOptions(max_size=512))
    quicklook, coverage, transform, crs = pv
    assert quicklook.shape[:2] == coverage.shape and quicklook.shape[2] == 4
    assert coverage.dtype == np.uint16 and coverage.max() >= 3
    assert crs.to_epsg() == 32652 and transform.a == pytest.approx(pv.raw["cell_m"])
    assert transform * (0, 0) == pytest.approx((pv.bounds[0], pv.bounds[3]))
    assert len(pv.footprints()) == 12 and len(pv.footprint_names) == 12
    assert pv.footprints()[0].area > 1000  # m², 지도 좌표
    assert pv.read_gap_mask().shape == coverage.shape
    assert pv.quicklook == pv.quicklook_path  # 0.1.0 호환: 경로


def test_unpack_align_result(processed):
    ws, _, _ = processed
    report = json.loads((ws / "project" / "align_report.json").read_text(encoding="utf-8"))
    a = qo.AlignResult.from_dict(report, ws)
    cameras, points, crs = a
    assert len(cameras) == 12 and len(points) == a.num_points and crs.to_epsg() == 32652


# ───────────────────────── CLI (0.2.0) ─────────────────────────


def test_cli_text_format(workspace, capsys):
    from quickortho.cli import main

    assert main(["render", str(workspace), "--gsd-scale", "4", "--format", "text"]) == 0
    out = capsys.readouterr().out
    assert "▶" in out and "정사 모자이크" in out and not out.lstrip().startswith("{")
    assert main(["--format=text", "render", "/없는/폴더"]) == 1
    err = capsys.readouterr().err
    assert "[not_aligned]" in err


def test_cli_json_default_when_piped(workspace, capsys):
    from quickortho.cli import main

    assert main(["render", str(workspace), "--gsd-scale", "4", "--dsm", "plane", "--dsm-z", "5"]) == 0
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last["type"] == "result" and last["data"]["dsm"]["mode"] == "plane"


def test_cli_export_and_doctor(workspace, tmp_path, capsys):
    from quickortho.cli import main

    assert main(["export", str(workspace), "--cameras-csv", str(tmp_path / "c.csv"),
                 "--cameras-json", str(tmp_path / "c.json"), "--points-ply", str(tmp_path / "p.ply")]) == 0
    assert (tmp_path / "c.csv").exists() and (tmp_path / "p.ply").exists()
    cams = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert cams["epsg"] == 32652 and len(cams["cameras"]) == 12
    capsys.readouterr()
    assert main(["doctor", str(tmp_path)]) == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert res["ok"] and any(c["name"] == "기능 시험" for c in res["checks"])
    assert qo.doctor(tmp_path)["ok"]
