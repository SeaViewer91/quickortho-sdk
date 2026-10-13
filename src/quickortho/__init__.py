"""QuickOrtho SDK: 드론 영상으로 정사 모자이크(GeoTIFF)를 만드는 Python SDK.

빠른 시작::

    import quickortho as qo

    project = qo.Project.create("DJI_images/", "workspace/")
    result = project.process(on_event=qo.print_progress)
    print(result.orthomosaic, result.gsd_m)

자세한 사용법은 https://github.com/SeaViewer91/quickortho-sdk/tree/main/docs 참고.

공개 API는 이 모듈에서 가져오는 이름뿐임. ``quickortho._core`` 아래는 내부 구현이며 예고 없이 바뀔 수 있음.
"""

from ._version import __version__

# rasterio를 불러오기 전에 PROJ·GDAL 데이터 경로를 패키지 내장 데이터로 고정한다 (_geoenv.py 참고)
from . import _geoenv

_geoenv.isolate()

from .errors import (  # noqa: E402
    AlignmentError,
    Cancelled,
    InputError,
    ProcessingError,
    ProjectError,
    QuickOrthoError,
)
from .doctor import run_doctor as doctor
from .events import STAGES, CancelToken, Event, EventCallback, print_progress
from .gcp import GCP, Mark, TiePoint, load_gcps, read_gcp_file
from .geodata import (
    CameraPose,
    Cameras,
    PointCloud,
    band_names,
    read_dsm,
    read_indices,
    read_orthomosaic,
    read_raster,
    rotation_from_opk,
)
from .options import OrthoOptions, PreviewOptions, SfmOptions
from .project import Project, preview, process, scan
from .results import (
    AlignResult,
    MultispectralInfo,
    OrthoResult,
    PreviewResult,
    RefineResult,
    ScanResult,
    WaterInfo,
)
from . import multispectral  # noqa: E402  (qo.multispectral: 방사보정만 쓰는 경우)
from . import indices  # noqa: E402  (qo.indices: 식생지수 목록·직접 계산)
from . import water  # noqa: E402  (qo.water: 수면 판별·마스크·DSM 수면 평면)

__all__ = [
    "__version__",
    # 처리
    "Project",
    "scan",
    "preview",
    "process",
    # 옵션
    "OrthoOptions",
    "SfmOptions",
    "PreviewOptions",
    # 산출물 데이터
    "CameraPose",
    "Cameras",
    "PointCloud",
    "read_orthomosaic",
    "read_dsm",
    "read_raster",
    "read_indices",
    "band_names",
    "rotation_from_opk",
    # 결과
    "ScanResult",
    "PreviewResult",
    "AlignResult",
    "OrthoResult",
    "RefineResult",
    "MultispectralInfo",
    "WaterInfo",
    # 다중분광 (모듈)
    "multispectral",
    "indices",
    "water",
    # 진행·중단
    "Event",
    "EventCallback",
    "CancelToken",
    "STAGES",
    "print_progress",
    # GCP
    "GCP",
    "Mark",
    "TiePoint",
    "load_gcps",
    "read_gcp_file",
    # 진단
    "doctor",
    # 예외
    "QuickOrthoError",
    "InputError",
    "AlignmentError",
    "ProcessingError",
    "ProjectError",
    "Cancelled",
]
