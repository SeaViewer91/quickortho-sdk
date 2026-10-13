"""수면 처리 전체 경로 시험: 합성 바다 장면(왼쪽이 무늬 없는 청록 수면)으로 판별 → 특징점 제외 → SfM → DSM 수면 평면.

바다는 높이 0 m(SEA_Z), 육지는 물가에서 10 m에 걸쳐 원래 지형으로 올라감. 수면에는 SfM 점이 생기지 않으므로
수면 처리 없이는 간이 DSM이 수면을 육지 높이로 외삽함.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

import quickortho as qo
import synthetic as syn
from conftest import FAST_SFM
from quickortho.cli import main

SEA_E = 5.0


@pytest.fixture(scope="module")
def sea_scene(tmp_path_factory) -> syn.Scene:
    return syn.make_scene(tmp_path_factory.mktemp("sea") / "imgs", sea_e=SEA_E)


@pytest.fixture(scope="module")
def sea_processed(sea_scene, tmp_path_factory):
    ws = tmp_path_factory.mktemp("seaws") / "ws"
    events: list[qo.Event] = []
    res = qo.Project.create(sea_scene.folder, ws).process(
        qo.OrthoOptions(sfm=FAST_SFM, water="on", gsd_scale=2), on_event=events.append)
    return ws, events, res


@pytest.fixture
def sea_ws(sea_processed, tmp_path) -> Path:
    dst = tmp_path / "seaws"
    shutil.copytree(sea_processed[0], dst)
    return dst


def _dsm_errors(scene: syn.Scene, z, tf):
    """(수면 오차 중앙값, 육지 오차 중앙값, 수면 칸 마스크) — DSM과 참 높이 비교."""
    e0, n0 = Transformer.from_crs(4326, syn.EPSG, always_xy=True).transform(syn.LON0, syn.LAT0)
    rows, cols = np.mgrid[0:z.shape[0], 0:z.shape[1]]
    x, y = tf * (cols + 0.5, rows + 0.5)
    e, n = x - e0, y - n0
    sea = (e < SEA_E - 7) & (e > -30) & (n > 10) & (n < 80)
    land = (e > 20) & (e < 100) & (n > 0) & (n < 90)
    t = scene.height(e, n)
    return float(np.median(np.abs(z[sea] - t[sea]))), float(np.median(np.abs(z[land] - t[land]))), sea


def test_water_on_flattens_sea(sea_processed, sea_scene):
    ws, events, res = sea_processed
    stages = [e.stage for e in events if e.type == "stage"]
    assert stages[:2] == ["scan", "water"] and stages.index("water") < stages.index("features")
    assert all(s in qo.STAGES for s in stages)
    w = res.water
    assert w is not None and w.mode == "on" and w.active and w.applied and w.source == "auto"
    assert 0.1 < w.image_fraction < 0.4 and w.images_with_water >= 4
    assert w.feature_masked_images == w.images_with_water  # 넓은 수면이 보이는 영상만 특징점 제외
    assert len(w.levels_m) == 1 and abs(w.levels_m[0] - syn.SEA_Z) < 0.3  # 물가 높이 = 수면 높이
    assert w.area_m2 > 5000 and w.components[0]["water"] and w.components[0]["point_ratio"] < 0.05
    rep = res.report["water"]
    for k in ("mode", "active", "image_fraction", "images_with_water", "confident_fraction", "feature_masked_images",
              "candidate_cells", "components", "water_components", "area_m2", "land_point_density", "detail"):
        assert k in rep, k
    assert res.report["sfm"]["num_registered"] >= 11
    sea_err, land_err, _ = _dsm_errors(sea_scene, *res.read_dsm()[:2])
    assert sea_err < 0.1 and land_err < 0.3
    # 판별 결과 저장 (render·refine에서 다시 씀)
    masks = qo.Project.open(ws).water_masks()
    assert sorted(masks) == sorted(p.name for p in sea_scene.folder.glob("*.JPG"))
    assert all(max(m.shape) == qo.water.MASK_PX for m in masks.values())
    assert set(np.unique(np.concatenate([np.unique(m) for m in masks.values()]))) <= {0, 1, 2}
    align = json.loads((ws / "project" / "align_report.json").read_text(encoding="utf-8"))
    assert align["water"]["active"] and align["water"]["feature_masked_images"] == w.feature_masked_images


def test_water_off_render_matches_unflattened(sea_ws, sea_scene, sea_processed):
    """같은 정렬로 off면 수면 평면 없음 (0.4.x와 같은 DSM): 수면 오차가 크고 육지는 같음."""
    p = qo.Project.open(sea_ws)
    events: list[qo.Event] = []
    r = p.orthomosaic(qo.OrthoOptions(water="off", gsd_scale=2), on_event=events.append)
    assert "water" not in [e.stage for e in events if e.type == "stage"]
    assert r.water.mode == "off" and not r.water.active and not r.water.applied
    assert r.water.feature_masked_images > 0  # 정렬 때의 정보는 남음
    sea_off, land_off, _ = _dsm_errors(sea_scene, *r.read_dsm()[:2])
    sea_on, land_on, _ = _dsm_errors(sea_scene, *sea_processed[2].read_dsm()[:2])
    assert sea_off > 3 * sea_on and sea_off > 0.15
    assert abs(land_off - land_on) < 0.02
    meta = json.loads((sea_ws / "project" / "meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["water"] == "off"
    # 저장된 설정(off)을 refine이 그대로 씀
    p.set_edits(max_reproj_error_px=1.5)
    assert not p.refine().ortho.water.applied


def test_refine_reuses_saved_masks_and_old_settings_off(sea_ws):
    p = qo.Project.open(sea_ws)
    p.set_edits(max_reproj_error_px=1.5)
    events: list[qo.Event] = []
    rr = p.refine(on_event=events.append)
    assert "water" not in [e.stage for e in events if e.type == "stage"]  # 다시 판별하지 않음
    assert rr.ortho.water.applied and rr.ortho.water.mode == "on"
    # 0.5.0 전 설정(수면 설정 없음)은 끔
    meta_p = sea_ws / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["render"].pop("water")
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    w = p.refine().ortho.water
    assert w.mode == "off" and not w.applied


def test_render_time_detection_when_aligned_without_water(sea_scene, tmp_path):
    """정렬을 off로 했어도 정사에서 켜면 그때 판별해 DSM 수면 평면을 씀 (특징점 제외는 없음)."""
    p = qo.Project.create(sea_scene.folder, tmp_path / "ws")
    a = p.align(qo.OrthoOptions(sfm=FAST_SFM, water="off"))
    assert a.water is None and not (tmp_path / "ws" / "project" / "water").exists()
    events: list[qo.Event] = []
    r = p.orthomosaic(qo.OrthoOptions(water="auto", gsd_scale=2), on_event=events.append)
    stages = [e.stage for e in events if e.type == "stage"]
    assert stages[0] == "water" and stages[1] == "dsm"
    assert r.water.active and r.water.applied and r.water.feature_masked_images == 0
    assert "water_s" in r.report["timings_s"]
    # 두 번째부터는 저장된 판별 결과를 씀
    events.clear()
    p.orthomosaic(qo.OrthoOptions(water="on", gsd_scale=4), on_event=events.append)
    assert "water" not in [e.stage for e in events if e.type == "stage"]
    # 외부·평면 DSM에는 적용하지 않음
    r3 = p.orthomosaic(qo.OrthoOptions(water="on", gsd_scale=4, dsm="plane"))
    assert not r3.water.active and "note" in r3.water.report


def test_user_masks(sea_ws, sea_scene):
    p = qo.Project.open(sea_ws)
    auto = p.water_masks()
    with pytest.raises(qo.InputError) as e1:
        p.set_water_masks({"없는영상.JPG": np.ones((10, 10))})
    assert e1.value.code == "invalid_argument"
    with pytest.raises(qo.InputError):
        p.set_water_masks({"DJI_0000.JPG": np.ones(10)})
    for bad in (str(sea_scene.folder / "DJI_0000.JPG"), "../imgs/DJI_0000.JPG"):  # 영상 폴더 밖으로 쓰지 않음
        with pytest.raises(qo.InputError):
            p.set_water_masks({bad: np.ones((10, 10))})
    assert not list(sea_scene.folder.glob("*.png"))
    # 자기 마스크(bool, 원본 해상도)는 확실한 수면으로 저장
    user = {k: np.kron(v > 0, np.ones((2, 2), bool)) for k, v in auto.items()}
    assert p.set_water_masks(user) == len(user)
    saved = p.water_masks()
    assert all(max(m.shape) <= 480 and set(np.unique(m)) <= {0, 2} for m in saved.values())
    r = p.orthomosaic(qo.OrthoOptions(water="on", gsd_scale=4))
    assert r.water.source == "user" and r.water.applied
    # 폴더에서 넣기 (<영상 이름>.png)
    d = sea_ws / "mymasks"
    d.mkdir()
    import cv2

    for k, v in auto.items():
        cv2.imwrite(str(d / f"{k}.png"), (v > 0).astype(np.uint8) * 255)
    assert p.set_water_masks(d) == len(auto)
    # 다시 정렬해도 사용자 마스크는 그대로 씀
    a = p.align(qo.OrthoOptions(sfm=FAST_SFM, water="on"))
    assert a.water.source == "user" and a.water.feature_masked_images > 0
    assert json.loads((sea_ws / "project" / "water.json").read_text(encoding="utf-8"))["source"] == "user"
    # None이면 지움 → 다음 정렬은 다시 자동 판별
    assert p.set_water_masks(None) == 0 and p.water_masks() == {}


def test_public_detect_and_flatten(sea_processed, sea_scene, tmp_path):
    ws, _, res = sea_processed
    det = qo.water.detect(sea_scene.folder)
    saved = qo.water.read_masks(ws)
    assert sorted(det.masks) == sorted(saved)
    assert all(np.array_equal(det.masks[k], saved[k]) for k in saved)  # 같은 판별
    assert det.active("auto") and det.active("on") and not det.active("off")
    assert abs(det.fraction - res.water.image_fraction) < 1e-3
    files = sorted(sea_scene.folder.glob("*.JPG"))[:2]
    d2 = qo.water.detect(files)
    assert sorted(d2.masks) == [f.name for f in files]
    m = qo.water.classify(files[0])
    assert np.array_equal(m, saved[files[0].name])
    with pytest.raises(qo.InputError):
        qo.water.classify(np.zeros((10, 10)))
    with pytest.raises(qo.InputError):
        qo.water.normalize_mode("sea")
    # 이미 만든 결과에 수면 평면만 따로 (off로 만든 DSM에 적용하면 on 결과와 거의 같음)
    p = qo.Project.open(ws)
    off_ws = tmp_path / "off"
    shutil.copytree(ws, off_ws)
    z, tf, _ = qo.Project.open(off_ws).orthomosaic(qo.OrthoOptions(water="off", gsd_scale=2)).read_dsm()
    z2, wmask, level, stats = qo.water.flatten_dsm(z, tf, p.cameras(), saved, p.points())
    z_on, _, _ = res.read_dsm()
    assert stats["water_components"] == 1 and abs(stats["area_m2"] - res.water.area_m2) / res.water.area_m2 < 0.05
    assert np.nanmedian(np.abs(z2[wmask] - z_on[wmask])) < 0.05
    assert np.allclose(z2[~wmask], z[~wmask], equal_nan=True)


def test_water_option_validation_and_cli(sea_ws, capsys):
    p = qo.Project.open(sea_ws)
    with pytest.raises(qo.InputError) as e:
        p.orthomosaic(qo.OrthoOptions(water="sea"), on_event=lambda ev: pytest.fail("처리 시작됨"))
    assert e.value.code == "invalid_argument"
    r = p.orthomosaic(qo.OrthoOptions(water=" ON ", gsd_scale=4))
    assert r.water.mode == "on"
    assert qo.OrthoOptions(water="on").render_dict()["water"] == "on"
    assert qo.OrthoOptions.from_render_dict({"water": "off"}).water == "off"
    assert main(["render", str(sea_ws), "--gsd-scale", "4", "--water", "off", "--format", "json"]) == 0
    ev = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    assert ev[-1]["data"]["water"]["mode"] == "off"
    assert main(["render", str(sea_ws), "--water", "sea", "--format", "json"]) != 0
    capsys.readouterr()
    assert main(["render", str(sea_ws), "--gsd-scale", "4", "--format", "text"]) == 0
    assert "수면" in capsys.readouterr().out
