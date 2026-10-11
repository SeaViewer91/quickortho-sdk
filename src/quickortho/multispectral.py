"""다중분광 카메라(MicaSense RedEdge·Altum 계열) 메타데이터와 방사보정.

정사 모자이크는 :class:`quickortho.Project`가 폴더를 보고 자동으로 다중분광 처리를 하므로 이 모듈이 필요 없음.
정사 모자이크 없이 **밴드 영상 한 장의 방사보정만** 쓰고 싶을 때(예: 지상 분광 비교, 자체 처리 파이프라인) 씀.

예::

    from quickortho import multispectral as ms

    ds = ms.scan("flight_0925/")               # 밴드 영상 묶기 (캡처·패널 구분)
    cap = ds.flight_captures[0]
    b = ds.captures[cap][ds.out_indices[0]]    # 첫 출력 밴드
    L = ms.radiance(b, ms.read_raw(ds.root / b.file))      # 방사휘도 (W/m²/sr/nm)
    factors, info = ms.reflectance_factors(ds, "auto")     # 캡처·밴드별 반사율 환산 계수
    rho = L * factors[cap][ds.out_indices[0]]              # 반사율

방사보정식과 반사율 환산 방식은 문서 ``docs/concepts.md``의 "다중분광 처리" 절 참고.
이 모듈은 0.4.0에서 새로 공개한 것으로, 1.0 전까지 이름·반환 형식이 바뀔 수 있음.
"""

from __future__ import annotations

from pathlib import Path

from ._core.multispectral import (
    BandImage,
    MsDataset,
    panel_stats,
    radiance,
    read_band,
    read_raw,
    reflectance_factors,
    smooth_irradiance,
    vignette,
)
from ._core.multispectral import scan_multispectral as _scan


def scan(folder: str | Path) -> MsDataset | None:
    """폴더의 다중분광 밴드 TIFF를 읽어 캡처별로 묶음. 다중분광 영상이 없으면 ``None``.

    밴드 영상은 Pix4D 카메라 XMP(``Camera:BandName``, ``Camera:RigCameraIndex``)가 있는 TIFF만 받음.
    """
    return _scan(Path(folder).expanduser())


__all__ = [
    "scan",
    "MsDataset",
    "BandImage",
    "read_band",
    "read_raw",
    "vignette",
    "radiance",
    "panel_stats",
    "smooth_irradiance",
    "reflectance_factors",
]
