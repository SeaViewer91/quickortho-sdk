"""공개 API 시험: 합성 영상으로 스캔 → 미리보기 → 정렬 → 정사 모자이크 → GCP 보정 전체를 돌린다."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio

import quickortho as qo
from conftest import FAST_SFM, GPS_BIAS
from quickortho.cli import main, serve


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ───────────────────────── 기본 ─────────────────────────


def test_public_names_importable():
    for name in qo.__all__:
        assert hasattr(qo, name), name
    assert __import__("re").match(r"^\d+\.\d+\.\d+(\.dev\d+|a\d+|b\d+|rc\d+)?$", qo.__version__)


def test_scan_and_preview(scene, tmp_path):
    sr = qo.scan(scene.folder)
    assert len(sr.selected_images) == 12 and sr.num_with_gps == 12
    assert sr.summary["rolling_shutter_warning"] is False

    pr = qo.preview(scene.folder, tmp_path / "pv", qo.PreviewOptions(max_size=512))
    assert pr.num_images == 12
    assert pr.quicklook is not None and pr.quicklook.exists()
    assert pr.coverage.exists() and pr.geojson.exists()
    assert len(pr.corners_lonlat) == 4


# ───────────────────────── 정렬·정사 모자이크 ─────────────────────────


def test_process_end_to_end(processed, scene):
    ws, events, res = processed
    rep = res.report
    assert rep["sfm"]["num_registered"] == 12
    assert rep["georef"]["gps_residual_rms_m"] < 0.3  # GPS 오차가 일정하므로 잔차는 작음
    assert res.epsg == 32652 and not res.refined
    assert res.orthomosaic.exists() and res.dsm.exists() and res.preview.exists()
    with rasterio.open(res.orthomosaic) as ds:
        assert ds.crs.to_epsg() == 32652 and ds.count == 4
        assert abs(ds.res[0] - res.gsd_m) < 1e-9
    assert rep["report_version"] == 1 and rep["engine_version"] == qo.__version__
    assert rep["timings_s"]["total_s"] > 0 and res.peak_memory_mb > 0
    # 단계는 정해진 순서로 한 번씩 시작함
    stages = [e.stage for e in events if e.type == "stage"]
    assert stages == ["scan", "features", "matching", "mapping", "georef", "dsm", "ortho", "finalize"]
    assert all(s in qo.STAGES for s in stages)
    fr = [e.fraction for e in events if e.type == "progress"]
    assert fr and all(0.0 <= f <= 1.0 for f in fr)
    # 임시 파일이 남지 않음
    assert not list(ws.glob("*.tmp.tif")) and not (ws / "work").exists()


def test_open_and_result(processed):
    ws, _, res = processed
    p = qo.Project.open(ws)
    assert p.is_aligned and p.is_rendered and not p.is_refined
    assert p.images.name == "images"
    r = p.result()
    assert r is not None and r.orthomosaic == res.orthomosaic and r.width == res.width


def test_rerender_with_other_gsd_skips_sfm(workspace, processed):
    _, _, first = processed
    p = qo.Project.open(workspace)
    events: list[qo.Event] = []
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4.0), on_event=events.append)
    assert [e.stage for e in events if e.type == "stage"] == ["dsm", "ortho", "finalize"]
    assert r.gsd_m == pytest.approx(first.gsd_m * 4, rel=1e-6)  # 기본값 gsd_scale=1 대비 4배
    assert abs(r.width - first.width / 4) <= 2
    assert r.report["sfm"]["num_registered"] == 12  # 정렬 보고서는 유지됨
    assert json.loads((workspace / "project" / "meta.json").read_text(encoding="utf-8"))["render"]["gsd_scale"] == 4.0


def test_align_only_then_render(scene, tmp_path):
    p = qo.Project.create(scene.folder, tmp_path / "ws")
    a = p.align(qo.OrthoOptions(sfm=FAST_SFM))
    assert a.num_registered == 12 and a.epsg == 32652
    assert p.is_aligned and not p.is_rendered and p.result() is None
    with pytest.raises(qo.ProjectError) as ei:
        p.predict(world={"x": 500000.0, "y": 3884000.0, "z": 0.0, "epsg": 32652})
    assert ei.value.code == "not_rendered"
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    assert p.is_rendered and r.width > 0


# ───────────────────────── 중단 ─────────────────────────


def test_cancel_before_start(scene, tmp_path):
    tok = qo.CancelToken()
    tok.cancel()
    with pytest.raises(qo.Cancelled) as ei:
        qo.Project.create(scene.folder, tmp_path / "ws").process(cancel=tok)
    assert ei.value.code == "cancelled"
    assert not (tmp_path / "ws" / "project").exists()


def test_cancel_during_render_keeps_previous_result(workspace):
    p = qo.Project.open(workspace)
    before = _sha(workspace / "orthomosaic.tif"), _sha(workspace / "dsm.tif")
    tok = qo.CancelToken()

    def on_event(ev: qo.Event) -> None:
        if ev.type == "progress" and ev.stage == "ortho":
            tok.cancel()  # 다음 확인 지점에서 Cancelled

    with pytest.raises(qo.Cancelled):
        p.orthomosaic(qo.OrthoOptions(gsd_scale=3.0), on_event=on_event, cancel=tok)
    assert (_sha(workspace / "orthomosaic.tif"), _sha(workspace / "dsm.tif")) == before
    assert not list(workspace.glob("*.tmp.tif"))
    assert p.is_rendered


def test_cancel_at_finalize_keeps_previous_result(workspace):
    p = qo.Project.open(workspace)
    before = _sha(workspace / "orthomosaic.tif")
    tok = qo.CancelToken()

    def on_event(ev: qo.Event) -> None:
        if ev.type == "stage" and ev.stage == "finalize":
            tok.cancel()  # COG 변환이 끝난 뒤, 결과 교체 전에 Cancelled

    with pytest.raises(qo.Cancelled):
        p.orthomosaic(qo.OrthoOptions(gsd_scale=3.0), on_event=on_event, cancel=tok)
    assert _sha(workspace / "orthomosaic.tif") == before
    assert not list(workspace.glob("*.tmp.tif"))


def test_image_dir_missing(workspace, tmp_path):
    meta_p = workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["image_dir"] = str(tmp_path / "옮겨진폴더")
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    p = qo.Project.open(workspace)
    for fn in (p.orthomosaic, p.refine, p.reset_refinement):
        with pytest.raises(qo.InputError) as ei:
            fn()
        assert ei.value.code == "image_dir_missing"


# ───────────────────────── 정밀 보정 ─────────────────────────


def _synthetic_gcps(scene, names_xy, epsg=32652, check=()):
    rng = np.random.default_rng(5)
    gcps = []
    for name, (e, n) in names_xy.items():
        z = float(scene.height(e, n))
        x, y = scene.utm(e, n)
        g = qo.GCP(name, x, y, z, epsg, role="check" if name in check else "control")
        for img in scene.cameras:
            uv = scene.project(img, e, n, z)
            if uv is not None:
                g.marks.append(qo.Mark(img, uv[0] + rng.normal(0, 0.2), uv[1] + rng.normal(0, 0.2)))
        assert len(g.marks) >= 2
        gcps.append(g)
    return gcps


GCP_XY = {
    "G1": (5.0, 10.0), "G2": (75.0, 5.0), "G3": (10.0, 90.0), "G4": (80.0, 95.0),
    "C1": (40.0, 50.0), "C2": (60.0, 30.0),
}


def test_gcp_refine_removes_gps_bias(workspace, scene):
    p = qo.Project.open(workspace)
    gcps = _synthetic_gcps(scene, GCP_XY, check=("C1", "C2"))
    saved = p.set_edits(gcps=gcps, max_reproj_error_px=2.0)
    assert len(saved["gcps"]) == 6 and saved["max_reproj_error_px"] == 2.0
    assert [g.name for g in p.gcps] == list(GCP_XY)

    # 보정 전: 검사점 위치를 예측해 보면 GPS 오차만큼 어긋나 있음
    c1 = p.gcps[4]
    pred = p.predict(c1.marks[:3])
    assert pred["method"] == "triangulated"
    off = np.array([pred["point"]["x"] - c1.x, pred["point"]["y"] - c1.y])
    assert np.linalg.norm(off) == pytest.approx(np.hypot(GPS_BIAS[0], GPS_BIAS[1]), abs=0.5)

    rr = p.refine()
    assert rr.mode == "gcp" and p.is_refined and rr.ortho.refined
    assert rr.check is not None and rr.check["count"] == 2
    assert rr.check["rmse_xy"] < 0.15 and rr.check["rmse_z"] < 0.3
    assert rr.ortho.report["georef"]["mode"] == "gcp"

    # 보정 후 다시 만든 정사 모자이크에도 보정 결과가 유지됨
    r2 = p.orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    assert r2.refined and r2.report["refine"]["gcp_summary"]["check"]["count"] == 2

    # 보정 취소: 보정 결과는 지우고 수정 사항은 남김
    r3 = p.reset_refinement()
    assert not r3.refined and not p.is_refined and "refine" not in p.report()
    assert len(p.gcps) == 6


def test_refine_without_gcp_uses_gps(workspace):
    p = qo.Project.open(workspace)
    p.set_edits(max_reproj_error_px=1.0)
    rr = p.refine()
    assert rr.mode == "gps" and rr.control is None and rr.rmse_after_px <= rr.rmse_before_px


def test_set_edits_partial_and_invalid(workspace):
    p = qo.Project.open(workspace)
    p.set_edits(max_reproj_error_px=1.5)
    p.set_edits(tiepoints=[qo.TiePoint("T1", [qo.Mark("DJI_0000.JPG", 10, 20), qo.Mark("DJI_0001.JPG", 30, 40)])])
    e = p.get_edits()
    assert e["max_reproj_error_px"] == 1.5 and e["tiepoints"][0]["id"] == "T1"
    assert p.tiepoints[0].marks[1].x == 30
    with pytest.raises(qo.InputError) as ei:
        p.set_edits(gcps=[{"name": "bad"}])
    assert ei.value.code == "invalid_edits"


def test_tiepoint_stats_and_info(workspace):
    p = qo.Project.open(workspace)
    st = p.tiepoint_stats(top=10)
    assert st["summary"]["num_points"] > 100 and len(st["worst"]) == 10
    info = p.info()
    assert info["exists"] and len(info["images"]) == 12 and info["frame"]["epsg"] == 32652


def test_load_gcps_cp949(tmp_path):
    f = tmp_path / "gcp.csv"
    f.write_bytes("점명,X(북),Y(동),표고\nG1,200100.5,500200.25,12.3\nG2,200150.0,500260.0,15.0\n".encode("cp949"))
    gcps = qo.load_gcps(f, epsg=5186, columns={"name": 0, "x": 2, "y": 1, "z": 3}, check=["G2"])
    assert [g.name for g in gcps] == ["G1", "G2"]
    assert gcps[0].x == 500200.25 and gcps[0].y == 200100.5 and gcps[0].z == 12.3
    assert gcps[0].role == "control" and gcps[1].role == "check" and gcps[1].epsg == 5186


# ───────────────────────── 예외 ─────────────────────────


def test_error_codes(tmp_path, scene):
    with pytest.raises(qo.InputError) as e1:
        qo.Project.create(tmp_path / "없는폴더", tmp_path / "ws")
    assert e1.value.code == "folder_not_found"
    with pytest.raises(qo.ProjectError) as e2:
        qo.Project.open(tmp_path)
    assert e2.value.code == "not_aligned"
    few = tmp_path / "few"
    few.mkdir()
    for name in list(scene.cameras)[:2]:
        (few / name).write_bytes((scene.folder / name).read_bytes())
    with pytest.raises(qo.InputError) as e3:
        qo.Project.create(few, tmp_path / "ws2").align()
    assert e3.value.code == "too_few_images" and isinstance(e3.value, qo.QuickOrthoError)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(qo.InputError) as e4:
        qo.process(empty, tmp_path / "ws3")
    assert e4.value.code == "no_images"


# ───────────────────────── 이전 버전 워크스페이스 ─────────────────────────


def test_open_v020_workspace(workspace):
    """데스크톱 앱 v0.2.0 엔진이 만든 워크스페이스(align_report.json 대신 base_report.json)를 열 수 있어야 함."""
    proj_dir = workspace / "project"
    full = json.loads((workspace / "report.json").read_text(encoding="utf-8"))
    full.pop("report_version")
    (proj_dir / "align_report.json").unlink()
    (proj_dir / "base_report.json").write_text(json.dumps(full), encoding="utf-8")
    p = qo.Project.open(workspace)
    r = p.orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    assert r.report["sfm"]["num_registered"] == 12 and r.report["report_version"] == 1


def test_render_v020_workspace_failed_after_align(workspace):
    """데스크톱 앱 v0.2.0이 정렬 직후 정사 모자이크 단계에서 실패한 워크스페이스 (보고서가 하나도 없음).

    정렬을 다시 하지 않고 정사 모자이크를 만들 수 있어야 하며, 보고서의 정합 정보는 재구성에서 다시 만든다.
    """
    proj_dir = workspace / "project"
    for f in ("align_report.json", "base_report.json"):
        (proj_dir / f).unlink(missing_ok=True)
    for f in ("report.json", "orthomosaic.tif", "dsm.tif", "preview.png"):
        (workspace / f).unlink(missing_ok=True)
    r = qo.Project.open(workspace).orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    rep = r.report
    assert rep["sfm"]["num_registered"] == 12 and rep["input"]["with_gps"] == 12
    assert rep["georef"]["gps_residual_rms_m"] is not None
    assert any("이전 실행" in w for w in rep["warnings"])
    assert (proj_dir / "align_report.json").exists()


def test_saved_render_options_migrates_old_defaults():
    """0.3.0 미만이 저장한 정사 옵션 중 당시 기본값(GSD 배율 2, 캐시 600 MB)만 새 기본값으로 바꾼다."""
    from quickortho._core.pipeline import saved_render_options

    o, note = saved_render_options({"engine_version": "0.2.1", "render": {
        "gsd_m": None, "gsd_scale": 2.0, "cache_budget_mb": 600, "bounds": [0, 0, 10, 10]}})
    assert o.gsd_scale == 1.0 and o.cache_budget_mb is None and o.bounds == (0, 0, 10, 10)
    assert note and "0.2.1" in note
    # 사용자가 고른 값은 그대로
    o, note = saved_render_options({"engine_version": "0.2.1", "render": {"gsd_scale": 4.0, "cache_budget_mb": 300}})
    assert (o.gsd_scale, o.cache_budget_mb, note) == (4.0, 300, None)
    o, _ = saved_render_options({"engine_version": "0.2.1", "render": {"gsd_m": 0.05, "gsd_scale": 2.0}})
    assert o.gsd_m == 0.05
    # 0.3.0 이상이 저장한 배율 2는 사용자가 고른 값 (정렬은 이전 버전으로 했어도)
    o, note = saved_render_options({"engine_version": "0.2.1", "render_engine_version": "0.3.0",
                                    "render": {"gsd_scale": 2.0}})
    assert o.gsd_scale == 2.0 and note is None
    # 데스크톱 앱 v0.2.x는 gsd_m·gsd_scale만 저장함 (캐시 키 없음)
    o, note = saved_render_options({"engine_version": "0.2.1", "render": {"gsd_m": None, "gsd_scale": 2.0}})
    assert o.gsd_scale == 1.0 and o.cache_budget_mb is None and "gsd_scale=2" in note
    # 캐시만 바뀌면 해상도 안내를 하지 않음
    _, note = saved_render_options({"engine_version": "0.2.1", "render": {"gsd_scale": 4.0, "cache_budget_mb": 600}})
    assert "캐시" in note and "gsd_scale=2" not in note
    # 사전 릴리스·개발 버전 문자열
    o, note = saved_render_options({"engine_version": "0.2.1", "render_engine_version": "0.3.0rc1",
                                    "render": {"gsd_scale": 2.0, "cache_budget_mb": 600}})
    assert (o.gsd_scale, o.cache_budget_mb, note) == (2.0, 600, None)
    o, _ = saved_render_options({"engine_version": "0.2.0.dev0", "render": {"gsd_scale": 2.0}})
    assert o.gsd_scale == 1.0
    # 정사 옵션이 저장되지 않은 워크스페이스(정렬만 함)는 새 기본값
    o, note = saved_render_options({"engine_version": "0.2.0"})
    assert o.gsd_scale == 1.0 and o.cache_budget_mb is None and note is None


def test_refine_upgrades_old_default_gsd(workspace, processed):
    """0.2.x가 기본값으로 만든 워크스페이스를 보정하면 정사 모자이크를 원본 해상도로 다시 만들고, 바꾼 값을 저장한다."""
    first = processed[2]
    meta_p = workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["engine_version"] = "0.2.1"
    meta.pop("render_engine_version", None)
    meta["render"] = {**meta["render"], "gsd_scale": 2.0, "cache_budget_mb": 600}
    meta_p.write_text(json.dumps(meta), encoding="utf-8")

    p = qo.Project.open(workspace)
    p.set_edits(max_reproj_error_px=1.0)
    rr = p.refine()
    assert rr.ortho.gsd_m == pytest.approx(first.gsd_m, rel=0.05)  # 보정으로 자세가 조금 바뀜
    assert any("GSD 배율 2 → 1" in w for w in rr.ortho.report["warnings"])
    saved = json.loads(meta_p.read_text(encoding="utf-8"))
    assert saved["render"]["gsd_scale"] == 1.0 and saved["render"]["cache_budget_mb"] is None
    assert saved["render_engine_version"] == qo.__version__
    # 안내는 그 실행의 보고서에만 남고, 이후 정사 모자이크 보고서에는 나오지 않음
    r2 = p.orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    assert not any("GSD 배율" in w for w in r2.report["warnings"])


def test_reset_refinement_upgrades_old_default_gsd(workspace, processed):
    """보정 취소로 다시 만들 때도 같은 규칙을 적용하고 그 보고서에 안내를 남긴다."""
    meta_p = workspace / "project" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["engine_version"] = "0.2.1"
    meta.pop("render_engine_version", None)
    meta["render"] = {"gsd_m": None, "gsd_scale": 2.0}  # 데스크톱 앱 v0.2.x 형식
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    r = qo.Project.open(workspace).reset_refinement()
    assert r.gsd_m == pytest.approx(processed[2].gsd_m, rel=1e-6)
    assert any("GSD 배율 2 → 1" in w for w in r.report["warnings"])


def test_sparse_dsm_grid_is_half_point_spacing(processed):
    rep = processed[2].report
    area = rep["ortho"]["width"] * rep["ortho"]["height"] * rep["ortho"]["gsd_m"] ** 2
    # 결과 화소 수는 경계를 GSD 배수로 올림해 정하므로 면적이 조금 다를 수 있음
    assert rep["dsm"]["resolution_m"] == pytest.approx(max(0.5, 0.5 * (area / rep["dsm"]["num_points"]) ** 0.5), rel=0.01)
    assert rep["ortho"]["blend"] == "seamline"
    assert rep["ortho"]["gsd_m"] == pytest.approx(rep["ortho"]["source_gsd_m"])  # 기본값: 원본 해상도


def test_cache_budget_validation(workspace):
    with pytest.raises(qo.InputError) as ei:
        qo.Project.open(workspace).orthomosaic(qo.OrthoOptions(cache_budget_mb=0))
    assert ei.value.code == "invalid_argument"


# ───────────────────────── CLI·serve ─────────────────────────


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.strip().splitlines()]


def test_cli_render(workspace, capsys):
    assert main(["render", str(workspace), "--gsd-scale", "4"]) == 0
    ev = _lines(capsys.readouterr().out)
    assert ev[-1]["type"] == "result" and ev[-1]["command"] == "render"
    assert {e["name"] for e in ev if e["type"] == "stage"} == {"dsm", "ortho", "finalize"}


def test_cli_error_has_code(tmp_path, capsys):
    assert main(["render", str(tmp_path)]) == 1
    ev = _lines(capsys.readouterr().out)
    assert ev[-1]["type"] == "error" and ev[-1]["code"] == "not_aligned"


def test_serve_protocol(tmp_path):
    req = "\n".join([
        json.dumps({"job": 1, "argv": ["version"]}),
        json.dumps({"job": 2, "argv": ["nope"]}),
        json.dumps({"job": 3, "argv": ["scan", str(tmp_path / "없음")]}),
        "이건 JSON이 아님",
    ]) + "\n"
    out = io.StringIO()
    assert serve(io.StringIO(req), out) == 0
    ev = _lines(out.getvalue())
    assert ev[0]["type"] == "ready" and ev[0]["ok"] and ev[0]["protocol"] == 1
    res1 = [e for e in ev if e.get("job") == 1 and e["type"] == "result"][0]
    assert res1["data"]["sdk"] == qo.__version__ and res1["data"]["protocol"] == 1
    err2 = [e for e in ev if e.get("job") == 2 and e["type"] == "error"][0]
    assert err2["code"] == "invalid_argument"
    err3 = [e for e in ev if e.get("job") == 3 and e["type"] == "error"][0]
    assert err3["code"] == "folder_not_found"
    bad = [e for e in ev if e["type"] == "error" and e.get("job") is None][0]
    assert bad["code"] == "invalid_request"
    assert [e["code"] for e in ev if e["type"] == "done"] == [0, 1, 1, 1]
