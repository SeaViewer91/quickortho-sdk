"""SfM까지 돌려 볼 수 있는 합성 다중분광(MicaSense 형식) 비행.

``synthetic.py``의 기복 지형·격자 비행을 그대로 쓰고, 캡처마다 Green(기준)·Red·NIR 밴드를 16비트 TIFF로 만든다.
제조사 형식 그대로 Pix4D 카메라 XMP(밴드 이름, rig 번호, 내부표정 mm, 방사보정 계수, DLS 입사광)와
EXIF(흑색 수준, 노출, ISO, 초점면 해상도, GPS)를 넣으므로 실제 다중분광 영상과 같은 경로로 처리된다.

- 반사율 참값: 무늬 영상의 채널로 밴드마다 다른 반사율을 만든다 (:func:`truth`)
- 입사광은 비행 중 20% 서서히 늘고(해가 높아짐), 원시 값에 그대로 반영된다. DLS로 반사율을 복원하면 바뀜이 없어져야 함
- 지상 패널 촬영 1캡처 (반사율 0.5 패널)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, TiffImagePlugin
from pyproj import Transformer

import synthetic as syn

PITCH_MM = 0.00375  # 화소 크기 (mm)
RX = 1.0 / PITCH_MM  # 초점면 해상도 (화소/mm)
BLACK = 100.0
EXPOSURE = 0.001
A1 = 3e-4
BITS = 16
PANEL_ALBEDO = 0.5
# 밴드: (이름, rig 번호, 파장, 무늬 채널, 반사율 = a + b·채널/255)
BANDS = [("Green", 1, 560.0, 1, 0.05, 0.30), ("Red", 2, 668.0, 0, 0.04, 0.20), ("NIR", 3, 842.0, 2, 0.25, 0.40)]


@dataclass
class MsScene:
    folder: Path
    base: syn.Scene  # 카메라 위치·자세 (기준 밴드 영상 이름이 키)
    irradiance: dict[str, float]  # 캡처 → 입사광
    tex: np.ndarray

    def truth(self, band: str, e: np.ndarray, n: np.ndarray) -> np.ndarray:
        """지역 좌표 (e, n)의 밴드 반사율 참값."""
        return truth(self.tex, band, e, n)


def truth(tex: np.ndarray, band: str, e, n) -> np.ndarray:
    _, _, _, ch, a, b = next(x for x in BANDS if x[0] == band)
    xmin, _, _, ymax = syn.EXTENT
    shape = np.shape(e)
    col = ((np.asarray(e, np.float64).ravel() - xmin) / syn.TEX_RES).astype(np.float32)
    row = ((ymax - np.asarray(n, np.float64).ravel()) / syn.TEX_RES).astype(np.float32)
    m = len(col)
    w = 1024
    pad = -m % w  # cv2.remap은 한 변이 32767 미만이어야 하므로 접어서 읽음
    col = np.pad(col, (0, pad)).reshape(-1, w)
    row = np.pad(row, (0, pad)).reshape(-1, w)
    v = cv2.remap(tex[..., ch].astype(np.float32), col, row, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return a + b * v.ravel()[:m].reshape(shape) / 255.0


def _dn(refl: np.ndarray, e: float) -> np.ndarray:
    L = refl * e / math.pi
    scale = A1 / (1.0 * EXPOSURE * 2.0**BITS)
    return np.clip(L / scale + BLACK, 0, 65535).astype(np.uint16)


def _xmp(band: str, index: int, wl: float, capture: str, e: float, panel: bool) -> bytes:
    fmm = syn.F_PX / RX
    ppx, ppy = (syn.W / 2 - 0.5) / RX, (syn.H / 2 - 0.5) / RX  # 읽을 때 +0.5 → COLMAP 주점 (W/2, H/2)
    attrs = {
        "Camera:BandName": band,
        "Camera:CentralWavelength": f"{wl:g}",
        "Camera:RigCameraIndex": str(index),
        "Camera:PerspectiveFocalLength": f"{fmm:.8f}",
        "Camera:PrincipalPoint": f"{ppx:.8f},{ppy:.8f}",
        "Camera:PerspectiveDistortion": "0,0,0,0,0",
        "Camera:RigRelatives": "0,0,0",
        "Camera:Irradiance": f"{e:.6f}",
        "Camera:IrradianceScaleToSIUnits": "1",
        "MicaSense:CaptureId": capture,
        "MicaSense:RadiometricCalibration": f"{A1:g},0,0",
    }
    if panel:
        attrs["Camera:CalibrationPicture"] = "2"
        attrs["Camera:Albedo"] = f"{PANEL_ALBEDO}"
        x0, y0, x1, y1 = syn.W // 2 - 120, syn.H // 2 - 120, syn.W // 2 + 120, syn.H // 2 + 120
        attrs["Camera:ReflectArea"] = f"{x0},{y0},{x1},{y0},{x1},{y1},{x0},{y1}"
    body = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:Camera="http://pix4d.com/camera/1.0" '
        'xmlns:MicaSense="http://micasense.com/MicaSense/1.0/" '
        f"{body}/></rdf:RDF></x:xmpmeta>"
    ).encode()


def _write_tiff(path: Path, raw: np.ndarray, xmp: bytes, lon: float, lat: float, alt: float) -> None:
    # TIFF는 exif= 대신 tiffinfo에 하위 IFD(EXIF 34665, GPS 34853)를 dict로 넣어야 저장됨
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[271] = "MicaSense"
    ifd[272] = "SYN-MS"
    ifd[50714] = BLACK
    ifd[700] = xmp
    ifd.tagtype[700] = 1
    ifd[34665] = {33434: EXPOSURE, 34855: 100, 41486: RX, 41487: RX, 41488: 4}
    ifd[34853] = {1: "N", 2: syn._dms(lat), 3: "E", 4: syn._dms(lon), 5: 0, 6: float(alt)}
    Image.fromarray(raw).save(path, tiffinfo=ifd)


def make_ms_scene(folder: Path, rows: int = 3, cols: int = 4, seed: int = 0) -> MsScene:
    folder.mkdir(parents=True, exist_ok=True)
    tex = syn._texture(seed)
    to_utm = Transformer.from_crs(4326, syn.EPSG, always_xy=True)
    to_ll = Transformer.from_crs(syn.EPSG, 4326, always_xy=True)
    e0, n0 = to_utm.transform(syn.LON0, syn.LAT0)
    gw, gh = syn.ALT * syn.W / syn.F_PX, syn.ALT * syn.H / syn.F_PX
    step_e, step_n = 0.3 * gh, 0.4 * gw
    rng = np.random.default_rng(seed + 1)
    cams, rots, irr = {}, {}, {}
    u, v = np.meshgrid(np.arange(syn.W) + 0.5, np.arange(syn.H) + 0.5)

    def ground(c, R):
        rays = np.stack([(u - syn.W / 2) / syn.F_PX, (v - syn.H / 2) / syn.F_PX, np.ones_like(u)], axis=-1) @ R.T
        z = np.zeros(u.shape)
        for _ in range(8):
            t = (z - c[2]) / rays[..., 2]
            e = c[0] + t * rays[..., 0]
            n = c[1] + t * rays[..., 1]
            z = syn.terrain(e, n)
        return e, n

    k = 0
    for r in range(rows):
        yaw = 90.0 if r % 2 == 0 else 270.0
        order = range(cols) if r % 2 == 0 else reversed(range(cols))
        for c in order:
            ce = c * step_e + rng.normal(0, 0.5)
            cn = r * step_n + rng.normal(0, 0.5)
            cz = syn.ALT + rng.normal(0, 0.3)
            R = syn.rotation(yaw + rng.normal(0, 2.0), rng.uniform(-3, 3), rng.uniform(-3, 3))
            cap = f"CAP{k:04d}"
            e_irr = float(1.0 + 0.2 * k / max(1, rows * cols - 1))  # 비행 중 입사광이 서서히 늘어남
            ge, gn = ground((ce, cn, cz), R)
            lon, lat = to_ll.transform(e0 + ce, n0 + cn)
            for band, idx, wl, _, _, _ in BANDS:
                name = f"IMG_{k:04d}_{idx}.tif"
                raw = _dn(truth(tex, band, ge, gn), e_irr)
                _write_tiff(folder / name, raw, _xmp(band, idx, wl, cap, e_irr, False), lon, lat, cz)
                if idx == 1:
                    cams[name] = (ce, cn, cz)
                    rots[name] = R
            irr[cap] = e_irr
            k += 1
    # 지상 패널 촬영 (반사율 0.5 패널이 영상 가운데). 실제 MicaSense처럼 비행 영상보다 먼저 정렬되는 이름으로 둠
    lon, lat = to_ll.transform(e0 - 20.0, n0 - 20.0)
    for band, idx, wl, _, _, _ in BANDS:
        img = np.full((syn.H, syn.W), 0.1)
        img[syn.H // 2 - 150: syn.H // 2 + 150, syn.W // 2 - 150: syn.W // 2 + 150] = PANEL_ALBEDO
        _write_tiff(folder / f"IMG_0000P_{idx}.tif", _dn(img, 1.0), _xmp(band, idx, wl, "PANEL", 1.0, True),
                    lon, lat, 1.0)
    return MsScene(folder, syn.Scene(folder, cams, rots, (e0, n0)), irr, tex)
