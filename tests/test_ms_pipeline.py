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
    # 기본 선택 NDVI·NDRE·GNDVI 중 Red edge가 없어 NDRE는 건너뜀
    assert info.index_names == ["NDVI", "GNDVI"] and info.indices_skipped == ["NDRE"]
    assert res.indices == info.indices and res.indices_skipped == ["NDRE"]
    assert all(p.exists() and p.parent == ws / "indices" for p in info.indices.values())
    assert sorted(f.name for f in (ws / "indices").iterdir()) == ["GNDVI.png", "GNDVI.tif", "NDVI.png", "NDVI.tif"]
    assert info.index_preview == ws / "indices" / "NDVI.png" and info.index_previews["GNDVI"].exists()
    assert rep["multispectral"]["indices"] == ["NDVI", "GNDVI"] and rep["multispectral"]["indices_skipped"] == ["NDRE"]
    assert set(rep["multispectral"]["index_legend"]) == {"NDVI", "GNDVI"}
    assert info.index_legend["NDVI"]["min"] == -0.2 and len(info.index_legend["NDVI"]["colors"]) == 3
    assert set(rep["outputs"]["indices"]) == {"NDVI", "GNDVI"} and set(rep["outputs"]["index_previews"]) == {"NDVI", "GNDVI"}
    assert not (ws / "indices.tif").exists() and not (ws / "ndvi.png").exists()
    assert max(info.band_alignment_px.values()) < 0.2
    assert all(after < 2.0 for _, after in info.capture_inconsistency_pct.values())
    assert rep["ortho"]["color_balance"] is False and "color" not in stages
    assert rep["multispectral"]["preview"] == "false_color"  # Blue가 없으면 NIR·Red·Green 위색
    # 산출물을 변수로: (H, W, 밴드) float32 반사율, 자료 없는 곳은 NaN
    img, tf, crs = res
    assert img.dtype == np.float32 and img.shape[2] == 3 and np.isnan(img).any()
    assert qo.band_names(ws) == ["Green", "Red", "NIR"] and qo.band_names(info.indices["GNDVI"]) == ["GNDVI"]
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
    ndvi, tf2, _ = info.read_indices("ndvi")
    assert ndvi.shape == (H, W) and tf2 == tf
    green, red, nir = img[..., 0], img[..., 1], img[..., 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        np.testing.assert_allclose(ndvi, (nir - red) / (nir + red), atol=1e-5, equal_nan=True)
        stack, _, _ = qo.read_indices(ws)  # 있는 지수를 등록 순서로 쌓음
        np.testing.assert_allclose(stack[..., 1], (nir - green) / (nir + green), atol=1e-5, equal_nan=True)
    assert stack.shape == (H, W, 2)
    # 공개 API로 반사율 정사 모자이크에서 바로 계산해도 같음
    v, _, _ = qo.indices.compute_raster(ws, "NDVI")
    np.testing.assert_array_equal(v, ndvi)


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
    before = _sha(ms_workspace / "orthomosaic.tif"), _sha(ms_workspace / "indices" / "NDVI.tif")
    tok = qo.CancelToken()

    def on_event(ev: qo.Event) -> None:
        if ev.type == "progress" and ev.stage == "bands":
            tok.cancel()

    with pytest.raises(qo.Cancelled):
        p.orthomosaic(qo.OrthoOptions(gsd_scale=3), on_event=on_event, cancel=tok)
    assert (_sha(ms_workspace / "orthomosaic.tif"), _sha(ms_workspace / "indices" / "NDVI.tif")) == before
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


def test_ms_index_selection_saved_and_reused(ms_workspace):
    """고른 지수만 만들고, 선택은 meta에 저장되어 refine에서 그대로 쓰임."""
    p = qo.Project.open(ms_workspace)
    with pytest.raises(qo.InputError) as ei:  # 잘못된 이름은 처리 시작 전에 실패
        p.orthomosaic(qo.OrthoOptions(indices=["NDVI", "XYZ"]), on_event=lambda e: pytest.fail("처리 시작됨"))
    assert ei.value.code == "invalid_argument"
    with pytest.raises(qo.InputError):
        p.orthomosaic(qo.OrthoOptions(indices="NDVI"))  # 문자열 하나는 받지 않음
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, indices=["cig", "NDWI", "ndwi", "EVI"]))
    assert r.multispectral.index_names == ["NDWI", "CIg"] and r.indices_skipped == ["EVI"]
    assert sorted(f.name for f in (ms_workspace / "indices").iterdir()) == ["CIg.png", "CIg.tif", "NDWI.png", "NDWI.tif"]
    meta = json.loads((ms_workspace / "project" / "meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["indices"] == ["NDWI", "EVI", "CIg"]  # 정리된 선택 (계산 못 한 것도 선택으로 남김)
    assert qo.OrthoOptions.from_render_dict(meta["render"]).indices == ["NDWI", "EVI", "CIg"]
    p.set_edits(max_reproj_error_px=1.0)
    rr = p.refine()
    assert rr.ortho.multispectral.index_names == ["NDWI", "CIg"]
    # 지수 없음
    r0 = p.orthomosaic(qo.OrthoOptions(gsd_scale=4, indices=[]))
    assert r0.indices == {} and r0.indices_skipped == [] and not (ms_workspace / "indices").exists()
    assert r0.multispectral.index_preview is None
    with pytest.raises(qo.ProjectError):
        qo.read_indices(ms_workspace)


def test_ms_make_indices_only(ms_workspace):
    """지수만 다시 만들기: 정사 모자이크는 그대로, 지수·보고서·저장된 선택만 바뀜."""
    p = qo.Project.open(ms_workspace)
    ortho_sha = _sha(ms_workspace / "orthomosaic.tif")
    rep0 = json.loads((ms_workspace / "report.json").read_text(encoding="utf-8"))
    ndvi0, _, _ = qo.read_indices(ms_workspace, "NDVI")
    # v0.4.0 형식 파일이 남아 있던 워크스페이스라고 가정
    (ms_workspace / "indices.tif").write_bytes(b"old")
    (ms_workspace / "ndvi.png").write_bytes(b"old")
    events: list[qo.Event] = []
    r = p.make_indices(["OSAVI", "ndvi"], on_event=events.append)
    assert [e.stage for e in events if e.type == "stage"] == ["finalize"]
    assert r.multispectral.index_names == ["NDVI", "OSAVI"] and r.indices_skipped == []
    assert _sha(ms_workspace / "orthomosaic.tif") == ortho_sha
    assert sorted(f.name for f in (ms_workspace / "indices").iterdir()) == ["NDVI.png", "NDVI.tif", "OSAVI.png", "OSAVI.tif"]
    assert not (ms_workspace / "indices.tif").exists() and not (ms_workspace / "ndvi.png").exists()
    np.testing.assert_array_equal(qo.read_indices(ms_workspace, "NDVI")[0], ndvi0)  # 같은 값
    rep = json.loads((ms_workspace / "report.json").read_text(encoding="utf-8"))
    assert rep["multispectral"]["indices"] == ["NDVI", "OSAVI"] and set(rep["outputs"]["indices"]) == {"NDVI", "OSAVI"}
    assert rep["sfm"] == rep0["sfm"] and rep["outputs"]["orthomosaic"] == rep0["outputs"]["orthomosaic"]
    assert rep["timings_s"]["indices_s"] >= 0
    meta = json.loads((ms_workspace / "project" / "meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["indices"] == ["NDVI", "OSAVI"]
    assert qo.Project.open(ms_workspace).result().multispectral.index_names == ["NDVI", "OSAVI"]
    # None이면 기본 선택
    assert p.make_indices().multispectral.index_names == ["NDVI", "GNDVI"]
    with pytest.raises(qo.InputError):
        p.make_indices(["BAD"])


def test_ms_make_indices_errors(ms_processed, workspace, tmp_path):
    with pytest.raises(qo.ProjectError) as e1:  # RGB 결과
        qo.Project.open(workspace).make_indices()
    assert e1.value.code == "not_multispectral"
    rgb = qo.Project.open(workspace).orthomosaic(qo.OrthoOptions(gsd_scale=4, indices=["NDVI"]))  # RGB에서는 무시
    assert rgb.indices == {} and rgb.multispectral is None and not (workspace / "indices").exists()
    msws = tmp_path / "msws"  # workspace fixture가 tmp_path/ws를 쓰므로 다른 이름으로 복사
    shutil.copytree(ms_processed[0], msws)
    (msws / "report.json").unlink()
    with pytest.raises(qo.ProjectError) as e2:
        qo.Project.open(msws).make_indices()
    assert e2.value.code == "not_rendered"


def test_ms_legacy_render_settings_use_default(ms_workspace):
    """0.4.1 이전에 저장된 render 설정(indices 없음)은 기본 선택으로 다시 만듦."""
    meta_p = ms_workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["render"].pop("indices", None)
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    assert qo.OrthoOptions.from_render_dict(meta["render"]).indices is None
    p = qo.Project.open(ms_workspace)
    p.set_edits(max_reproj_error_px=1.0)
    assert p.refine().ortho.multispectral.index_names == ["NDVI", "GNDVI"]


def test_ms_preview_index_catalog(ms_scene, scene, tmp_path):
    pv = qo.preview(ms_scene.folder, tmp_path / "pv")
    cat = {c["name"]: c for c in pv.multispectral["index_catalog"]}
    assert list(cat) == qo.indices.names()
    assert cat["NDVI"]["available"] and cat["NDWI"]["available"] and not cat["NDRE"]["available"]
    assert cat["NDRE"]["default"] and cat["NDVI"]["label"] and cat["EVI"]["bands"] == ["nir", "red", "blue"]
    assert [b["name"] for b in pv.multispectral["bands"]] == ["Green", "Red", "NIR"]
    assert qo.preview(scene.folder, tmp_path / "pv2").multispectral is None


def test_ms_cli_indices(ms_workspace, capsys):
    assert main(["indices", str(ms_workspace), "--indices", "NDWI,ndvi", "--format", "json"]) == 0
    ev = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    assert ev[-1]["type"] == "result" and ev[-1]["data"]["multispectral"]["indices"] == ["NDVI", "NDWI"]
    assert main(["indices", str(ms_workspace), "--indices", "none", "--format", "json"]) == 0
    capsys.readouterr()
    assert not (ms_workspace / "indices").exists()
    assert main(["render", str(ms_workspace), "--gsd-scale", "4", "--indices", "gndvi", "--format", "json"]) == 0
    capsys.readouterr()
    assert sorted(f.name for f in (ms_workspace / "indices").iterdir()) == ["GNDVI.png", "GNDVI.tif"]
    assert main(["indices", str(ms_workspace), "--indices", "BAD", "--format", "json"]) != 0
    err = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    assert err[-1]["type"] == "error" and err[-1]["code"] == "invalid_argument"


def test_ms_make_indices_cancel_keeps_consistent(ms_workspace):
    """지수만 다시 만들다 중단하면 지수 파일·보고서·저장된 선택 모두 이전 그대로."""
    p = qo.Project.open(ms_workspace)
    before = sorted(f.name for f in (ms_workspace / "indices").iterdir())
    rep0 = (ms_workspace / "report.json").read_text(encoding="utf-8")
    meta0 = json.loads((ms_workspace / "project" / "meta.json").read_text(encoding="utf-8"))["render"]
    tok = qo.CancelToken()

    def cancel_at_stage(ev: qo.Event) -> None:
        if ev.type == "stage":
            tok.cancel()  # 다음 이벤트(교체 직전 progress)에서 Cancelled

    def fail_at_progress(ev: qo.Event) -> None:
        if ev.type == "progress":
            raise RuntimeError("콜백 오류")  # 교체 직전 마지막 이벤트에서 난 예외

    for on_event, exc, cancel in ((cancel_at_stage, qo.Cancelled, tok), (fail_at_progress, RuntimeError, None)):
        with pytest.raises(exc):
            p.make_indices(["NDWI"], on_event=on_event, cancel=cancel)
        assert sorted(f.name for f in (ms_workspace / "indices").iterdir()) == before
        assert (ms_workspace / "report.json").read_text(encoding="utf-8") == rep0
        assert json.loads((ms_workspace / "project" / "meta.json").read_text(encoding="utf-8"))["render"] == meta0
        assert not (ms_workspace / "indices.new").exists()


def test_ms_refine_bad_saved_selection_falls_back(ms_workspace):
    """meta에 저장된 지수 선택이 잘못되었으면 긴 처리 끝에 실패하지 않고 기본값으로 만들며 경고함."""
    meta_p = ms_workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["render"]["indices"] = "NDVI,NDWI"  # 목록이 아닌 문자열 (손으로 고친 경우 등)
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    p = qo.Project.open(ms_workspace)
    p.set_edits(max_reproj_error_px=1.0)
    rr = p.refine()
    assert rr.ortho.multispectral.index_names == ["NDVI", "GNDVI"]
    assert any("지수 선택" in w for w in rr.ortho.warnings)


def test_ms_cli_indices_text(ms_workspace, capsys):
    assert main(["indices", str(ms_workspace), "--indices", "ndvi,evi", "--format", "text"]) == 0
    txt = capsys.readouterr().out
    assert "NDVI" in txt and "건너뛴 지수" in txt and "EVI" in txt and "{" not in txt


def test_ms_render_index_failure_keeps_previous(ms_workspace, monkeypatch):
    """정사 모자이크 재생성 중 지수 계산이 실패하면 정사 모자이크·지수·보고서 모두 이전 그대로 (지수는 교체 전에 만듦)."""
    import quickortho._core.pipeline as pl

    files = [ms_workspace / "orthomosaic.tif", ms_workspace / "dsm.tif", ms_workspace / "report.json",
             *sorted((ms_workspace / "indices").iterdir())]
    before = [_sha(f) for f in files]

    def broken(*a, **k):
        raise OSError("디스크 가득 참")

    monkeypatch.setattr(pl, "build_indices", broken)
    with pytest.raises(OSError):
        qo.Project.open(ms_workspace).orthomosaic(qo.OrthoOptions(gsd_scale=4, indices=["NDWI"]))
    assert [_sha(f) for f in files] == before
    assert not list(ms_workspace.glob("*.tmp.tif")) and not (ms_workspace / "indices.new").exists()


def test_ms_water_not_applied(ms_processed, ms_workspace):
    """다중분광에는 수면 처리를 하지 않음 (판별 단계 없음, on이면 경고)."""
    assert "water" not in [e.stage for e in ms_processed[1] if e.type == "stage"]
    assert "water" not in ms_processed[2].report and ms_processed[2].water is None
    r = qo.Project.open(ms_workspace).orthomosaic(qo.OrthoOptions(gsd_scale=4, water="on"))
    assert any("수면 처리" in w for w in r.warnings) and r.water is None
