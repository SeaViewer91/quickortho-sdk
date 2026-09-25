"""정밀 보정 핵심 계산 단위 테스트 (합성 재구성 사용, 실제 드론 영상 불필요)."""

from pathlib import Path

import numpy as np
import pycolmap
import pytest

from quickortho_engine.marking import _guess_columns, parse_gcp_file
from quickortho_engine.refine import (
    add_point,
    apply_sim3,
    bundle_adjust,
    filter_large_errors,
    observation_errors,
    refine_intrinsics_ok,
    reproj_stats,
    resolve_marks,
    triangulate,
    umeyama,
)


def _rot_z(deg: float) -> np.ndarray:
    a = np.deg2rad(deg)
    return np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])


def synthetic_rec(n_points: int = 300, noise_px: float = 0.3, seed: int = 0) -> pycolmap.Reconstruction:
    """고도 100 m에서 연직으로 찍은 3×3 격자 촬영과 지면 점들."""
    rng = np.random.default_rng(seed)
    rec = pycolmap.Reconstruction()
    cam = pycolmap.Camera.create_from_model_name(1, "SIMPLE_RADIAL", 1000.0, 1000, 800)
    rec.add_camera_with_trivial_rig(cam)
    R = np.diag([1.0, -1.0, -1.0])  # 카메라 z축이 아래(-Z)를 향함
    iid = 1
    for cx in (-40.0, 0.0, 40.0):
        for cy in (-30.0, 0.0, 30.0):
            c = np.array([cx, cy, 100.0])
            pose = pycolmap.Rigid3d(pycolmap.Rotation3d(R), -R @ c)
            im = pycolmap.Image(name=f"img{iid}.jpg", camera_id=1, image_id=iid)
            rec.add_image_with_trivial_frame(im, pose)
            iid += 1
    for _ in range(n_points):
        X = np.array([rng.uniform(-60, 60), rng.uniform(-45, 45), rng.normal(0, 2)])
        track = pycolmap.Track()
        for i in rec.reg_image_ids():
            image = rec.image(i)
            uv = image.project_point(X)
            if uv is None or not (0 <= uv[0] < 1000 and 0 <= uv[1] < 800):
                continue
            image.points2D.append(pycolmap.Point2D((uv + rng.normal(0, noise_px, 2)).reshape(2, 1)))
            track.add_element(i, len(image.points2D) - 1)
        if track.length() >= 2:
            rec.add_point3D(X, track)
    rec.update_point_3d_errors()
    return rec


def test_umeyama_recovers_similarity():
    rng = np.random.default_rng(3)
    src = rng.uniform(-50, 50, (20, 3))
    R = _rot_z(12.0)
    dst = 1.3 * src @ R.T + np.array([5.0, -2.0, 7.0])
    s, R2, t = umeyama(src, dst)
    assert s == pytest.approx(1.3, rel=1e-9)
    assert np.allclose(R2, R, atol=1e-9)
    assert np.allclose(t, [5.0, -2.0, 7.0], atol=1e-8)


def test_filter_large_errors_lowers_rmse():
    rec = synthetic_rec()
    # 일부 관측을 크게 틀리게 만든다 (잘못된 매칭)
    t = observation_errors(rec)
    for iid, idx in list(zip(t.image_ids, t.p2d_idx))[::25]:
        rec.image(int(iid)).points2D[int(idx)].xy += np.array([15.0, -10.0])
    before = reproj_stats(rec)
    n = filter_large_errors(rec, 2.0)
    after = reproj_stats(rec)
    assert n > 0
    assert after["rmse_px"] < 1.0 < before["rmse_px"]


def test_triangulate_and_add_manual_point():
    rec = synthetic_rec(n_points=50)
    X = np.array([3.0, -4.0, 1.5])
    marks = []
    for i in rec.reg_image_ids():
        uv = rec.image(i).project_point(X)
        if uv is not None and 0 <= uv[0] < 1000 and 0 <= uv[1] < 800:
            marks.append({"image": rec.image(i).name, "x": float(uv[0]), "y": float(uv[1])})
    marks.append({"image": "없는영상.jpg", "x": 1, "y": 1})
    obs = resolve_marks(rec, marks)
    assert len(obs) == len(marks) - 1 >= 2
    Xt = triangulate(rec, obs)
    assert np.allclose(Xt, X, atol=1e-6)
    n0 = rec.num_points3D()
    pid = add_point(rec, Xt, obs)
    assert rec.num_points3D() == n0 + 1
    assert rec.point3D(pid).track.length() == len(obs)


def test_gcp_bundle_adjustment_recovers_datum():
    """GPS 수준 오차(평행 이동·회전·축척)가 있는 재구성을 GCP 고정점 번들 조정으로 바로잡는다."""
    rec = synthetic_rec(n_points=400, noise_px=0.2)
    gcps_true = np.array([[-45.0, -35.0, 1.0], [45.0, -35.0, -1.0], [45.0, 35.0, 2.0], [-45.0, 35.0, 0.0], [0.0, 0.0, 0.4]])
    marks = []
    for X in gcps_true:
        obs = []
        for i in rec.reg_image_ids():
            uv = rec.image(i).project_point(X)
            if uv is not None and 0 <= uv[0] < 1000 and 0 <= uv[1] < 800:
                obs.append((i, uv))
        marks.append(obs[:3])
    # 재구성을 틀어 놓는다 (GPS 편차 흉내)
    apply_sim3(rec, 1.03, _rot_z(1.0), np.array([3.0, -2.0, 20.0]))
    control = [0, 1, 2, 3]
    src = np.array([triangulate(rec, marks[k]) for k in control])
    s, R, t = umeyama(src, gcps_true[control])
    apply_sim3(rec, s, R, t)
    assert refine_intrinsics_ok(rec, gcps_true[control])  # 높이 차 3 m / 고도 100 m
    pids = {add_point(rec, gcps_true[k], marks[k]) for k in control}
    bundle_adjust(rec, constant_points=pids, refine_intrinsics=True)
    check = triangulate(rec, marks[4])
    assert np.linalg.norm(check - gcps_true[4]) < 0.05


def test_intrinsics_fixed_without_relief():
    rec = synthetic_rec(n_points=50)
    flat = np.array([[-45.0, -35.0, 0.1], [45.0, -35.0, 0.0], [45.0, 35.0, 0.2], [-45.0, 35.0, 0.0]])
    assert not refine_intrinsics_ok(rec, flat)
    assert not refine_intrinsics_ok(rec, flat[:3] + np.array([0, 0, 10.0]) * np.arange(3)[:, None])


def test_bundle_adjust_without_gcp_keeps_scale():
    """GCP 없는 번들 조정은 내부표정을 고정해 초점거리-고도 표류가 없어야 한다."""
    rec = synthetic_rec(n_points=400, noise_px=0.2)
    f0 = rec.camera(1).params.copy()
    X = np.array([0.0, 0.0, 0.0])
    obs = [(i, rec.image(i).project_point(X)) for i in rec.reg_image_ids()][:4]
    bundle_adjust(rec)
    assert np.allclose(rec.camera(1).params, f0)
    assert np.linalg.norm(triangulate(rec, obs) - X) < 0.3


def test_parse_gcp_file_korean_cp949(tmp_path: Path):
    p = tmp_path / "gcp.txt"
    p.write_bytes("점명 X좌표 Y좌표 표고\n1 269300.12 366550.23 20.1\n2 269310.12 366570.23 21.1\n".encode("cp949"))
    d = parse_gcp_file(p)
    assert d["encoding"] == "cp949" and d["delimiter"] == "whitespace"
    assert d["header"][0] == "점명" and d["num_rows"] == 2
    assert d["guess"]["z"] == 3


def test_guess_columns_by_header():
    g = _guess_columns(["id", "lat", "lon", "elev"], [["a", "35", "128", "10"]], [False, True, True, True])
    assert g == {"name": 0, "x": 2, "y": 1, "z": 3}
