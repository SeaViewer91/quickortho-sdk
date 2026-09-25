"""워크스페이스 단위 처리 API.

:class:`Project` 하나는 영상 폴더 하나와 그 처리 결과를 담는 워크스페이스 폴더 하나에 대응함.
처리 단계는 Pix4D의 단계 구분과 비슷하게 나뉨.

1. :meth:`Project.align` — 스캔 → SfM → GPS 좌표 정렬 (가장 오래 걸림)
2. :meth:`Project.orthomosaic` — 간이 DSM → 정사투영 → COG (GSD 등을 바꿔 여러 번 실행 가능)
3. :meth:`Project.refine` — GCP·타이포인트로 번들 조정 후 정사 모자이크 재생성 (선택)

:meth:`Project.process`는 1과 2를 한 번에 실행함.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from ._core import marking
from ._core.pipeline import run_align, run_render
from ._core.preview import run_preview
from ._core.project import Project as _Store
from ._core.refine import run_refine
from ._core.scan import scan_folder
from .errors import InputError, ProjectError
from .events import CancelToken, EventCallback, make_emitter
from .gcp import GCP, Mark, TiePoint
from .options import OrthoOptions, PreviewOptions
from .results import AlignResult, OrthoResult, PreviewResult, RefineResult, ScanResult

_UNSET: Any = object()


def _start(on_event: EventCallback | None, cancel: CancelToken | None):
    if cancel is not None:
        cancel.raise_if_cancelled()
    return make_emitter(on_event, cancel)


def _images_dir(images: str | Path) -> Path:
    p = Path(images).expanduser().resolve()
    if not p.is_dir():
        raise InputError(f"영상 폴더를 찾을 수 없음: {p}", "folder_not_found")
    return p


# ───────────────────────── 폴더 단위 함수 ─────────────────────────


def scan(
    folder: str | Path,
    *,
    recursive: bool = False,
    on_event: EventCallback | None = None,
    cancel: CancelToken | None = None,
) -> ScanResult:
    """영상 폴더의 EXIF·DJI XMP를 읽어 위치·고도·짐벌 자세·카메라 정보를 수집함.

    카메라별로 묶은 뒤 매핑용(광각) 카메라를 자동 선별함. 영상 헤더만 읽으므로 수백 장도 1초 내외임.

    Args:
        folder: 영상 폴더.
        recursive: 하위 폴더까지 읽을지 여부.
        on_event: 진행 이벤트 콜백.
        cancel: 중단 토큰.

    Raises:
        InputError: 폴더가 없음 (``folder_not_found``).
        Cancelled: 중단됨.
    """
    out = _start(on_event, cancel)
    return ScanResult.from_dict(scan_folder(_images_dir(folder), recursive=recursive, emitter=out))


def preview(
    folder: str | Path,
    output: str | Path,
    options: PreviewOptions | None = None,
    *,
    on_event: EventCallback | None = None,
    cancel: CancelToken | None = None,
) -> PreviewResult:
    """SfM 없이 EXIF·XMP만으로 촬영 범위·중복도·누락 구역·간이 모자이크를 만듦 (수 초).

    지면을 이륙 지점 높이의 평면으로 가정하므로 위치 오차가 수 m 수준임. 현장에서 촬영 누락을 확인하는 용도임.

    Args:
        folder: 영상 폴더.
        output: 결과 폴더 (없으면 만듦). ``quicklook.png``, ``coverage.png``, ``preview.geojson``, ``preview.json``을 씀.
        options: 미리보기 옵션.
        on_event: 진행 이벤트 콜백.
        cancel: 중단 토큰.

    Raises:
        InputError: 폴더가 없음(``folder_not_found``), GPS가 있는 영상이 없음(``no_gps``).
        Cancelled: 중단됨.
    """
    opts = options or PreviewOptions()
    out = _start(on_event, cancel)
    output = Path(output).expanduser().resolve()
    res = run_preview(_images_dir(folder), output, out, max_size=opts.max_size, quicklook=opts.quicklook)
    return PreviewResult.from_dict(res, output)


def process(
    images: str | Path,
    workspace: str | Path,
    options: OrthoOptions | None = None,
    *,
    on_event: EventCallback | None = None,
    cancel: CancelToken | None = None,
) -> OrthoResult:
    """영상 폴더로 정사 모자이크를 한 번에 만듦. ``Project.create(images, workspace).process(...)``와 같음."""
    if cancel is not None:
        cancel.raise_if_cancelled()
    return Project.create(images, workspace).process(options, on_event=on_event, cancel=cancel)


# ───────────────────────── 워크스페이스 ─────────────────────────


class Project:
    """영상 폴더 하나와 처리 결과 워크스페이스 하나.

    워크스페이스 구조::

        <workspace>/
        ├── orthomosaic.tif   정사 모자이크 (COG, RGBA)
        ├── dsm.tif           간이 DSM (float32)
        ├── preview.png       미리보기 PNG
        ├── report.json       처리 보고서
        ├── preview/          빠른 미리보기 결과 (preview() 실행 시)
        └── project/          정렬 결과·보정 데이터 (직접 수정하지 않음)

    생성은 :meth:`create`, 기존 워크스페이스 열기는 :meth:`open`을 씀.
    같은 워크스페이스를 여러 스레드·프로세스에서 동시에 처리하는 것은 지원하지 않음.
    """

    def __init__(self, workspace: str | Path, images: str | Path | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._images = Path(images).expanduser().resolve() if images is not None else None
        self._store = _Store(self.workspace)

    # ── 생성·열기 ──
    @classmethod
    def create(cls, images: str | Path, workspace: str | Path) -> "Project":
        """새 워크스페이스를 준비함. 폴더가 이미 있으면 그대로 씀 (다음 align에서 이전 결과를 덮어씀).

        Raises:
            InputError: 영상 폴더가 없음 (``folder_not_found``).
        """
        img = _images_dir(images)
        ws = Path(workspace).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return cls(ws, img)

    @classmethod
    def open(cls, workspace: str | Path) -> "Project":
        """정렬을 마친 기존 워크스페이스를 엶. QuickOrtho 데스크톱 앱(v0.2.0)이 만든 ``ortho/`` 폴더도 열 수 있음.

        Raises:
            ProjectError: 정렬 결과가 없음 (``not_aligned``).
        """
        p = cls(workspace)
        p._store.require()
        return p

    # ── 상태 ──
    @property
    def images(self) -> Path:
        """영상 폴더."""
        if self._images is not None:
            return self._images
        if self._store.exists():
            return self._store.image_dir
        raise ProjectError("영상 폴더를 알 수 없음. Project.create()로 만들어야 함", "not_aligned")

    @property
    def is_aligned(self) -> bool:
        """정렬(SfM) 결과가 있는지 여부."""
        return self._store.exists()

    @property
    def is_rendered(self) -> bool:
        """정사 모자이크가 있는지 여부."""
        return (self.workspace / "orthomosaic.tif").exists() and "outputs" in self.report()

    @property
    def is_refined(self) -> bool:
        """정밀 보정 결과가 있는지 여부."""
        return self._store.exists() and self._store.has_refined()

    def report(self) -> dict[str, Any]:
        """현재 ``report.json`` 내용. 없으면 빈 dict."""
        import json

        p = self.workspace / "report.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def result(self) -> OrthoResult | None:
        """마지막 정사 모자이크 결과. 없으면 ``None``."""
        return OrthoResult.from_report(self.report(), self.workspace) if self.is_rendered else None

    def __repr__(self) -> str:
        state = "refined" if self.is_refined else "rendered" if self.is_rendered else "aligned" if self.is_aligned else "new"
        return f"Project(workspace={str(self.workspace)!r}, state={state!r})"

    # ── 처리 ──
    def scan(self, *, recursive: bool = False, on_event: EventCallback | None = None,
             cancel: CancelToken | None = None) -> ScanResult:
        """영상 폴더 스캔. :func:`quickortho.scan`과 같음."""
        return scan(self.images, recursive=recursive, on_event=on_event, cancel=cancel)

    def preview(self, options: PreviewOptions | None = None, *, output: str | Path | None = None,
                on_event: EventCallback | None = None, cancel: CancelToken | None = None) -> PreviewResult:
        """빠른 미리보기. 결과는 기본으로 ``<workspace>/preview/``에 씀. :func:`quickortho.preview` 참고."""
        return preview(self.images, output or self.workspace / "preview", options, on_event=on_event, cancel=cancel)

    def align(self, options: OrthoOptions | None = None, *, on_event: EventCallback | None = None,
              cancel: CancelToken | None = None) -> AlignResult:
        """스캔 → SfM → GPS 좌표 정렬을 실행하고 결과를 ``project/``에 저장함.

        이전 정렬·보정 결과와 정사 모자이크 결과물은 새 정렬과 맞지 않으므로 지움. GCP·타이포인트의 사진 표시는 유지함.
        ``options``에서는 ``sfm``과 ``keep_work``만 씀.

        Raises:
            InputError: 영상이 없음(``no_images``), 처리 가능한 영상이 3장 미만(``too_few_images``),
                GPS 있는 영상이 3장 미만(``too_few_gps``).
            AlignmentError: SfM 실패(``sfm_failed``), 좌표 정렬 실패(``georef_too_few``, ``georef_mismatch``).
            Cancelled: 중단됨.
        """
        opts = options or OrthoOptions()
        out = _start(on_event, cancel)
        report, _, _ = run_align(self.images, self.workspace, opts, out)
        return AlignResult.from_dict(report)

    def orthomosaic(self, options: OrthoOptions | None = None, *, on_event: EventCallback | None = None,
                    cancel: CancelToken | None = None) -> OrthoResult:
        """현재 정렬 결과(보정했으면 보정 결과)로 간이 DSM과 정사 모자이크를 만듦.

        SfM을 다시 하지 않으므로 GSD만 바꿔 다시 만들 때 빠름. ``options``에서는 ``gsd_m``, ``gsd_scale``,
        ``cache_budget_mb``를 씀. 여기서 쓴 옵션은 저장되어 이후 :meth:`refine`의 재생성에도 쓰임.

        Raises:
            ProjectError: 정렬 결과가 없음 (``not_aligned``).
            InputError: 원본 영상 폴더가 없어짐 (``image_dir_missing``).
            ProcessingError: DSM 생성 실패 (``dsm_failed``).
            Cancelled: 중단됨.
        """
        opts = options or OrthoOptions()
        out = _start(on_event, cancel)
        report = run_render(self.workspace, opts, out)
        return OrthoResult.from_report(report, self.workspace)

    def process(self, options: OrthoOptions | None = None, *, on_event: EventCallback | None = None,
                cancel: CancelToken | None = None) -> OrthoResult:
        """:meth:`align` 후 :meth:`orthomosaic`을 실행함. 발생하는 예외는 두 메서드의 예외와 같음."""
        opts = options or OrthoOptions()
        out = _start(on_event, cancel)
        _, rec, epsg = run_align(self.images, self.workspace, opts, out)
        report = run_render(self.workspace, opts, out, rec_abs=rec, epsg=epsg)
        return OrthoResult.from_report(report, self.workspace)

    # ── 정밀 보정 ──
    def get_edits(self) -> dict[str, Any]:
        """저장된 보정 수정 사항 (``project/edits.json``). 형식은 문서 ``docs/refinement.md`` 참고."""
        self._store.require()
        return self._store.load_edits()

    @property
    def gcps(self) -> list[GCP]:
        """저장된 GCP 목록."""
        return [GCP.from_dict(g) for g in self.get_edits()["gcps"]]

    @property
    def tiepoints(self) -> list[TiePoint]:
        """저장된 수동 타이포인트 목록."""
        return [TiePoint.from_dict(t) for t in self.get_edits()["tiepoints"]]

    def set_edits(
        self,
        *,
        gcps: Iterable[GCP | dict] | None = _UNSET,
        tiepoints: Iterable[TiePoint | dict] | None = _UNSET,
        max_reproj_error_px: float | None = _UNSET,
        deleted_points: Iterable[int] | None = _UNSET,
    ) -> dict[str, Any]:
        """보정 수정 사항을 바꿔 저장함. 지정한 항목만 바꾸고 나머지는 유지함.

        Args:
            gcps: GCP 목록 (전체를 바꿈). ``None``이면 비움.
            tiepoints: 수동 타이포인트 목록 (전체를 바꿈). ``None``이면 비움.
            max_reproj_error_px: 이 값(px)보다 재투영 오차가 큰 관측을 보정 때 자동 제거함. ``None``이면 사용 안 함.
            deleted_points: 보정 때 지울 3D 점 ID 목록 (:meth:`tiepoint_stats`의 ``worst[].id``). ``None``이면 비움.

        Returns:
            검증·정리되어 저장된 수정 사항 dict.

        Raises:
            ProjectError: 정렬 결과가 없음.
            InputError: 값 형식이 잘못됨 (``invalid_edits``).
        """
        edits = self.get_edits()
        if gcps is not _UNSET:
            edits["gcps"] = [g.to_dict() if isinstance(g, GCP) else dict(g) for g in (gcps or [])]
        if tiepoints is not _UNSET:
            edits["tiepoints"] = [t.to_dict() if isinstance(t, TiePoint) else dict(t) for t in (tiepoints or [])]
        if max_reproj_error_px is not _UNSET:
            edits["max_reproj_error_px"] = max_reproj_error_px
        if deleted_points is not _UNSET:
            edits["deleted_points"] = list(deleted_points or [])
        try:
            return marking.save_edits(self.workspace, edits)
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"보정 수정 사항 형식이 잘못됨: {exc}", "invalid_edits") from exc

    def refine(self, *, on_event: EventCallback | None = None, cancel: CancelToken | None = None) -> RefineResult:
        """저장된 수정 사항으로 번들 조정을 하고 DSM·정사 모자이크를 다시 만듦.

        항상 최초 정렬 결과(base)에서 시작해 수정 사항을 처음부터 적용하므로, 여러 번 실행해도 결과가 누적되지 않음.
        좌표 기준은 기준점 수에 따라 정해짐: 3점 이상 ``"gcp"``, 1~2점 ``"gcp_shift"``, 0점 ``"gps"``.

        Raises:
            ProjectError: 정렬 결과가 없음.
            InputError: 원본 영상 폴더가 없어짐 (``image_dir_missing``).
            Cancelled: 중단됨.
        """
        out = _start(on_event, cancel)
        report = run_refine(self.workspace, out)
        return RefineResult.from_report(report, self.workspace)

    def reset_refinement(self, *, on_event: EventCallback | None = None,
                         cancel: CancelToken | None = None) -> OrthoResult:
        """보정 결과를 지우고 최초 정렬 결과로 정사 모자이크를 다시 만듦. 수정 사항(edits)은 남겨 둠."""
        out = _start(on_event, cancel)
        report = run_refine(self.workspace, out, reset=True)
        return OrthoResult.from_report(report, self.workspace)

    def info(self) -> dict[str, Any]:
        """보정 화면용 프로젝트 정보: 영상 목록(정합 여부·크기), 현재 좌표계, 수정 사항, GCP 경위도, 좌표계 목록."""
        self._store.require()
        return marking.project_info(self.workspace)

    def tiepoint_stats(self, top: int = 300) -> dict[str, Any]:
        """타이포인트 오차 통계: 요약, 오차 분포, 자동 제거 기준별 제거량 미리보기, 권장 기준, 오차 큰 점, 영상별 오차."""
        return marking.tiepoint_stats(self.workspace, top=top)

    def predict(
        self,
        marks: Iterable[Mark | dict] = (),
        world: Optional[dict[str, Any]] = None,
        *,
        chips: bool = False,
    ) -> dict[str, Any]:
        """표시한 점 또는 측량 좌표로 3D 위치를 구하고, 그 점이 보이는 사진과 사진 좌표를 예측함.

        GCP를 사진에 표시할 때 후보 사진을 고르는 데 씀.

        Args:
            marks: 이미 표시한 사진 좌표. 2장 이상이면 삼각측량, 1장이면 DSM과 교차해 3D 위치를 구함.
            world: 측량 좌표 ``{"x", "y", "z", "epsg", "z_from_dsm"(선택)}``. ``marks``로 위치를 못 구할 때 씀.
            chips: ``True``면 후보 사진마다 원본 해상도 512px 조각 이미지를 ``project/cache/``에 만들고 경로를 넣음.

        Returns:
            ``method``(``"triangulated"``·``"survey"``·``"survey_dsm"``·``"dsm"``·``None``), ``point``(좌표),
            ``residuals_px``, ``candidates``(사진·x·y, 중심에 가까운 순) dict.
            위치를 구하지 못하면 ``{"method": None, "candidates": []}``만 돌려줌.

        Raises:
            ProjectError: 정렬 결과가 없음(``not_aligned``), DSM이 없음(``not_rendered``, 정렬만 하고 정사 모자이크를 만들지 않음).
        """
        self._store.require()
        if not (self.workspace / "dsm.tif").exists():
            raise ProjectError(
                "DSM(dsm.tif)이 없음. orthomosaic() 또는 process()를 먼저 실행해야 함", "not_rendered"
            )
        spec = {"marks": [m.to_dict() if isinstance(m, Mark) else dict(m) for m in marks], "world": world,
                "chips": chips}
        return marking.predict(self.workspace, spec)


__all__ = ["Project", "scan", "preview", "process"]
