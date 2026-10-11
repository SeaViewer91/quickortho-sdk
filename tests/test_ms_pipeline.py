"""다중분광 전체 경로 시험: 합성 MicaSense 형식 비행으로 스캔 → 기준 밴드 SfM → 밴드 정합·반사율 → 정사 → 지수 → 보정."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

import quickortho as qo
import synthetic as syn
import synthetic_ms as sm
from conftest import FAST_SFM
from quickortho.cli import main


@pytest.fixture(scope="module")
def ms_scene(tmp_path_factory) -> sm.MsScene:
    return sm.make_ms_scene(tmp_path_factory.mktemp("ms") / "imgs")


@pytest.fixture(scope="module")
def ms_processed(ms_scene, tmp_path_factory):
    ws = tmp_path_factory.mktemp("msws") / "ws"
    events: list[qo.Event] = []
    res = qo.Project.create(ms_scene.folder, ws).process(qo.OrthoOptions(sfm=FAST_SFM), on_event=events.append)
    return ws, events, res


@pytest.fixture
def ms_workspace(ms_processed, tmp_path) -> Path:
    dst = tmp_path / "ws"
    shutil.copytree(ms_processed[0], dst)  # 기준 밴드 영상(project/msref)은 워크스페이스 기준으로 찾으므로 사본도 독립적으로 동작함
    return dst


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_scan_multispectral_folder(ms_scene):
    s = qo.scan(ms_scene.folder)
    ms = s.multispectral
    assert ms["reference_band"] == "Green" and [b["name"] for b in ms["bands"]] == ["Green", "Red", "NIR"]
    assert ms["captures"] == 12 and ms["panel_captures"] == 1 and ms["has_dls"]
    sel = s.selected_images
    assert len(sel) == 12 and all(im["band"] == "Green" for im in sel)  # 정렬에는 기준 밴드만
    assert len(s.images) == 39 and any("panel" in im["flags"] for im in s.images)
    # 패널 캡처가 먼저 정렬되어도 기준 밴드 카메라는 선택됨, 보조 밴드만 제외 목록에 있음
    green = [k for k in s.cameras if "| Green |" in k]
    assert len(green) == 1 and s.cameras[green[0]]["selected"] and green[0] not in s.excluded_cameras
    assert sum("다중분광 보조 밴드" in v for v in s.excluded_cameras.values()) == 2
    assert qo.multispectral.scan(ms_scene.folder).band_names() == ["Green", "Red", "NIR"]


def test_ms_process_end_to_end(ms_processed, ms_scene):
    ws, events, res = ms_processed
    rep = res.report
    stages = [e.stage for e in events if e.type == "stage"]
    assert stages == ["scan", "scan", "features", "matching", "mapping", "georef", "dsm", "bands", "ortho", "finalize"]
    assert all(s in qo.STAGES for s in stages)
    assert rep["sfm"]["num_registered"] == 12 and rep["input"]["multispectral"]["captures"] == 12
    assert (ws / "project" / "msref" / "IMG_0000_1.tif.jpg").exists()
    info = res.multispectral
    assert info is not None and info.bands == ["Green", "Red", "NIR"] and info.reference_band == "Green"
    assert info.values == "reflectance" and info.radiometry == "dls+panel"
    assert info.index_names == ["NDVI", "GNDVI"] and info.indices.exists() and info.index_preview.exists()
    assert max(info.band_alignment_px.values()) < 0.2
    assert all(after < 2.0 for _, after in info.capture_inconsistency_pct.values())
    assert rep["ortho"]["color_balance"] is False and "color" not in stages
    assert rep["multispectral"]["preview"] == "false_color"  # Blue가 없으면 NIR·Red·Green 위색
    # 산출물을 변수로: (H, W, 밴드) float32 반사율, 자료 없는 곳은 NaN
    img, tf, crs = res
    assert img.dtype == np.float32 and img.shape[2] == 3 and np.isnan(img).any()
    assert qo.band_names(ws) == ["Green", "Red", "NIR"] and qo.band_names(info.indices) == ["NDVI", "GNDVI"]
    # 반사율 참값과 비교 (DLS가 비행 중 입사광 변화를 따라가고, 패널로 수준을 맞춤)
    e0, n0 = Transformer.from_crs(4326, syn.EPSG, always_xy=True).transform(syn.LON0, syn.LAT0)
    H, W, _ = img.shape
    rows, cols = np.meshgrid(np.arange(0, H, 4), np.arange(0, W, 4), indexing="ij")
    x, y = tf * (cols + 0.5, rows + 0.5)
    for k, b in enumerate(info.bands):
        v = img[rows, cols, k]
        t = ms_scene.truth(b, x - e0, y - n0)
        m = np.isfinite(v)
        assert abs(np.median(v[m] / t[m]) - 1) < 0.02, b
        assert np.median(np.abs(v[m] - t[m])) < 0.01, b
    ndvi, tf2, _ = info.read_indices("NDVI")
    assert ndvi.shape == (H, W) and tf2 == tf
    red, nir = img[..., 1], img[..., 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        np.testing.assert_allclose(ndvi, (nir - red) / (nir + red), atol=1e-5, equal_nan=True)


def test_ms_project_reopen_and_photo_coords(ms_processed, ms_scene):
    ws = ms_processed[0]
    p = qo.Project.open(ws)
    assert p.images == ms_scene.folder.resolve()  # 원본 밴드 폴더 (msref 아님)
    # 기준 밴드 TIFF 이름으로도 사진 좌표를 쓸 수 있음 (정렬은 8비트 변환본 이름)
    a = p.image_to_ground("IMG_0000_1.tif", [[480.0, 360.0]], z=0.0)
    b = p.image_to_ground("IMG_0000_1.tif.jpg", [[480.0, 360.0]], z=0.0)
    np.testing.assert_allclose(a, b)
    # 내부표정은 메타데이터 값으로 고정
    cam = p.cameras()["IMG_0000_1.tif.jpg"]
    assert cam.K[0, 0] == pytest.approx(syn.F_PX) and cam.K[0, 2] == pytest.approx(syn.W / 2 - 0.5)


def test_ms_refine_keeps_intrinsics_and_rerenders(ms_workspace):
    p = qo.Project.open(ms_workspace)
    p.set_edits(max_reproj_error_px=1.0)
    events: list[qo.Event] = []
    rr = p.refine(on_event=events.append)
    assert [e.stage for e in events if e.type == "stage"] == ["refine", "refine", "dsm", "bands", "ortho", "finalize"]
    assert rr.ortho.multispectral is not None and rr.ortho.report["refine"]["timings_s"]["bands_s"] > 0
    assert p.cameras()["IMG_0000_1.tif.jpg"].K[0, 0] == pytest.approx(syn.F_PX)


def test_ms_radiance_mode_and_options(ms_workspace):
    p = qo.Project.open(ms_workspace)
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, radiometry="radiance"))
    info = r.multispectral
    assert info.values == "radiance" and info.capture_inconsistency_pct is None  # 방사휘도는 블록 조정 안 함
    med = np.nanmedian(r.read()[0], axis=(0, 1))
    assert 0.02 < med[0] < 0.2  # 반사율×E/π 수준 (W/m²/sr/nm)
    meta = json.loads((ms_workspace / "project" / "meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["radiometry"] == "radiance" and meta["render"]["ms_normalize"] is True
    r2 = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, ms_normalize=False))
    assert r2.multispectral.capture_inconsistency_pct is None
    assert "capture_normalization" not in r2.report["multispectral"]["radiometry"]
    with pytest.raises(qo.InputError) as e1:
        p.orthomosaic(qo.OrthoOptions(radiometry="bogus"))
    assert e1.value.code == "invalid_argument"
    with pytest.raises(qo.InputError) as e2:
        r.read(order="BGR")  # 다중분광에는 채널 순서가 없음
    assert e2.value.code == "invalid_argument"


def test_ms_cancel_during_bands_keeps_previous_result(ms_workspace):
    p = qo.Project.open(ms_workspace)
    before = _sha(ms_workspace / "orthomosaic.tif"), _sha(ms_workspace / "indices.tif")
    tok = qo.CancelToken()

    def on_event(ev: qo.Event) -> None:
        if ev.type == "progress" and ev.stage == "bands":
            tok.cancel()

    with pytest.raises(qo.Cancelled):
        p.orthomosaic(qo.OrthoOptions(gsd_scale=3), on_event=on_event, cancel=tok)
    assert (_sha(ms_workspace / "orthomosaic.tif"), _sha(ms_workspace / "indices.tif")) == before
    assert not list(ms_workspace.glob("*.tmp.tif"))


def test_ms_workspace_can_be_moved(ms_workspace, tmp_path):
    moved = tmp_path / "moved" / "ws"
    shutil.move(str(ms_workspace), str(moved))
    r = qo.Project.open(moved).orthomosaic(qo.OrthoOptions(gsd_scale=4))
    assert r.multispectral is not None and r.orthomosaic.parent == moved


def test_ms_realign_cancel_keeps_previous_workspace(ms_workspace):
    """새 정렬을 중단해도 이전 다중분광 정렬 결과(기준 밴드 영상 포함)로 계속 쓸 수 있다."""
    p = qo.Project.open(ms_workspace)
    tok = qo.CancelToken()

    def on_event(ev: qo.Event) -> None:
        if ev.type == "stage" and ev.stage == "features":
            tok.cancel()

    with pytest.raises(qo.Cancelled):
        p.align(qo.OrthoOptions(sfm=FAST_SFM), on_event=on_event, cancel=tok)
    assert (ms_workspace / "project" / "msref" / "IMG_0000_1.tif.jpg").exists()
    assert not (ms_workspace / "project" / "msref.new").exists()
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4))
    assert r.multispectral is not None


def test_ms_radiometry_checked_before_sfm(ms_scene, tmp_path):
    """명시한 반사율 방식에 필요한 자료가 없으면 SfM 전에 알린다."""
    src = tmp_path / "nopanel"
    src.mkdir()
    for f in ms_scene.folder.glob("IMG_*.tif"):
        if "P_" not in f.name:
            shutil.copy(f, src / f.name)
    events: list[qo.Event] = []
    with pytest.raises(qo.InputError) as ei:
        qo.Project.create(src, tmp_path / "ws").align(qo.OrthoOptions(radiometry="panel"), on_event=events.append)
    assert ei.value.code == "invalid_argument" and "features" not in [e.stage for e in events]


def test_ms_source_folder_missing(ms_workspace, tmp_path):
    meta_p = ms_workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["multispectral"]["source_dir"] = str(tmp_path / "없는폴더")
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(qo.InputError) as ei:
        qo.Project.open(ms_workspace).orthomosaic()
    assert ei.value.code == "image_dir_missing"


def test_ms_preview_and_cli(ms_scene, ms_workspace, tmp_path, capsys):
    pv = qo.preview(ms_scene.folder, tmp_path / "pv")
    assert pv.quicklook_path is not None and pv.quicklook_path.exists()
    assert main(["render", str(ms_workspace), "--gsd-scale", "4", "--radiometry", "dls", "--no-ms-normalize",
                 "--format", "json"]) == 0
    ev = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    m = ev[-1]["data"]["multispectral"]
    assert m["radiometry"]["mode"] == "dls" and "capture_normalization" not in m["radiometry"]
