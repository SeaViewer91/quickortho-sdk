"""영상 폴더 스캔: EXIF·DJI XMP 메타데이터 수집과 매핑용 카메라 선별."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .protocol import Emitter

IMAGE_SUFFIXES = {".jpg", ".jpeg"}

# EXIF 태그 번호
_TAG_MAKE = 271
_TAG_MODEL = 272
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_DATETIME_ORIGINAL = 36867
_TAG_FOCAL_LENGTH = 37386
_TAG_FOCAL_35MM = 41989

# XMP는 JPEG 앞부분(APP1)에 있음. DJI 영상은 EXIF 썸네일 뒤에 오므로 여유 있게 읽음
_XMP_READ_BYTES = 512 * 1024
_XMP_RE = re.compile(rb"<x:xmpmeta.*?</x:xmpmeta>", re.DOTALL)
_DJI_ATTR_RE = re.compile(r'drone-dji:(\w+)="([^"]*)"')
_DJI_ELEM_RE = re.compile(r"<drone-dji:(\w+)>([^<]*)</drone-dji:\1>")

# DJI 다중 카메라 기종의 파일명 접미사 (예: DJI_0001_W.JPG = 광각)
_SUFFIX_RE = re.compile(r"_([A-Z])$")

# 롤링 셔터 카메라의 EXIF Model 값 (Mavic 2 Pro: L1D-20c, Mavic 2 Zoom: FC2204)
ROLLING_SHUTTER_MODELS = {"L1D-20C", "FC2204"}

# 짐벌 pitch가 이 값보다 크면(덜 숙이면) 경사 촬영으로 간주
OBLIQUE_PITCH_DEG = -70.0


@dataclass
class ImageRecord:
    file: str
    width: int
    height: int
    make: str | None = None
    model: str | None = None
    datetime: str | None = None
    focal_mm: float | None = None
    focal_35mm: float | None = None
    lat: float | None = None
    lon: float | None = None
    abs_alt: float | None = None
    rel_alt: float | None = None
    gimbal_yaw: float | None = None
    gimbal_pitch: float | None = None
    gimbal_roll: float | None = None
    flight_yaw: float | None = None
    rtk_flag: int | None = None
    camera_key: str = ""
    selected: bool = True
    flags: list[str] = field(default_factory=list)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_deg(dms: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(v) for v in dms)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    if str(ref).strip().upper() in ("S", "W"):
        deg = -deg
    return deg


def read_dji_xmp(path: Path) -> dict[str, str]:
    """파일 앞부분에서 XMP 블록을 찾아 drone-dji 네임스페이스 값을 반환한다."""
    with open(path, "rb") as f:
        head = f.read(_XMP_READ_BYTES)
    match = _XMP_RE.search(head)
    if not match:
        return {}
    text = match.group(0).decode("utf-8", errors="ignore")
    values = dict(_DJI_ATTR_RE.findall(text))
    values.update(_DJI_ELEM_RE.findall(text))
    return values


def read_image(path: Path, root: Path) -> ImageRecord:
    with Image.open(path) as img:  # 픽셀은 디코딩하지 않음 (헤더만 읽음)
        width, height = img.size
        exif = img.getexif()
        exif_ifd = exif.get_ifd(_IFD_EXIF)
        gps = exif.get_ifd(_IFD_GPS)

    rec = ImageRecord(file=path.relative_to(root).as_posix(), width=width, height=height)
    rec.make = (str(exif.get(_TAG_MAKE)).strip("\x00 ") or None) if exif.get(_TAG_MAKE) else None
    rec.model = (str(exif.get(_TAG_MODEL)).strip("\x00 ") or None) if exif.get(_TAG_MODEL) else None
    rec.datetime = exif_ifd.get(_TAG_DATETIME_ORIGINAL)
    rec.focal_mm = _to_float(exif_ifd.get(_TAG_FOCAL_LENGTH))
    rec.focal_35mm = _to_float(exif_ifd.get(_TAG_FOCAL_35MM))

    if gps:
        rec.lat = _dms_to_deg(gps.get(2), gps.get(1))
        rec.lon = _dms_to_deg(gps.get(4), gps.get(3))
        alt = _to_float(gps.get(6))
        if alt is not None and gps.get(5) in (1, b"\x01"):
            alt = -alt
        rec.abs_alt = alt

    xmp = read_dji_xmp(path)
    if xmp:
        rec.rel_alt = _to_float(xmp.get("RelativeAltitude"))
        abs_alt = _to_float(xmp.get("AbsoluteAltitude"))
        if abs_alt is not None:
            rec.abs_alt = abs_alt
        rec.gimbal_yaw = _to_float(xmp.get("GimbalYawDegree"))
        rec.gimbal_pitch = _to_float(xmp.get("GimbalPitchDegree"))
        rec.gimbal_roll = _to_float(xmp.get("GimbalRollDegree"))
        rec.flight_yaw = _to_float(xmp.get("FlightYawDegree"))
        rtk = _to_float(xmp.get("RtkFlag"))
        rec.rtk_flag = int(rtk) if rtk is not None else None
        if rec.lat is None:
            rec.lat = _to_float(xmp.get("GpsLatitude"))
            rec.lon = _to_float(xmp.get("GpsLongitude") or xmp.get("GpsLongtitude"))

    if rec.lat is None or rec.lon is None:
        rec.flags.append("no_gps")
    if rec.gimbal_pitch is not None and rec.gimbal_pitch > OBLIQUE_PITCH_DEG:
        rec.flags.append("oblique")

    focal = f"{rec.focal_mm:.1f}mm" if rec.focal_mm is not None else "?mm"
    rec.camera_key = f"{rec.make or '?'} {rec.model or '?'} | {focal} | {width}x{height}"
    return rec


def _suffix(file: str) -> str | None:
    m = _SUFFIX_RE.search(Path(file).stem)
    return m.group(1) if m else None


def select_mapping_cameras(records: list[ImageRecord]) -> dict[str, str]:
    """기종(make+model)마다 카메라가 여럿이면 광각 카메라만 남긴다.

    서로 다른 기종은 모두 매핑용으로 유지한다(혼합 데이터셋 허용).
    반환값은 제외된 camera_key → 사유.
    """
    by_model: dict[tuple[str | None, str | None], dict[str, list[ImageRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in records:
        by_model[(r.make, r.model)][r.camera_key].append(r)

    excluded: dict[str, str] = {}
    for cams in by_model.values():
        if len(cams) <= 1:
            continue

        def score(item: tuple[str, list[ImageRecord]]) -> tuple[int, float]:
            _, recs = item
            # 1순위: 파일명 접미사 _W(광각)
            is_wide_suffix = any(_suffix(r.file) == "W" for r in recs)
            # 2순위: 35mm 환산 초점거리(없으면 실제 초점거리)가 가장 짧은 카메라
            focals = [r.focal_35mm or r.focal_mm for r in recs if (r.focal_35mm or r.focal_mm)]
            focal = min(focals) if focals else float("inf")
            return (0 if is_wide_suffix else 1, focal)

        keep_key, _ = min(cams.items(), key=score)
        for key, recs in cams.items():
            if key == keep_key:
                continue
            excluded[key] = "같은 기종의 비광각 카메라(망원 등)로 판단하여 제외"
            for r in recs:
                r.selected = False
    return excluded


def scan_folder(folder: Path, recursive: bool = False, emitter: Emitter | None = None) -> dict[str, Any]:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"폴더를 찾을 수 없음: {folder}")

    out = emitter or Emitter()
    pattern = "**/*" if recursive else "*"
    files = sorted(
        p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    out.stage("scan", f"영상 {len(files)}장 스캔 시작")

    records: list[ImageRecord] = []
    failed: list[dict[str, str]] = []
    step = max(1, len(files) // 100)
    for i, path in enumerate(files, 1):
        try:
            records.append(read_image(path, folder))
        except Exception as exc:  # 손상 파일 등은 건너뛰고 기록
            failed.append({"file": path.relative_to(folder).as_posix(), "error": str(exc)})
            out.log(f"읽기 실패: {path.name} ({exc})", level="warn")
        if i % step == 0 or i == len(files):
            out.progress("scan", i, len(files))

    excluded = select_mapping_cameras(records)

    cameras: dict[str, dict[str, Any]] = {}
    for r in records:
        cam = cameras.setdefault(
            r.camera_key,
            {"make": r.make, "model": r.model, "focal_mm": r.focal_mm, "focal_35mm": r.focal_35mm,
             "width": r.width, "height": r.height, "count": 0, "selected": r.selected},
        )
        cam["count"] += 1

    selected = [r for r in records if r.selected]
    summary = {
        "total_files": len(files),
        "read_ok": len(records),
        "read_failed": len(failed),
        "selected": len(selected),
        "no_gps": sum("no_gps" in r.flags for r in selected),
        "oblique": sum("oblique" in r.flags for r in selected),
        "rtk_fixed": sum(r.rtk_flag == 50 for r in selected),
        "rolling_shutter_warning": any(
            (r.model or "").upper() in ROLLING_SHUTTER_MODELS for r in selected
        ),
    }
    return {
        "folder": str(folder),
        "summary": summary,
        "cameras": cameras,
        "excluded_cameras": excluded,
        "failed": failed,
        "images": [asdict(r) for r in records],
    }
