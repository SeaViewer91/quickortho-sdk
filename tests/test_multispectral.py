"""다중분광: XMP 해석, 방사보정식, rig 자세, 정사투영(반사율·밴드 정합), 캡처 이득, 지수."""

import io
import math
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from PIL import Image, TiffImagePlugin
from pyproj import CRS

from quickortho._core import multispectral as ms
from quickortho._core.ms_ortho import (
    build_band_sources,
    refine_rig,
    render_ms_orthomosaic,
    solve_capture_gains,
)
from quickortho._core.indices import available_indices, index_catalog, normalize_selection, write_indices
from quickortho._core.ortho import View, _footprint, finalize_cog
from quickortho._core.protocol import Emitter
from quickortho._core.surface import Dsm

W, H = 160, 120
E = 1.2  # 입사광 W/m²/nm
A1 = 2e-4


def _band(file, capture, band, index, wl, rig_deg=None, f=120.0, irradiance=E, vig=None) -> ms.BandImage:
    return ms.BandImage(
        file=file, capture=capture, band=band, index=index, wavelength=wl, width=W, height=H,
        make="Test", model="Cam", bits=16, black_level=100.0, exposure=0.001, gain=1.0,
        radcal=[A1, 0.0, 0.0], vig_center=vig[0] if vig else None, vig_poly=vig[1] if vig else None,
        vig2d=None, vig2d_exp=None, fx=f, fy=f, cx=W / 2, cy=H / 2, dist=[0.0] * 5,
        rig_rot_deg=rig_deg or [0.0, 0.0, 0.0], rig_trans_mm=None, irradiance=irradiance,
        albedo=None, panel_area=None,
    )


def test_xmp_attribute_element_and_sequence(tmp_path: Path):
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:Camera="http://pix4d.com/camera/1.0" Camera:BandName="NIR">'
        b"<Camera:RigCameraIndex>3</Camera:RigCameraIndex>"
        b"<Camera:VignettingCenter><rdf:Seq><rdf:li>612.9</rdf:li><rdf:li>474.7</rdf:li></rdf:Seq>"
        b"</Camera:VignettingCenter>"
        b"<Camera:ReflectArea><rdf:Seq><rdf:li>1,2,3,4,5,6,7,8</rdf:li></rdf:Seq></Camera:ReflectArea>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[700] = xmp
    ifd.tagtype[700] = 1
    Image.fromarray(np.zeros((4, 4), np.uint16)).save(tmp_path / "a.tif", tiffinfo=ifd)
    x = ms.read_xmp(tmp_path / "a.tif")
    assert x["Camera:BandName"] == "NIR"
    assert x["Camera:RigCameraIndex"] == "3"
    assert ms._floats(x["Camera:VignettingCenter"]) == [612.9, 474.7]
    assert ms._floats(x["Camera:ReflectArea"]) == [1, 2, 3, 4, 5, 6, 7, 8]
    # 다중분광 메타데이터가 부족한 TIFF는 밴드 영상으로 받지 않음
    with pytest.raises(ValueError):
        ms.read_band(tmp_path / "a.tif", tmp_path)


def _dn_from_radiance(b: ms.BandImage, L: np.ndarray, vig: np.ndarray | None = None) -> np.ndarray:
    a1, a2, a3 = b.radcal
    y = np.arange(b.height, dtype=np.float64)[:, None]
    row = 1.0 / (1.0 + a2 * y / b.exposure - a3 * y)
    v = np.ones_like(L) if vig is None else vig
    return L / (v * row * a1 / (b.gain * b.exposure * 2.0**b.bits)) + b.black_level


def test_radiance_inverts_published_model():
    b = _band("x.tif", "c", "Red", 2, 668, vig=([80.0, 60.0], [1e-4, 2e-6, -1e-8]))
    b.radcal = [A1, 3e-8, -1e-5]
    L = np.linspace(0.05, 0.3, W * H).reshape(H, W)
    vig = ms.vignette(b)
    # 방사형 다항식: 중심에서 1, 바깥으로 갈수록 보정 배율이 달라짐
    assert vig[60, 80] == pytest.approx(1.0, abs=1e-3)
    raw = _dn_from_radiance(b, L, vig)
    np.testing.assert_allclose(ms.radiance(b, raw.astype(np.float32)), L, rtol=1e-4)


def test_vignette_2d_polynomial():
    b = _band("x.tif", "c", "Red", 2, 668)
    b.vig2d, b.vig2d_exp = [1.0, 0.5, 0.25], [0, 0, 1, 0, 0, 1]  # 1 + 0.5·x/W + 0.25·y/H
    v = ms.vignette(b)
    assert v[0, 0] == pytest.approx(1.0)
    assert v[H - 1, W - 1] == pytest.approx(1 / (1 + 0.5 * (W - 1) / W + 0.25 * (H - 1) / H), rel=1e-5)


def test_rig_relative_matches_infinity_homography():
    """밴드 카메라 = 기준 자세 ∘ rig 상대 자세. 먼 점은 제조사 호모그래피 C_ref·R_ref^T·R_b·C_b⁻¹ 대로 옮겨져야 함."""
    ref = _band("g.tif", "c", "Green", 1, 560, rig_deg=[0.1, -0.2, 0.05])
    nir = _band("n.tif", "c", "NIR", 3, 842, rig_deg=[0.4, 0.3, -0.2])
    R, c = ms.band_from_ref(nir, ref)
    assert np.allclose(R @ R.T, np.eye(3))
    K = np.array([[120.0, 0, W / 2], [0, 120.0, H / 2], [0, 0, 1]])
    Hm = K @ ms.rig_rotation(ref.rig_rot_deg).T @ ms.rig_rotation(nir.rig_rot_deg) @ np.linalg.inv(K)
    d_ref = np.array([0.1, -0.05, 1.0])  # 기준 카메라 좌표의 방향 (무한원)
    p_ref = K @ d_ref
    p_band = K @ (R @ d_ref)
    q = Hm @ (p_band / p_band[2])
    assert np.allclose(q[:2] / q[2], p_ref[:2] / p_ref[2], atol=1e-9)


def test_smooth_irradiance_and_dls_factors(tmp_path: Path):
    caps = {}
    for j, e in enumerate([1.0, 1.0, 3.0, 1.0, 1.0]):  # 가운데 값은 순간 튐
        caps[f"c{j}"] = {1: _band(f"c{j}_1.tif", f"c{j}", "Green", 1, 560, irradiance=e)}
        caps[f"c{j}"][1].datetime = f"2026:01:01 00:00:0{j}"
    ds = ms.MsDataset(tmp_path, caps, 1, [1])
    factors, info = ms.reflectance_factors(ds)
    assert info["mode"] == "dls"
    assert factors["c2"][1] == pytest.approx(math.pi / 1.0)  # 이동 중앙값으로 튐 제거


def _nadir(cx: float, cy: float, h: float):
    R = np.diag([1.0, -1.0, -1.0])
    C = np.array([cx, cy, h])
    return R, -R @ C, C


def _scene(band: str, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    chk = ((np.floor(X / 4) + np.floor(Y / 4)) % 2).astype(np.float64)
    smooth = 0.02 * np.sin(X / 7.0) + 0.02 * np.cos(Y / 5.0)
    return (0.1 + 0.3 * chk if band == "Green" else 0.55 - 0.35 * chk) + smooth


def _write_band(root: Path, b: ms.BandImage, R: np.ndarray, t: np.ndarray, rig_true: list[float], ref: ms.BandImage):
    """참 rig로 밴드 영상을 합성한다: 화소 광선을 z=0 지면에 쏘아 반사율 → 방사휘도 → 원시 값."""
    Rr = ms.rig_rotation(rig_true).T @ ms.rig_rotation(ref.rig_rot_deg)
    Rb = Rr @ R
    tb = Rr @ t
    C = -Rb.T @ tb
    v, u = np.mgrid[0:H, 0:W].astype(np.float64)
    d = np.column_stack([(u.ravel() + 0.5 - b.cx) / b.fx, (v.ravel() + 0.5 - b.cy) / b.fy, np.ones(u.size)]) @ Rb
    s = -C[2] / d[:, 2]
    Xg, Yg = C[0] + s * d[:, 0], C[1] + s * d[:, 1]
    L = (_scene(b.band, Xg, Yg) * E / math.pi).reshape(H, W)
    raw = np.clip(_dn_from_radiance(b, L), 0, 65535).astype(np.uint16)
    Image.fromarray(raw).save(root / b.file)


def _make_flight(root: Path, rig_meta: list[float], rig_true: list[float]):
    caps, views = {}, []
    for j, (cx, cy) in enumerate([(-8.0, 0.0), (8.0, 0.0), (0.0, 7.0)]):
        cap = f"c{j}"
        g = _band(f"IMG_{j}_2.tif", cap, "Green", 1, 560)
        n = _band(f"IMG_{j}_4.tif", cap, "NIR", 3, 842, rig_deg=rig_meta)
        R, t, C = _nadir(cx, cy, 50.0)
        _write_band(root, g, R, t, g.rig_rot_deg, g)
        _write_band(root, n, R, t, rig_true, g)
        caps[cap] = {1: g, 3: n}
        cam = ms.colmap_camera(g)
        fp = _footprint(cam, R, C, 0.0)
        views.append(View(ms.proxy_name(g.file), cam, R, t, C, (fp[:, 0].min(), fp[:, 1].min(), fp[:, 0].max(), fp[:, 1].max())))
    ds = ms.MsDataset(root, caps, 1, [1, 3])
    factors = {c: {1: math.pi / E, 3: math.pi / E} for c in caps}
    return ds, views, factors


def _render(tmp_path: Path, ds, views, sources, name="o.tif"):
    dsm = Dsm(z=np.zeros((3, 3), np.float32), x0=-200, y0=100, res=200, num_points=0)
    out = tmp_path / name
    gsd = 50.0 / 120.0
    render_ms_orthomosaic(
        views, sources, ["Green", "NIR"], dsm, ds.root, out, CRS.from_epsg(32652), gsd, gsd, gsd,
        (-20.0, -15.0, 20.0, 20.0), Emitter(io.StringIO()), tile=64,
    )
    with rasterio.open(out) as d:
        data = d.read()
        tr = d.transform
    X, Y = np.meshgrid(tr.c + (np.arange(data.shape[2]) + 0.5) * tr.a, tr.f + (np.arange(data.shape[1]) + 0.5) * tr.e)
    return data, X, Y


def _error(data, X, Y):
    """체커 경계 근처(보간 영향)를 뺀 화소의 참 반사율과의 차이(중앙값)와, NIR의 위치 어긋남(화소, 위상상관)."""
    fx, fy = np.abs((X / 4) - np.round(X / 4)) * 4, np.abs((Y / 4) - np.round(Y / 4)) * 4
    valid = np.isfinite(data).all(axis=0)
    interior = (fx > 0.8) & (fy > 0.8) & valid
    errs = [np.abs(data[k][interior] - _scene(b, X[interior], Y[interior])) for k, b in enumerate(["Green", "NIR"])]
    # 위치 어긋남: got ≈ truth(x − s) ≈ truth − ∇truth·s 를 최소제곱으로 풀어 s(화소)를 구함
    truth = _scene("NIR", X, Y)
    gy, gx = np.gradient(truth)
    m = valid & (np.hypot(gx, gy) > 0.01)
    A = np.column_stack([gx[m], gy[m]])
    sol, *_ = np.linalg.lstsq(A, truth[m] - data[1][m], rcond=None)
    dx, dy = sol
    return max(float(np.median(e)) for e in errs), float(np.hypot(dx, dy))


def test_ms_ortho_recovers_reflectance_and_registers_bands(tmp_path: Path):
    rig = [0.6, -0.4, 0.3]
    ds, views, factors = _make_flight(tmp_path, rig_meta=rig, rig_true=rig)
    sources = build_band_sources(views, ds, factors)
    data, X, Y = _render(tmp_path, ds, views, sources)
    med, shift = _error(data, X, Y)
    assert med < 0.005  # 방사보정·투영이 맞으면 반사율이 그대로 돌아옴
    assert shift < 0.1  # 밴드가 어긋나지 않음 (rig가 틀리면 아래 시험처럼 커짐)
    # NDVI 부호: 체커 칸마다 식생(높음)·토양(낮음)이 번갈아 나옴
    ndvi = (data[1] - data[0]) / (data[1] + data[0])
    assert np.nanmax(ndvi) > 0.6 and np.nanmin(ndvi) < 0.0


def test_refine_rig_recovers_metadata_error(tmp_path: Path):
    """메타데이터 rig가 실제와 0.5° 다르면 밴드가 어긋남 → refine_rig로 맞춰짐."""
    ds, views, factors = _make_flight(tmp_path, rig_meta=[0.0, 0.0, 0.0], rig_true=[0.5, -0.3, 0.0])
    dsm = Dsm(z=np.zeros((3, 3), np.float32), x0=-200, y0=100, res=200, num_points=0)
    sources = build_band_sources(views, ds, factors)
    data, X, Y = _render(tmp_path, ds, views, sources, "before.tif")
    _, shift_before = _error(data, X, Y)
    corr: dict = {}
    for _ in range(2):  # 파이프라인과 같이 두 번 반복
        c, stats = refine_rig(views, sources, ds, dsm, 0.0, step=4)
        assert stats["bands"]["NIR"]["n"] == 3
        assert abs(stats["bands"]["Green"]["shift_px"][0]) < 0.05  # 기준 밴드 자신은 이동이 없어야 함 (치우침 없음)
        corr = {i: c[i] @ corr[i] if i in corr else c[i] for i in c}
        sources = build_band_sources(views, ds, factors, rig_corr=corr)
    data, X, Y = _render(tmp_path, ds, views, sources, "after.tif")
    med, shift_after = _error(data, X, Y)
    assert shift_before > 0.8  # 0.5°면 이 화각에서 약 1화소
    assert shift_after < 0.1 and med < 0.005


def test_capture_gains_remove_offsets_keep_level():
    rng = np.random.default_rng(0)
    ncap, npt = 6, 400
    true_g = np.log([0.8, 1.2, 1.0, 1.1, 0.9, 1.05])
    true_g -= true_g.mean()
    level = np.log(rng.uniform(0.05, 0.5, npt))
    p, c, v = [], [], []
    for i in range(npt):
        for j in rng.choice(ncap, 3, replace=False):
            p.append(i)
            c.append(j)
            v.append(level[i] - true_g[j] + rng.normal(0, 0.01))
    g, before, after = solve_capture_gains(np.array(p), np.array(c), np.array(v), ncap, prior=0.5)
    assert np.allclose(g, true_g, atol=0.02)
    assert abs(g.mean()) < 1e-9  # 전체 수준(평균 이득 1)은 그대로
    assert before > 0.08 and after < 0.02


def _refl(tmp_path: Path) -> Path:
    src = tmp_path / "r.tif"
    data = np.stack([np.full((4, 4), 0.1), np.full((4, 4), 0.05), np.full((4, 4), 0.5)]).astype(np.float32)
    data[:, 0, 0] = np.nan
    prof = {"driver": "GTiff", "width": 4, "height": 4, "count": 3, "dtype": "float32", "nodata": float("nan"),
            "crs": CRS.from_epsg(32652), "transform": rasterio.transform.from_origin(0, 4, 1, 1)}
    with rasterio.open(src, "w", **prof) as d:
        d.write(data)
        for i, n in enumerate(["Green", "Red", "NIR"], 1):
            d.set_band_description(i, n)
    return src


def test_write_selected_indices_only(tmp_path: Path):
    src = _refl(tmp_path)
    bands = ["Green", "Red", "NIR"]
    # v0.4.0 형식 산출물이 남아 있어도 지워짐
    (tmp_path / "indices.tif").write_bytes(b"old")
    (tmp_path / "ndvi.png").write_bytes(b"old")
    tifs, pngs, skipped = write_indices(src, tmp_path, bands, ["NDVI", "NDWI", "NDRE"], finalize_cog)
    assert list(tifs) == ["NDVI", "NDWI"] and skipped == ["NDRE"]  # Red edge 밴드가 없어 NDRE는 건너뜀
    assert sorted(p.name for p in (tmp_path / "indices").iterdir()) == ["NDVI.png", "NDVI.tif", "NDWI.png", "NDWI.tif"]
    assert not (tmp_path / "indices.tif").exists() and not (tmp_path / "ndvi.png").exists()
    assert Path(pngs["NDWI"]).exists()
    with rasterio.open(tifs["NDVI"]) as d:
        ndvi = d.read(1)
        assert d.descriptions == ("NDVI",) and d.count == 1 and d.dtypes[0] == "float32" and np.isnan(d.nodata)
    assert ndvi[1, 1] == pytest.approx((0.5 - 0.05) / 0.55)
    assert np.isnan(ndvi[0, 0])
    # 다시 만들면 고르지 않은 지수 파일은 지워짐
    tifs, _, _ = write_indices(src, tmp_path, bands, ["GNDVI"], finalize_cog)
    assert list(tifs) == ["GNDVI"]
    assert sorted(p.name for p in (tmp_path / "indices").iterdir()) == ["GNDVI.png", "GNDVI.tif"]
    tifs, _, _ = write_indices(src, tmp_path, bands, [], finalize_cog)
    assert tifs == {} and not (tmp_path / "indices").exists()


def test_write_indices_failure_keeps_previous(tmp_path: Path):
    """SDK: 새 지수는 indices.new에 만든 뒤 바꾸므로 도중에 실패해도 이전 지수가 남음."""
    src = _refl(tmp_path)
    bands = ["Green", "Red", "NIR"]
    write_indices(src, tmp_path, bands, ["NDVI"], finalize_cog)

    def broken(tmp, dst):
        raise OSError("디스크 가득 참")

    with pytest.raises(OSError):
        write_indices(src, tmp_path, bands, ["GNDVI"], broken)
    assert sorted(p.name for p in (tmp_path / "indices").iterdir()) == ["NDVI.png", "NDVI.tif"]
    assert not (tmp_path / "indices.new").exists()


def test_index_swap_failure_restores_previous(tmp_path: Path, monkeypatch):
    """바꾸는 도중(새 폴더를 옮기다) 실패하면 이전 지수 폴더를 되돌림 (Windows에서 파일이 잠긴 경우 등)."""
    import quickortho._core.indices as ix

    src = _refl(tmp_path)
    bands = ["Green", "Red", "NIR"]
    write_indices(src, tmp_path, bands, ["NDVI"], finalize_cog)
    real = os.replace

    def flaky(a, b):
        if Path(a).name == "indices.new":
            raise PermissionError("잠김")
        return real(a, b)

    monkeypatch.setattr(ix.os, "replace", flaky)
    with pytest.raises(PermissionError):
        write_indices(src, tmp_path, bands, ["GNDVI"], finalize_cog)
    monkeypatch.undo()
    assert sorted(p.name for p in (tmp_path / "indices").iterdir()) == ["NDVI.png", "NDVI.tif"]
    assert not (tmp_path / "indices.new").exists() and not (tmp_path / "indices.old").exists()
    # 바꾸기 직전 중단(before_swap 예외)도 이전 지수 유지
    with pytest.raises(KeyboardInterrupt):
        write_indices(src, tmp_path, bands, ["GNDVI"], finalize_cog, before_swap=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert sorted(p.name for p in (tmp_path / "indices").iterdir()) == ["NDVI.png", "NDVI.tif"]


def test_index_selection_and_catalog():
    assert normalize_selection(None) == ["NDVI", "NDRE", "GNDVI"]
    assert normalize_selection(["gndvi", "ndvi", "NDVI"]) == ["NDVI", "GNDVI"]  # 대소문자·중복 정리, 등록 순서
    assert normalize_selection([]) == []
    with pytest.raises(ValueError):
        normalize_selection(["XYZ"])
    rededge = ["Blue", "Green", "Red", "Red edge", "NIR"]
    assert set(available_indices(rededge)) == {"NDVI", "NDRE", "GNDVI", "NDWI", "OSAVI", "SAVI", "EVI", "CIre", "CIg"}
    cat = {c["name"]: c for c in index_catalog(["Green", "Red", "NIR"])}
    assert cat["NDVI"]["available"] and not cat["EVI"]["available"] and cat["NDRE"]["default"]
    assert not cat["NDWI"]["default"] and cat["NDWI"]["bands"] == ["green", "nir"]


def test_index_formulas():
    from quickortho import indices as qi

    b = {"Blue": np.float32(0.04), "Green": np.float32(0.08), "Red": np.float32(0.05),
         "Red edge": np.float32(0.2), "NIR": np.float32(0.5)}
    g, r, re, n, bl = 0.08, 0.05, 0.2, 0.5, 0.04
    want = {
        "NDVI": (n - r) / (n + r), "NDRE": (n - re) / (n + re), "GNDVI": (n - g) / (n + g),
        "NDWI": (g - n) / (g + n), "OSAVI": (n - r) / (n + r + 0.16), "SAVI": 1.5 * (n - r) / (n + r + 0.5),
        "EVI": 2.5 * (n - r) / (n + 6 * r - 7.5 * bl + 1), "CIre": n / re - 1, "CIg": n / g - 1,
    }
    assert list(want) == qi.names()
    for k, v in want.items():
        assert float(qi.compute(k.lower(), b)) == pytest.approx(v, rel=1e-5), k
    # 분모가 0에 가까우면 NaN (경고 없이)
    zero = {k: np.zeros(2, np.float32) for k in b}
    for k in qi.names():
        assert np.isnan(qi.compute(k, zero)).all() or k in ("SAVI", "OSAVI", "EVI"), k
    assert np.isnan(qi.compute("EVI", {"NIR": [0.0], "Red": [0.0], "Blue": [1 / 7.5]})).all()


def test_public_indices_api(tmp_path: Path):
    import quickortho as qo
    from quickortho import indices as qi

    with pytest.raises(qo.InputError) as e:
        qi.normalize(["NDVI", "BAD"])
    assert e.value.code == "invalid_argument"
    with pytest.raises(qo.InputError):
        qi.normalize("NDVI")  # 문자열 하나는 받지 않음 (글자 단위로 쪼개지지 않게)
    with pytest.raises(qo.InputError):
        qi.compute("NDRE", {"NIR": np.ones(2), "Red": np.ones(2)})
    with pytest.raises(TypeError):
        qi.INDEX_DEFS["X"] = qi.INDEX_DEFS["NDVI"]  # 읽기 전용
    assert qi.DEFAULT_INDICES == ("NDVI", "NDRE", "GNDVI")
    assert qi.legend(["ndwi"])["NDWI"]["colors"][-1] == "#2166ac"

    src = _refl(tmp_path)
    ndvi, tf, crs = qi.compute_raster(src, "ndvi")
    assert ndvi.shape == (4, 4) and ndvi.dtype == np.float32 and crs.to_epsg() == 32652
    assert ndvi[1, 1] == pytest.approx((0.5 - 0.05) / 0.55) and np.isnan(ndvi[0, 0])
    own = qi.IndexDef("RATIO", "내 지수", ("nir", "red"), lambda nir, red: nir / red, 0, 20)
    v, _, _ = qi.compute_raster(src, own, scale=0.5)
    assert v.shape == (2, 2) and np.nanmax(v) == pytest.approx(10.0)
    with pytest.raises(qo.InputError):
        qi.compute_raster(src, "NDRE")


def test_rgb_preview_from_reflectance(tmp_path: Path):
    from quickortho._core.ms_ortho import write_rgb_preview

    src = _refl(tmp_path)
    info = write_rgb_preview(src, tmp_path / "p.png", ["Green", "Red", "NIR"])
    assert info["preview"] == "false_color"  # Blue가 없으면 NIR·Red·Green 위색
    im = np.asarray(Image.open(tmp_path / "p.png"))
    assert im.shape == (4, 4, 4) and im[0, 0, 3] == 0 and im[1, 1, 3] == 255


def test_panel_missing_in_some_bands_falls_back_to_dls(tmp_path: Path):
    """패널 촬영에서 일부 밴드에 패널 정보가 없으면(실제 Altum 예제의 Green) 패널 보정 없이 DLS만 쓰고 그 밴드를 알림."""
    caps = {}
    for j in range(3):
        caps[f"c{j}"] = {1: _band(f"c{j}_1.tif", f"c{j}", "Green", 1, 560), 3: _band(f"c{j}_3.tif", f"c{j}", "NIR", 3, 842)}
    pg = _band("p_1.tif", "p", "Green", 1, 560)  # Green: 패널을 못 찾음 (영역·반사율 없음)
    pn = _band("p_3.tif", "p", "NIR", 3, 842)
    pn.albedo, pn.panel_area = 0.5, [(40, 30), (120, 30), (120, 90), (40, 90)]
    raw = _dn_from_radiance(pn, np.full((H, W), 0.5 * E / math.pi)).astype(np.uint16)
    Image.fromarray(raw).save(tmp_path / "p_3.tif")
    caps["p"] = {1: pg, 3: pn}
    ds = ms.MsDataset(tmp_path, caps, 1, [1, 3], panel_captures=["p"])
    _, info = ms.reflectance_factors(ds)
    assert info["mode"] == "dls" and info["panel_missing_bands"] == ["Green"]
    assert info["panel"]["NIR"]["reflectance_dls"] == pytest.approx(0.5, rel=1e-3)
    # 사용자가 방식을 직접 고르면 경고 대상이 아님
    _, info2 = ms.reflectance_factors(ds, "dls")
    assert "panel_missing_bands" not in info2
    with pytest.raises(ValueError):
        ms.reflectance_factors(ds, "dls+panel")
