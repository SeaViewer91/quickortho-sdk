"""처리 옵션.

모든 옵션은 기본값만으로 동작함. 기본값의 설계 기준은 맥북 에어 M1 8GB(CPU 전용)이며,
서버처럼 여유가 있는 환경에서는 :class:`SfmOptions`의 값을 키워 품질을 높일 수 있음.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SfmOptions:
    """SfM(특징점 추출·매칭·카메라 위치 추정) 옵션.

    Attributes:
        max_image_size: 특징점 추출 전에 영상 긴 변을 이 크기(px)로 줄임. 클수록 정밀하지만 느리고 메모리를 많이 씀.
        max_num_features: 영상당 최대 특징점 수.
        num_threads: 사용할 스레드 수. ``-1``이면 모든 코어를 씀.
        num_neighbors: GPS 기반 공간 매칭에서 영상마다 매칭할 가까운 영상 수.
        max_neighbor_distance_m: 공간 매칭 이웃의 최대 거리(m).
        exhaustive_below: 영상 수가 이 값 이하이거나 GPS가 없으면 모든 쌍을 매칭(전수 매칭)함.
        min_registered_ratio: 전역 SfM의 정합률이 이 값보다 낮으면 증분 SfM으로 다시 시도함.
    """

    max_image_size: int = 2000
    max_num_features: int = 4096
    num_threads: int = -1
    num_neighbors: int = 15
    max_neighbor_distance_m: float = 500.0
    exhaustive_below: int = 40
    min_registered_ratio: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrthoOptions:
    """정렬(SfM)과 정사 모자이크 생성 옵션.

    Attributes:
        gsd_m: 출력 GSD(m/화소). 지정하면 ``gsd_scale``보다 우선함.
        gsd_scale: ``gsd_m``이 없을 때 원본 GSD에 곱할 배율. 기본값 2는 원본보다 2배 거친 해상도임.
        keep_work: 중간 산출물(COLMAP DB, 희소 재구성 원본)을 ``<워크스페이스>/work``에 남김.
        cache_budget_mb: 정사투영 중 축소 영상 캐시에 쓸 메모리 상한(MB).
        sfm: SfM 옵션.
    """

    gsd_m: float | None = None
    gsd_scale: float = 2.0
    keep_work: bool = False
    cache_budget_mb: int = 600
    sfm: SfmOptions = field(default_factory=SfmOptions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreviewOptions:
    """빠른 미리보기 옵션.

    Attributes:
        max_size: 결과 PNG(간이 모자이크·중복도 지도)의 긴 변(px).
        quicklook: ``False``이면 영상을 디코딩하지 않고 촬영 범위·중복도·누락 구역만 계산함 (1초 내외).
    """

    max_size: int = 2048
    quicklook: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["SfmOptions", "OrthoOptions", "PreviewOptions"]
