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
from .errors import (
    AlignmentError,
    Cancelled,
    InputError,
    ProcessingError,
    ProjectError,
    QuickOrthoError,
)
from .events import STAGES, CancelToken, Event, EventCallback, print_progress
from .gcp import GCP, Mark, TiePoint, load_gcps, read_gcp_file
from .options import OrthoOptions, PreviewOptions, SfmOptions
from .project import Project, preview, process, scan
from .results import AlignResult, OrthoResult, PreviewResult, RefineResult, ScanResult

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
    # 결과
    "ScanResult",
    "PreviewResult",
    "AlignResult",
    "OrthoResult",
    "RefineResult",
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
    # 예외
    "QuickOrthoError",
    "InputError",
    "AlignmentError",
    "ProcessingError",
    "ProjectError",
    "Cancelled",
]
