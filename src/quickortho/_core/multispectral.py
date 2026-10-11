"""다중분광 카메라(MicaSense RedEdge·Altum 계열): 메타데이터, 캡처 묶기, 방사보정, 밴드별 카메라.

MicaSense는 Pix4D 카메라 XMP 네임스페이스(``Camera:``)와 자체 네임스페이스(``MicaSense:``, ``DLS:``)에
밴드 이름, 보정된 내부표정, 밴드 간 상대 자세(rig), 방사보정 계수, 입사광 센서 값을 기록한다.
방사보정식은 제조사가 공개한 모델을 따른다.

    L = V(x, y) · R(y) · (p − p_BL) / (g · t_e) · a1 / 2^N,   R(y) = 1 / (1 + a2·y / t_e − a3·y)

- p: 원시 값, p_BL: 흑색 수준, g: 이득(ISO/100), t_e: 노출(초), N: 비트 수
- V: 주변 감광 보정 (방사형 다항식 또는 2차원 다항식의 역수)
- 반사율 ρ = π·L / E. E는 입사광 센서(DLS)의 수평 입사광. 패널 촬영이 있으면 패널 기준 반사율로 DLS 편향을 보정함

정사투영은 기준 밴드(팬크로매틱 또는 Green)로 SfM을 하고, 각 밴드는 기준 밴드 자세에 rig 상대 자세를 더한
자기 카메라로 DSM에 직접 투영한다. 밴드를 영상 평면에서 겹치지 않으므로 저고도·높은 물체의 시차도 DSM만큼 정확함.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

TIFF_SUFFIXES = {".tif", ".tiff"}

# 출력에서 제외하는 밴드 (팬크로매틱은 기준 밴드로만 쓰고, 열적외는 반사율이 아님)
_PAN_NAMES = {"panchro", "panchromatic", "pan"}
_THERMAL_NAMES = {"lwir", "thermal"}

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_TAG_BITS = 258
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_BLACK_LEVEL = 50714
_TAG_EXPOSURE = 33434
_TAG_ISO = 34867
_TAG_ISO_ALT = 34855
_TAG_DATETIME = 36867
_TAG_SUBSEC = 37521
_TAG_FP_XRES = 41486
_TAG_FP_YRES = 41487
_TAG_FP_UNIT = 41488

_ELEM_RE = re.compile(r"<((?:Camera|MicaSense|DLS):\w+)>(.*?)</\1>", re.S)
_ATTR_RE = re.compile(r'\b((?:Camera|MicaSense|DLS):\w+)="([^"]*)"')
_LI_RE = re.compile(r"<rdf:li>(.*?)</rdf:li>", re.S)


def read_xmp(path: Path) -> dict[str, Any]:
    """TIFF의 XMP(태그 700)에서 Camera/MicaSense/DLS 값을 읽는다. 배열(rdf:Seq)은 문자열 목록으로 반환."""
    with Image.open(path) as im:
        raw = im.tag_v2.get(700) if hasattr(im, "tag_v2") else None
    if raw is None:
        return {}
    if isinstance(raw, tuple):
        raw = b"".join(raw) if raw and isinstance(raw[0], bytes) else bytes(raw)
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    out: dict[str, Any] = dict(_ATTR_RE.findall(text))
    for key, body in _ELEM_RE.findall(text):
        items = _LI_RE.findall(body)
        out[key] = [s.strip() for s in items] if items else body.strip()
    return out


def _floats(v: Any) -> list[float] | None:
    if v is None:
        return None
    if not isinstance(v, str):
        v = ",".join(str(x) for x in v)
    v = [s for s in re.split(r"[,\s]+", v.strip()) if s]
    try:
        return [float(x) for x in v]
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    vals = _floats(v)
    return vals[0] if vals else None


def _rational(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms(dms: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    ref = ref.decode(errors="ignore") if isinstance(ref, bytes) else str(ref)
    return -deg if ref.strip().upper() in ("S", "W") else deg


@dataclass
class BandImage:
    """다중분광 밴드 영상 한 장의 메타데이터."""

    file: str  # 영상 폴더 기준 상대 경로
    capture: str  # 같은 순간에 찍은 밴드 묶음 식별자
    band: str
    index: int  # rig 안의 카메라 번호
    wavelength: float | None
    width: int
    height: int
    make: str | None
    model: str | None
    bits: int
    black_level: float
    exposure: float
    gain: float
    radcal: list[float] | None
    vig_center: list[float] | None
    vig_poly: list[float] | None
    vig2d: list[float] | None
    vig2d_exp: list[float] | None
    fx: float
    fy: float
    cx: float  # COLMAP 화소 좌표 (왼쪽 위 모서리가 0, 첫 화소 중심이 0.5)
    cy: float
    dist: list[float]  # OpenCV 순서 k1, k2, p1, p2, k3
    rig_rot_deg: list[float] | None
    rig_trans_mm: list[float] | None
    irradiance: float | None  # W/m²/nm, 수평면 입사광 (DLS 2) 또는 센서 입사광
    albedo: float | None  # 패널 촬영이면 카메라가 읽은 패널 반사율
    panel_area: list[tuple[int, int]] | None
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    datetime: str | None = None
    cam_ypr: list[float] | None = None  # Camera:Yaw/Pitch/Roll (있는 기종만)

    @property
    def is_panel(self) -> bool:
        return self.albedo is not None and self.panel_area is not None

    @property
    def is_pan(self) -> bool:
        return self.band.strip().lower() in _PAN_NAMES

    @property
    def is_thermal(self) -> bool:
        return self.band.strip().lower() in _THERMAL_NAMES


def read_band(path: Path, root: Path) -> BandImage | None:
    """다중분광 밴드 영상이면 메타데이터를, 아니면 None을 반환한다."""
    xmp = read_xmp(path)
    band = xmp.get("Camera:BandName")
    if not band or "Camera:RigCameraIndex" not in xmp:
        return None
    with Image.open(path) as im:
        width, height = im.size
        exif = im.getexif()
        ex = exif.get_ifd(_EXIF_IFD)
        gps = exif.get_ifd(_GPS_IFD)
        bits = exif.get(_TAG_BITS)
        black = exif.get(_TAG_BLACK_LEVEL)
    if isinstance(bits, (tuple, list)):
        bits = bits[0]
    bits = int(bits or 16)
    black_level = float(np.mean(_floats(list(black)) if isinstance(black, (tuple, list)) else [black or 0]))
    exposure = _rational(ex.get(_TAG_EXPOSURE)) or 0.0
    iso = ex.get(_TAG_ISO) or ex.get(_TAG_ISO_ALT) or 100
    if isinstance(iso, (tuple, list)):
        iso = iso[0]
    gain = float(iso) / 100.0

    # 내부표정: 초점거리·주점은 mm, 초점면 해상도(화소/mm)로 화소 단위로 바꾼다
    unit = ex.get(_TAG_FP_UNIT) or 4
    to_mm = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}.get(int(unit), 1.0)
    rx = (_rational(ex.get(_TAG_FP_XRES)) or 0.0) / to_mm
    ry = (_rational(ex.get(_TAG_FP_YRES)) or 0.0) / to_mm
    focal_mm = _float(xmp.get("Camera:PerspectiveFocalLength"))
    pp = _floats(xmp.get("Camera:PrincipalPoint"))
    if not (rx and ry and focal_mm):
        raise ValueError(f"{path.name}: 초점거리·초점면 해상도 메타데이터가 없음")
    if not pp:
        pp = [width / 2 / rx, height / 2 / ry]
    d = _floats(xmp.get("Camera:PerspectiveDistortion")) or [0.0] * 5
    d = (d + [0.0] * 5)[:5]  # 메타데이터 순서 k1, k2, k3, p1, p2
    dist = [d[0], d[1], d[3], d[4], d[2]]

    # 입사광 단위 배율: 태그가 있으면 그 값, DLS 2(수평 입사광 태그 있음)는 0.01, DLS 1은 1 (제조사 공개 규칙)
    horiz = _float(xmp.get("DLS:HorizontalIrradiance"))
    spectral = _float(xmp.get("Camera:Irradiance")) or _float(xmp.get("DLS:SpectralIrradiance"))
    scale = _float(xmp.get("Camera:IrradianceScaleToSIUnits"))
    if scale is None:
        scale = 0.01 if "DLS:HorizontalIrradiance" in xmp else 1.0
    raw_e = horiz if horiz else spectral
    irradiance = raw_e * scale if raw_e else None

    area = _floats(xmp.get("Camera:ReflectArea"))
    panel_area = [(int(area[i]), int(area[i + 1])) for i in range(0, len(area) - 1, 2)] if area else None
    cal = _float(xmp.get("Camera:CalibrationPicture"))
    albedo = _float(xmp.get("Camera:Albedo")) if cal == 2 else None

    ypr = [_float(xmp.get(k)) for k in ("Camera:Yaw", "Camera:Pitch", "Camera:Roll")]
    lat = lon = alt = None
    if gps:
        lat = _dms(gps.get(2), gps.get(1))
        lon = _dms(gps.get(4), gps.get(3))
        alt = _rational(gps.get(6))
        if alt is not None and gps.get(5) in (1, b"\x01"):
            alt = -alt
    capture = xmp.get("MicaSense:CaptureId") or re.sub(r"_\d+$", "", path.stem)
    make = exif.get(_TAG_MAKE)
    model = exif.get(_TAG_MODEL)
    return BandImage(
        file=path.relative_to(root).as_posix(),
        capture=str(capture),
        band=str(band),
        index=int(_float(xmp.get("Camera:RigCameraIndex")) or 0),
        wavelength=_float(xmp.get("Camera:CentralWavelength")),
        width=width,
        height=height,
        make=str(make).strip("\x00 ") if make else None,
        model=str(model).strip("\x00 ") if model else None,
        bits=bits,
        black_level=black_level,
        exposure=exposure,
        gain=gain,
        radcal=_floats(xmp.get("MicaSense:RadiometricCalibration")),
        vig_center=_floats(xmp.get("Camera:VignettingCenter")),
        vig_poly=_floats(xmp.get("Camera:VignettingPolynomial")),
        vig2d=_floats(xmp.get("Camera:VignettingPolynomial2D")),
        vig2d_exp=_floats(xmp.get("Camera:VignettingPolynomial2DName")),
        fx=focal_mm * rx,
        fy=focal_mm * ry,
        cx=pp[0] * rx + 0.5,
        cy=pp[1] * ry + 0.5,
        dist=dist,
        rig_rot_deg=_floats(xmp.get("Camera:RigRelatives")),
        rig_trans_mm=_floats(xmp.get("Camera:RigTranslations")),
        irradiance=irradiance,
        albedo=albedo,
        panel_area=panel_area,
        lat=lat,
        lon=lon,
        alt=alt,
        datetime=ex.get(_TAG_DATETIME),
        cam_ypr=ypr if all(v is not None for v in ypr) else None,
    )


# ── 방사보정 ──────────────────────────────────────────────


def read_raw(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im, dtype=np.float32)


def vignette(b: BandImage) -> np.ndarray:
    """주변 감광 보정 배율 V (원시 값에 곱함)."""
    y, x = np.mgrid[0 : b.height, 0 : b.width].astype(np.float64)
    if b.vig_center and b.vig_poly:
        r = np.hypot(x - b.vig_center[0], y - b.vig_center[1])
        poly = np.ones_like(r)
        rp = np.ones_like(r)
        for k in b.vig_poly:  # 1 + k0·r + k1·r² + …
            rp *= r
            poly += k * rp
        return (1.0 / poly).astype(np.float32)
    if b.vig2d and b.vig2d_exp:
        xv, yv = x / b.width, y / b.height
        p = np.zeros_like(xv)
        for i, c in enumerate(b.vig2d):
            p += c * xv ** b.vig2d_exp[2 * i] * yv ** b.vig2d_exp[2 * i + 1]
        return (1.0 / p).astype(np.float32)
    return np.ones((b.height, b.width), np.float32)


def radiance(b: BandImage, raw: np.ndarray, vig: np.ndarray | None = None) -> np.ndarray:
    """원시 값 → 분광 방사휘도 (W/m²/sr/nm). 음수(잡음)는 자르지 않는다."""
    if not b.radcal or len(b.radcal) < 3 or b.exposure <= 0:
        raise ValueError(f"{b.file}: 방사보정 계수가 없음")
    a1, a2, a3 = b.radcal[:3]
    te = b.exposure
    y = np.arange(b.height, dtype=np.float64)[:, None]
    row = 1.0 / (1.0 + a2 * y / te - a3 * y)
    v = vignette(b) if vig is None else vig
    scale = a1 / (b.gain * te * float(2**b.bits))
    return ((raw - b.black_level) * v * row * scale).astype(np.float32)


def panel_mask(b: BandImage, shrink: float = 0.15) -> np.ndarray | None:
    """패널 유효 영역(사각형 네 꼭짓점)을 중심 쪽으로 줄인 마스크. 모서리·테두리 번짐을 피한다."""
    import cv2

    if not b.panel_area or len(b.panel_area) < 3:
        return None
    pts = np.asarray(b.panel_area, np.float64)
    c = pts.mean(axis=0)
    pts = c + (pts - c) * (1.0 - shrink)
    mask = np.zeros((b.height, b.width), np.uint8)
    cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 1)
    return mask.astype(bool)


def panel_stats(b: BandImage, root: Path) -> dict | None:
    """패널 촬영 영상의 패널 영역 방사휘도 평균과 DLS 기준 반사율."""
    m = panel_mask(b)
    if m is None or not m.any():
        return None
    raw = read_raw(root / b.file)
    sat = float((raw[m] >= 0.98 * (2**b.bits - 1)).mean())
    L = radiance(b, raw)[m]
    mean_l = float(np.mean(L))
    out = {
        "band": b.band,
        "albedo": b.albedo,
        "radiance": mean_l,
        "radiance_cv": float(np.std(L) / max(mean_l, 1e-12)),
        "saturated_frac": sat,
        "irradiance": b.irradiance,
    }
    if b.irradiance:
        out["reflectance_dls"] = math.pi * mean_l / b.irradiance
    return out


# ── rig: 밴드별 카메라 ─────────────────────────────────────


def rig_rotation(deg: list[float] | None) -> np.ndarray:
    """MicaSense RigRelatives(도) → 회전행렬 Rx·Ry·Rz."""
    if not deg:
        return np.eye(3)
    a, b, c = np.deg2rad(deg[:3])
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    return rx @ ry @ rz


def band_from_ref(band: BandImage, ref: BandImage) -> tuple[np.ndarray, np.ndarray]:
    """기준 밴드 카메라 좌표 → 밴드 카메라 좌표 변환 (R, c). x_band = R·(x_ref − c).

    c는 기준 카메라 좌표에서 밴드 카메라 중심(m). 회전은 둘 다 rig 기준 카메라에 대한 값이라 R_b^T·R_ref.
    """
    R = rig_rotation(band.rig_rot_deg).T @ rig_rotation(ref.rig_rot_deg)
    if band.rig_trans_mm and ref.rig_trans_mm:
        c = (np.asarray(band.rig_trans_mm[:3]) - np.asarray(ref.rig_trans_mm[:3])) / 1000.0
    else:
        c = np.zeros(3)
    return R, c


def colmap_camera(b: BandImage, camera_id: int = 1):
    """메타데이터 내부표정으로 만든 pycolmap 카메라 (FULL_OPENCV: 방사 k1~k3, 접선 p1·p2)."""
    import pycolmap

    k1, k2, p1, p2, k3 = b.dist
    cam = pycolmap.Camera.create_from_model_name(camera_id, "FULL_OPENCV", b.fx, b.width, b.height)
    cam.params = np.array([b.fx, b.fy, b.cx, b.cy, k1, k2, p1, p2, k3, 0.0, 0.0, 0.0], dtype=np.float64)
    return cam


def camera_params_string(b: BandImage) -> str:
    """COLMAP FULL_OPENCV 파라미터 문자열 (특징점 추출 시 초깃값)."""
    k1, k2, p1, p2, k3 = b.dist
    vals = [b.fx, b.fy, b.cx, b.cy, k1, k2, p1, p2, k3, 0.0, 0.0, 0.0]
    return ",".join(f"{v:.10g}" for v in vals)


# ── 데이터셋 ─────────────────────────────────────────────


@dataclass
class MsDataset:
    root: Path
    captures: dict[str, dict[int, BandImage]]  # capture → rig index → 밴드
    ref_index: int
    out_indices: list[int]  # 출력 밴드 (파장 순)
    panel_captures: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    @property
    def flight_captures(self) -> list[str]:
        return [c for c in self.captures if c not in self.panel_captures and c not in self.incomplete]

    def ref(self, capture: str) -> BandImage:
        return self.captures[capture][self.ref_index]

    def band_names(self) -> list[str]:
        any_cap = next(iter(self.captures.values()))
        return [any_cap[i].band for i in self.out_indices]

    def wavelengths(self) -> list[float | None]:
        any_cap = next(iter(self.captures.values()))
        return [any_cap[i].wavelength for i in self.out_indices]

    def capture_of_ref_file(self) -> dict[str, str]:
        return {self.ref(c).file: c for c in self.captures if self.ref_index in self.captures[c]}

    @property
    def model(self) -> str:
        b = self.ref(next(iter(self.captures)))
        return f"{b.make or ''} {b.model or ''}".strip()


def build_dataset(bands: list[BandImage], root: Path) -> MsDataset | None:
    if not bands:
        return None
    caps: dict[str, dict[int, BandImage]] = {}
    for b in bands:
        caps.setdefault(b.capture, {})[b.index] = b
    # 가장 많은 캡처가 가진 밴드 구성을 정상으로 봄
    from collections import Counter

    sig = Counter(tuple(sorted(c)) for c in caps.values()).most_common(1)[0][0]
    full = set(sig)
    sample = next(c for c in caps.values() if set(c) == full)
    pan = [i for i in sig if sample[i].is_pan]
    if pan:
        ref_index = pan[0]
    else:
        # 팬크로매틱이 없으면 Green(560nm 부근)을 기준 밴드로
        cand = [i for i in sig if not sample[i].is_thermal and sample[i].wavelength]
        ref_index = min(cand, key=lambda i: abs(sample[i].wavelength - 560)) if cand else sig[0]
    out = [i for i in sig if not sample[i].is_pan and not sample[i].is_thermal]
    out.sort(key=lambda i: sample[i].wavelength or 0)
    panels = sorted(c for c, d in caps.items() if any(b.is_panel for b in d.values()))
    incomplete = sorted(c for c, d in caps.items() if not full <= set(d))
    return MsDataset(root, caps, ref_index, out, panels, incomplete)


def scan_multispectral(folder: Path, files: list[Path] | None = None) -> MsDataset | None:
    folder = Path(folder)
    if files is None:
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in TIFF_SUFFIXES)
    bands = []
    for p in files:
        try:
            b = read_band(p, folder)
        except Exception:  # noqa: BLE001 - 손상·비다중분광 TIFF는 건너뜀
            b = None
        if b is not None:
            bands.append(b)
    return build_dataset(bands, folder)


# ── 반사율 환산 계수 ──────────────────────────────────────


def smooth_irradiance(values: list[float | None], window: int = 5) -> list[float | None]:
    """촬영 순서대로 놓인 입사광 값을 이동 중앙값으로 다듬는다 (DLS 기울기·순간 잡음 억제)."""
    arr = np.array([np.nan if v is None else v for v in values], dtype=np.float64)
    out: list[float | None] = []
    h = window // 2
    for i in range(len(arr)):
        seg = arr[max(0, i - h) : i + h + 1]
        seg = seg[np.isfinite(seg)]
        out.append(float(np.median(seg)) if len(seg) else None)
    return out


def reflectance_factors(ds: MsDataset, mode: str = "auto") -> tuple[dict[str, dict[int, float]], dict]:
    """캡처·밴드마다 방사휘도에 곱할 반사율 환산 계수와 보고서 정보를 만든다.

    mode: auto(가능한 최선), dls+panel, dls, panel, radiance
    - dls: ρ = π·L / E(t)  (E는 시간 순 이동 중앙값)
    - panel: ρ = L · albedo / L_panel  (비행 중 조도 변화는 반영 못 함)
    - dls+panel: DLS 방식에 패널로 구한 밴드별 배율 k = albedo / ρ_panel(DLS)를 곱함
    """
    flights = sorted(ds.flight_captures, key=lambda c: (ds.ref(c).datetime or "", ds.ref(c).file))
    has_dls = all(ds.captures[c][i].irradiance for c in flights for i in ds.out_indices) if flights else False
    panels: dict[int, list[dict]] = {}
    for c in ds.panel_captures:
        for i in ds.out_indices:
            b = ds.captures[c].get(i)
            if b is None or b.albedo is None:
                continue
            st = panel_stats(b, ds.root)
            if st:
                panels.setdefault(i, []).append(st)
    has_panel = len(panels) == len(ds.out_indices)

    mode_requested = mode
    if mode == "auto":
        mode = "dls+panel" if has_dls and has_panel else "dls" if has_dls else "panel" if has_panel else "radiance"
    if mode in ("dls", "dls+panel") and not has_dls:
        raise ValueError("입사광 센서(DLS) 값이 없는 캡처가 있어 DLS 방식을 쓸 수 없음")
    if mode in ("panel", "dls+panel") and not has_panel:
        raise ValueError("패널 촬영 영상이 없어 패널 방식을 쓸 수 없음")

    requested = mode_requested
    info: dict[str, Any] = {"mode": mode, "bands": ds.band_names(), "panel_captures": len(ds.panel_captures)}
    if requested == "auto" and ds.panel_captures and not has_panel:
        # 패널 촬영은 있지만 일부 밴드에 패널 영역·기준 반사율이 기록되지 않음 (카메라가 그 밴드에서 패널을 못 찾음).
        # 패널 방식은 모든 밴드에 패널이 있어야 쓰므로 이때는 DLS만 씀 → 보고서 경고로 알림
        names = {i: ds.captures[next(iter(ds.captures))][i].band for i in ds.out_indices}
        info["panel_missing_bands"] = [names[i] for i in ds.out_indices if i not in panels]
    k_panel = {i: 1.0 for i in ds.out_indices}
    if mode == "dls+panel":
        for i in ds.out_indices:
            refl = [p["reflectance_dls"] / p["albedo"] for p in panels[i] if p.get("reflectance_dls")]
            k_panel[i] = 1.0 / float(np.median(refl))
    if panels:
        info["panel"] = {
            ds.captures[next(iter(ds.captures))][i].band: {
                "albedo": float(np.median([p["albedo"] for p in v])),
                "radiance": float(np.median([p["radiance"] for p in v])),
                "reflectance_dls": (float(np.median([p["reflectance_dls"] for p in v]))
                                    if all("reflectance_dls" in p for p in v) else None),
                "saturated_frac": max(p["saturated_frac"] for p in v),
                "count": len(v),
            }
            for i, v in panels.items()
        }

    factors: dict[str, dict[int, float]] = {c: {} for c in flights}
    if mode in ("dls", "dls+panel"):
        cv = {}
        for i in ds.out_indices:
            raw_e = [ds.captures[c][i].irradiance for c in flights]
            sm = smooth_irradiance(raw_e)
            for c, e in zip(flights, sm):
                factors[c][i] = math.pi / e * k_panel[i]
            arr = np.array(raw_e, dtype=np.float64)
            cv[ds.captures[flights[0]][i].band] = float(np.std(arr) / np.mean(arr)) if len(arr) > 1 else 0.0
        info["irradiance_cv"] = cv
        info["panel_correction"] = {ds.captures[flights[0]][i].band: k_panel[i] for i in ds.out_indices}
    elif mode == "panel":
        for i in ds.out_indices:
            f = float(np.median([p["albedo"] / p["radiance"] for p in panels[i]]))
            for c in flights:
                factors[c][i] = f
    else:
        for c in flights:
            for i in ds.out_indices:
                factors[c][i] = 1.0
    return factors, info


# ── SfM용 기준 밴드 영상 ───────────────────────────────────


def proxy_name(ref_file: str) -> str:
    """기준 밴드 파일에 대응하는 SfM용 8비트 영상 이름 (원래 상대 경로 + .jpg)."""
    return ref_file + ".jpg"


def ref_file_of_proxy(name: str) -> str:
    return name[:-4] if name.endswith(".jpg") else name


def write_ref_proxies(
    ds: MsDataset, captures: list[str], out_dir: Path, quality: int = 92, progress=None,
) -> list[str]:
    """기준 밴드를 SfM·사진 보기용 8비트 JPEG로 바꿔 저장한다.

    12·16비트 원시 값을 그대로 줄이면 어두운 곳의 질감이 사라져 특징점이 줄어든다.
    흑색 수준·주변 감광·노출을 보정한 뒤 비행 전체에 같은 배율로 늘이고(감마 0.5), 국소 대비(CLAHE)를 높인다.
    progress: (완료 수, 전체 수)를 받는 함수 (SDK: 진행 이벤트·중단 확인 지점)
    """
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)
    refs = [ds.ref(c) for c in captures]
    vig = vignette(refs[0])

    def intensity(b: BandImage) -> np.ndarray:
        raw = read_raw(ds.root / b.file)
        return (raw - b.black_level) * vig / (b.gain * b.exposure)

    sample = refs[:: max(1, len(refs) // 12)]
    hi = float(np.median([np.percentile(intensity(b)[::4, ::4], 99.5) for b in sample]))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    names = []
    for j, b in enumerate(refs, 1):
        x = np.clip(intensity(b) / max(hi, 1e-9), 0.0, 1.0) ** 0.5
        img = clahe.apply((x * 255.0 + 0.5).astype(np.uint8))
        name = proxy_name(b.file)
        dst = out_dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        names.append(name)
        if progress is not None and (j % max(1, len(refs) // 50) == 0 or j == len(refs)):
            progress(j, len(refs))
    return names
