"""바다·호수 수면 처리 (0.5.0).

정사 처리는 :attr:`quickortho.OrthoOptions.water`(기본 ``"auto"``)로 수면을 자동으로 처리하므로 이 모듈이 필요 없음.
판별 결과를 직접 보거나, 자기 수면 마스크(NDWI, 딥러닝 분할 결과 등)를 쓰거나, DSM에 수면 평면만 따로 적용할 때 씀.

예::

    from quickortho import water as qw

    det = qw.detect("flight/")                       # 영상별 수면 마스크 (0 육지, 1 수면 후보, 2 확실한 수면)
    print(det.fraction, det.active("auto"))          # 영상 면적 중 수면 비율, auto면 켤지
    m = qw.classify(rgb_array)                       # 영상 한 장 (RGB 배열 또는 파일 경로)

    # 자기 마스크로 처리: 정렬 전에 넣으면 특징점 제외에도, 정사에서는 DSM 수면 평면에 씀
    p = qo.Project.create("flight/", "out/")
    p.set_water_masks({"DJI_0001.JPG": my_mask, ...})        # 0이 아니면 수면
    p.process(qo.OrthoOptions(water="on"))

    # 이미 만든 DSM에 수면 평면만 적용 (산출물을 변수로)
    z, tf, crs = qo.read_dsm("out/")
    z2, water_mask, level, stats = qw.flatten_dsm(z, tf, p.cameras(), qw.read_masks("out/"), p.points())

판별·확정 기준은 문서 ``docs/concepts.md``의 "수면 처리" 절 참고. 이 모듈은 1.0 전까지 이름·반환 형식이 바뀔 수 있음.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

import numpy as np

from ._core import water as _w
from .errors import InputError

MODES = _w.MODES
#: 마스크 값: 육지, 수면 후보, 확실한 수면 (확실한 수면만 정렬에서 특징점을 뺌)
LAND, CANDIDATE, CONFIDENT = _w.LAND, _w.CANDIDATE, _w.CONFIDENT
#: ``"auto"``가 켜지는 영상 면적 중 수면 비율 평균
AUTO_MIN_FRACTION = _w.AUTO_MIN_FRACTION
#: 저장·판정 마스크의 긴 변(px)
MASK_PX = _w.MASK_PX

PathLike = Union[str, Path]


@dataclass
class WaterDetection:
    """영상별 수면 판별 결과 (:func:`detect`).

    Attributes:
        masks: 영상 이름 → 마스크 ``(h, w)`` uint8 {0, 1, 2}, 긴 변 480px.
        fraction: 영상 면적 중 수면(1, 2) 비율 평균.
        images_with_water: 수면이 영상의 1% 넘게 보이는 영상 수.
        confident_fraction: 영상 면적 중 확실한 수면(2) 비율 평균.
    """

    masks: dict[str, np.ndarray] = field(repr=False)
    fraction: float
    images_with_water: int
    confident_fraction: float

    def active(self, mode: str = "auto") -> bool:
        """이 결과로 수면 처리를 켤지 (``"auto"``: 수면 비율 평균이 :data:`AUTO_MIN_FRACTION` 이상)."""
        return _w.decide(normalize_mode(mode), _w.summarize(self.masks))


def normalize_mode(mode: Optional[str]) -> str:
    """``"auto"``, ``"on"``, ``"off"`` (대소문자·공백 무시, ``None``은 auto). 그 밖이면 :class:`~quickortho.InputError`."""
    try:
        return _w.normalize_mode(mode)
    except ValueError as e:
        raise InputError(str(e), "invalid_argument") from None


def classify(image: Union[np.ndarray, PathLike], **params: Any) -> np.ndarray:
    """영상 한 장의 수면 마스크 (긴 변 480px, 0/1/2).

    image: RGB 배열 ``(H, W, 3)`` uint8 또는 파일 경로. 경로면 긴 변 1600px로 읽음 (판별은 이 해상도의 세밀한 질감을 씀.
        배열을 넘길 때도 긴 변 1500~2000px 정도가 맞음. 너무 작으면 잔디·그늘이 매끈해 보여 수면으로 잡힐 수 있음).
    params: 판별 기준 (``hp_max``, ``min_frac``, ``confident_frac``, ``confident_radius``). 기본값이 앱·SDK 처리와 같음.
    """
    if not isinstance(image, np.ndarray):
        p = Path(image).expanduser()
        if not p.exists():
            raise InputError(f"파일이 없음: {p}", "file_not_found")
        image = _w._load(p, _w.WORK_PX)
    if image.ndim != 3 or image.shape[2] < 3:
        raise InputError("image는 (H, W, 3) RGB 배열이어야 함", "invalid_argument")
    rgb = image[..., :3]
    if rgb.dtype != np.uint8:
        rgb = np.clip(np.nan_to_num(rgb.astype(np.float32), nan=0.0), 0, 255).astype(np.uint8)
    return _w.classify(rgb, **params)


def _image_list(images: Union[PathLike, Iterable[PathLike]]) -> tuple[list[str], dict[str, Path]]:
    if isinstance(images, (str, Path)):
        folder = Path(images).expanduser()
        if not folder.is_dir():
            raise InputError(f"폴더를 찾을 수 없음: {folder}", "folder_not_found")
        from ._core.scan import scan_folder

        scan = scan_folder(folder)
        names = [im["file"] for im in scan["images"] if im["selected"] and im.get("band") is None]
        return names, {n: folder / n for n in names}
    paths = [Path(p).expanduser() for p in images]
    names = [p.name for p in paths]
    if len(set(names)) != len(names):
        raise InputError("파일 이름이 겹침 (마스크는 파일 이름으로 구분함). 폴더로 넘기면 하위 경로를 이름으로 씀",
                         "invalid_argument")
    for p in paths:
        if not p.exists():
            raise InputError(f"파일이 없음: {p}", "file_not_found")
    return names, dict(zip(names, paths))


def detect(images: Union[PathLike, Iterable[PathLike]], *, threads: int = -1,
           on_event=None, cancel=None) -> WaterDetection:
    """영상마다 수면 마스크를 만듦 (영상당 약 0.15초, 2코어).

    images: 영상 폴더(스캔해서 처리 대상 영상만, 이름은 폴더 기준 경로) 또는 파일 경로 목록(이름은 파일 이름).
    on_event·cancel: 진행 이벤트(``water`` 단계)와 중단. :mod:`quickortho.events` 참고.
    """
    from .project import _start

    names, paths = _image_list(images)
    if not names:
        raise InputError("영상이 없음", "no_images")
    out = _start(on_event, cancel)
    out.stage("water", f"수면 판별 ({len(names)}장)")
    det = _w.detect(None, names, out, threads, paths=paths)
    return WaterDetection(det.masks, det.fraction, det.images_with_water, det.stats["confident_fraction"])


def read_masks(workspace: PathLike) -> dict[str, np.ndarray]:
    """워크스페이스에 저장된 수면 마스크 (``project/water/``). 없으면 빈 dict."""
    return _w.load_masks(Path(workspace).expanduser() / "project" / "water")


def normalize_masks(masks: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """마스크 묶음을 SDK 형식(긴 변 480px 이하, 0 육지·1 수면 후보·2 확실한 수면)으로 바꿈.

    값 해석은 묶음 전체로 정함:

    - 모두 0/100/200이면 SDK가 저장한 PNG 형식으로 보고 100으로 나눔
    - bool이 아니고 모두 0/1/2이며 어딘가에 2가 있으면 SDK 형식(:func:`detect` 결과) 그대로
    - 그 밖(bool, 0/1, 0/255, 확률 등)은 0보다 크면 확실한 수면(2)

    3차원 배열은 첫 채널을 씀. 이름의 ``\\``는 ``/``로 바꿈.
    """
    import cv2

    arrays: dict[str, np.ndarray] = {}
    for name, m in masks.items():
        a = np.asarray(m)
        if a.ndim == 3:
            a = a[..., 0]
        if a.ndim != 2 or a.size == 0:
            raise InputError(f"마스크는 2차원 배열이어야 함: {name}", "invalid_argument")
        arrays[str(name).replace("\\", "/")] = a
    any_bool = any(a.dtype == bool for a in arrays.values())
    vals = set()
    for a in arrays.values():
        if a.dtype != bool:
            vals |= set(np.unique(np.nan_to_num(a.astype(np.float32), nan=0.0)).tolist())
    if not any_bool and vals and vals <= {0.0, 100.0, 200.0} and (vals - {0.0}):
        scale, sdk = 100.0, True
    elif not any_bool and vals <= {0.0, 1.0, 2.0} and 2.0 in vals:
        scale, sdk = 1.0, True
    else:
        scale, sdk = 1.0, False
    out: dict[str, np.ndarray] = {}
    for name, a in arrays.items():
        f = np.nan_to_num(a.astype(np.float32), nan=0.0)
        if sdk:
            m8 = np.clip(np.round(f / scale), 0, 2).astype(np.uint8)
        else:
            m8 = np.where(f > 0, CONFIDENT, LAND).astype(np.uint8)
        h, w = m8.shape
        s = _w.MASK_PX / max(h, w)
        if s < 1:
            m8 = cv2.resize(m8, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_NEAREST)
        out[name] = m8
    return out


class _CamView:
    """CameraPose를 내부 수면 평면 계산(ground_water)이 쓰는 형태로 감쌈."""

    def __init__(self, cam, z0: float, full: tuple[float, float, float, float]):
        self.name = cam.name
        self._cam = cam
        self.camera = cam  # width, height
        W, H = cam.width, cam.height
        uv = np.array([[0, 0], [W / 2, 0], [W, 0], [W, H / 2], [W, H], [W / 2, H], [0, H], [0, H / 2]], float)
        d = cam.rays(uv)
        if (d[:, 2] < -1e-6).all():
            t = (z0 - cam.center[2]) / d[:, 2]
            p = cam.center[None, :2] + t[:, None] * d[:, :2]
            self.bbox = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())
        else:  # 수평선이 보이는 경사 영상: DSM 전체
            self.bbox = full

    def project_world(self, world: np.ndarray):
        uv = self._cam.project(world)
        ok = np.isfinite(uv).all(axis=1)
        inside = ok & self._cam.in_image(np.nan_to_num(uv, nan=-1.0))
        u = np.where(ok, uv[:, 0], -1.0)
        v = np.where(ok, uv[:, 1], -1.0)
        return u, v, inside, None


def flatten_dsm(dsm: np.ndarray, transform, cameras, masks: Mapping[str, np.ndarray], points, *,
                clean_points: bool = True, **params: Any):
    """DSM의 수면 칸을 물가 높이의 평면으로 덮음 (정사 처리의 수면 평면과 같은 계산).

    Args:
        dsm: DSM 높이 ``(rows, cols)`` (:func:`quickortho.read_dsm`). 북쪽이 위, 정사각 화소.
        transform: DSM의 ``affine.Affine``.
        cameras: 정합된 카메라 (:meth:`quickortho.Project.cameras`, DSM과 같은 좌표계).
        masks: 영상 이름 → 수면 마스크 (:func:`detect`, :func:`read_masks` 또는 자기 마스크. 0이 아니면 수면).
            사진과 해상도가 달라도 됨 (비율로 맞춤).
        points: 희소 3D 점 ``(N, 3)`` 또는 :class:`~quickortho.PointCloud` (같은 좌표계).
        clean_points: 정사 처리처럼 높이가 튀는 점을 먼저 뺌 (기본 ``True``).
        params: 확정 기준 (``vote_min``, ``empty_ratio``, ``ring_m``, ``level_q``, ``raised_max``, ``drop_max``).

    Returns:
        ``(dsm2, water_mask, level, stats)``: 덮은 DSM, 수면 칸 bool, 칸별 수면 높이(수면 아닌 칸 NaN),
        통계(보고서 ``water``와 같은 키).
    """
    from ._core.surface import Dsm

    z = np.asarray(dsm, dtype=np.float32)
    if z.ndim != 2:
        raise InputError("dsm은 2차원 배열이어야 함", "invalid_argument")
    if not np.isfinite(z).any():
        raise InputError("dsm에 값이 있는 칸이 없음", "invalid_argument")
    res = float(transform.a)
    if abs(abs(transform.e) - res) > 1e-6 * max(1.0, res) or transform.b or transform.d or transform.e >= 0:
        raise InputError("transform은 북쪽이 위인 정사각 화소여야 함", "invalid_argument")
    pts = np.asarray(getattr(points, "xyz", points), dtype=np.float64).reshape(-1, 3)
    if clean_points and len(pts):
        from ._core.surface import remove_z_outliers

        pts = remove_z_outliers(pts)
    m = normalize_masks(masks)
    x0, y0 = transform.c + res / 2, transform.f - res / 2  # 내부 격자는 칸 중심 기준
    d = Dsm(z=np.nan_to_num(z, nan=float(np.nanmedian(z))), x0=x0, y0=y0, res=res, num_points=len(pts))
    full = (transform.c, transform.f - res * z.shape[0], transform.c + res * z.shape[1], transform.f)
    z0 = float(np.nanmedian(z))
    views = [_CamView(c, z0, full) for c in cameras if c.name in m]
    water, level, stats = _w.ground_water(views, d, m, pts, **params)
    out = _w.flatten(d, water, level).z
    out[np.isnan(z) & ~water] = np.nan
    return out, water, level, stats


__all__ = [
    "MODES",
    "LAND",
    "CANDIDATE",
    "CONFIDENT",
    "AUTO_MIN_FRACTION",
    "MASK_PX",
    "WaterDetection",
    "normalize_mode",
    "classify",
    "detect",
    "read_masks",
    "normalize_masks",
    "flatten_dsm",
]
