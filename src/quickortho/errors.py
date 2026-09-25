"""QuickOrtho SDK 예외.

모든 예외는 :class:`QuickOrthoError`를 상속하며, 기계가 읽을 수 있는 ``code`` 문자열을 가짐.
서버에서는 ``code``로 HTTP 상태 코드나 오류 메시지를 고르면 됨.

예외 계층::

    QuickOrthoError
    ├── InputError        입력 영상·파일이 처리 조건을 만족하지 않음
    ├── AlignmentError    SfM·좌표 정렬 실패
    ├── ProcessingError   DSM·정사투영 등 처리 중 실패
    ├── ProjectError      워크스페이스(프로젝트) 상태가 요청한 작업과 맞지 않음
    └── Cancelled         CancelToken으로 작업이 중단됨
"""

from __future__ import annotations


class QuickOrthoError(Exception):
    """SDK 예외의 기반 클래스.

    Attributes:
        code: 오류 종류를 나타내는 고정 문자열 (예: ``"too_few_images"``). 버전이 바뀌어도 유지함.
        message: 사람이 읽는 설명 (한국어).
    """

    code: str = "error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class InputError(QuickOrthoError):
    """입력이 처리 조건을 만족하지 않음.

    code 값: ``folder_not_found``, ``no_images``, ``too_few_images``, ``too_few_gps``, ``no_gps``,
    ``image_dir_missing``, ``empty_file``, ``invalid_edits``, ``invalid_argument``
    """

    code = "invalid_input"


class AlignmentError(QuickOrthoError):
    """SfM(카메라 위치·자세 추정) 또는 GPS 좌표 정렬 실패.

    code 값: ``sfm_failed``, ``georef_too_few``, ``georef_mismatch``
    """

    code = "alignment_failed"


class ProcessingError(QuickOrthoError):
    """DSM 생성·정사투영 등 처리 중 실패.

    code 값: ``dsm_failed``
    """

    code = "processing_failed"


class ProjectError(QuickOrthoError):
    """워크스페이스 상태가 요청한 작업과 맞지 않음.

    code 값: ``not_aligned`` (정렬 결과가 없음), ``not_rendered`` (정사 모자이크·DSM이 없음)
    """

    code = "project_error"


class Cancelled(QuickOrthoError):
    """:class:`~quickortho.CancelToken`으로 작업이 중단됨."""

    code = "cancelled"

    def __init__(self, message: str = "작업이 중단됨") -> None:
        super().__init__(message)


__all__ = [
    "QuickOrthoError",
    "InputError",
    "AlignmentError",
    "ProcessingError",
    "ProjectError",
    "Cancelled",
]
