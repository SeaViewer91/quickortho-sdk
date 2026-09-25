"""처리 결과 객체.

모든 결과 객체는 자주 쓰는 값을 속성으로 제공하고, 원본 dict 전체를 ``raw``로 함께 가짐.
``raw``의 구조는 문서 ``docs/report.md``에 정리되어 있으며, JSON으로 그대로 저장·전송할 수 있음.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ScanResult:
    """영상 폴더 스캔 결과 (:func:`quickortho.scan`).

    Attributes:
        folder: 스캔한 폴더.
        images: 영상별 정보 목록. 각 항목은 dict이며 주요 키는 ``file``, ``width``, ``height``, ``make``, ``model``,
            ``lat``, ``lon``, ``abs_alt``, ``rel_alt``, ``gimbal_yaw``, ``gimbal_pitch``, ``selected``, ``flags``.
        cameras: 카메라(기종·초점거리·해상도 조합)별 요약. 키는 카메라 식별 문자열.
        summary: 전체 요약 (``total_files``, ``selected``, ``no_gps``, ``oblique``, ``rolling_shutter_warning`` 등).
        excluded_cameras: 자동 제외한 카메라와 사유 (예: 망원 카메라).
        failed: 읽기 실패한 파일과 오류 메시지.
        raw: 원본 dict.
    """

    folder: Path
    images: list[dict[str, Any]]
    cameras: dict[str, dict[str, Any]]
    summary: dict[str, Any]
    excluded_cameras: dict[str, str]
    failed: list[dict[str, str]]
    raw: dict[str, Any] = field(repr=False)

    @property
    def selected_images(self) -> list[dict[str, Any]]:
        """처리에 쓸 영상(매핑용 카메라로 선별된 영상)만."""
        return [im for im in self.images if im.get("selected")]

    @property
    def num_with_gps(self) -> int:
        """선별된 영상 중 GPS 정보가 있는 영상 수."""
        return sum(1 for im in self.selected_images if im.get("lat") is not None and im.get("lon") is not None)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ScanResult":
        return ScanResult(
            folder=Path(d["folder"]),
            images=d["images"],
            cameras=d["cameras"],
            summary=d["summary"],
            excluded_cameras=d.get("excluded_cameras", {}),
            failed=d.get("failed", []),
            raw=d,
        )


@dataclass(frozen=True)
class PreviewResult:
    """빠른 미리보기 결과 (:func:`quickortho.preview`).

    Attributes:
        output_dir: 결과 폴더.
        quicklook: 간이 모자이크 PNG 경로. ``quicklook=False``로 실행했으면 ``None``.
        coverage: 중복도 지도 PNG 경로.
        geojson: 촬영 범위·누락 구역·저중복 구역·촬영 위치 GeoJSON 경로 (WGS84).
        corners_lonlat: 두 PNG의 네 모서리 경위도 [좌상, 우상, 우하, 좌하]. 지도에 겹쳐 그릴 때 사용.
        num_images: 미리보기에 쓴 영상 수.
        num_gaps: 누락 구역 수.
        coverage_stats: 조사 면적, 누락 면적, 저중복 면적, 중복도 중앙값 등.
        warnings: 경고 문장 목록.
        raw: 원본 dict (``preview.json``과 같음).
    """

    output_dir: Path
    quicklook: Optional[Path]
    coverage: Path
    geojson: Path
    corners_lonlat: list[list[float]]
    num_images: int
    num_gaps: int
    coverage_stats: dict[str, float]
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)

    @staticmethod
    def from_dict(d: dict[str, Any], output_dir: Path) -> "PreviewResult":
        o = d["outputs"]
        return PreviewResult(
            output_dir=Path(output_dir),
            quicklook=Path(o["quicklook"]) if o.get("quicklook") else None,
            coverage=Path(o["coverage"]),
            geojson=Path(o["geojson"]),
            corners_lonlat=d["corners_lonlat"],
            num_images=d["num_images"],
            num_gaps=d["gaps"],
            coverage_stats=d["coverage"],
            warnings=d.get("warnings", []),
            raw=d,
        )


@dataclass(frozen=True)
class AlignResult:
    """정렬(SfM + GPS 좌표 정렬) 결과 (:meth:`quickortho.Project.align`).

    Attributes:
        num_images: SfM에 넣은 영상 수.
        num_registered: 위치·자세가 추정된(정합된) 영상 수.
        num_points: 희소 3D 점 수.
        reprojection_error_px: 평균 재투영 오차(px).
        epsg: 결과 좌표계 EPSG (UTM).
        gps_residual_m: 정렬 후 카메라 위치와 GPS의 수평 RMS 차이(m).
        warnings: 경고 문장 목록.
        raw: 원본 dict (``project/align_report.json``과 같음).
    """

    num_images: int
    num_registered: int
    num_points: int
    reprojection_error_px: float
    epsg: int
    gps_residual_m: float
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AlignResult":
        sfm, geo = d["sfm"], d["georef"]
        return AlignResult(
            num_images=int(sfm["num_input_images"]),
            num_registered=int(sfm["num_registered"]),
            num_points=int(sfm["num_points3D"]),
            reprojection_error_px=float(sfm["mean_reprojection_error_px"]),
            epsg=int(geo["epsg"]),
            gps_residual_m=float(geo["gps_residual_rms_m"]),
            warnings=list(d.get("warnings", [])),
            raw=d,
        )


@dataclass(frozen=True)
class OrthoResult:
    """정사 모자이크 결과 (:meth:`quickortho.Project.orthomosaic`, :meth:`~quickortho.Project.process`,
    :meth:`~quickortho.Project.refine`).

    Attributes:
        orthomosaic: 정사 모자이크 GeoTIFF(COG, RGBA) 경로.
        dsm: 간이 DSM GeoTIFF(float32) 경로.
        preview: 긴 변 2048px 미리보기 PNG 경로.
        report_path: ``report.json`` 경로.
        epsg: 결과 좌표계 EPSG. GCP 보정을 했으면 GCP 좌표계, 아니면 UTM.
        gsd_m: 결과 GSD(m/화소).
        width: 결과 너비(화소).
        height: 결과 높이(화소).
        corners_lonlat: 결과의 네 모서리 경위도 [좌상, 우상, 우하, 좌하].
        refined: 정밀 보정 결과로 만든 모자이크인지 여부.
        warnings: 경고 문장 목록.
        report: 처리 보고서 전체 (``report.json``과 같음).
    """

    orthomosaic: Path
    dsm: Path
    preview: Path
    report_path: Path
    epsg: int
    gsd_m: float
    width: int
    height: int
    corners_lonlat: list[list[float]]
    refined: bool
    warnings: list[str]
    report: dict[str, Any] = field(repr=False)

    @property
    def total_time_s(self) -> float:
        """정렬 + 마지막 정사 모자이크 생성에 걸린 시간(초)."""
        return float(self.report.get("timings_s", {}).get("total_s", 0.0))

    @property
    def peak_memory_mb(self) -> float:
        """처리 중 최대 메모리 사용량(MB)."""
        return float(self.report.get("peak_memory_mb", 0.0))

    @staticmethod
    def from_report(report: dict[str, Any], workspace: Path) -> "OrthoResult":
        o = report["outputs"]
        return OrthoResult(
            orthomosaic=Path(o["orthomosaic"]),
            dsm=Path(o["dsm"]),
            preview=Path(o["preview"]),
            report_path=Path(workspace) / "report.json",
            epsg=int(report["georef"]["epsg"]),
            gsd_m=float(report["ortho"]["gsd_m"]),
            width=int(report["ortho"]["width"]),
            height=int(report["ortho"]["height"]),
            corners_lonlat=report["preview_corners_lonlat"],
            refined="refine" in report,
            warnings=list(report.get("warnings", [])),
            report=report,
        )


@dataclass(frozen=True)
class RefineResult:
    """정밀 보정 결과 (:meth:`quickortho.Project.refine`).

    Attributes:
        mode: 좌표 기준. ``"gcp"``(기준점 3점 이상), ``"gcp_shift"``(1~2점, 평행 이동만),
            ``"gps"``(GCP 없이 번들 조정 후 GPS 재정렬), ``"base"``(보정 취소).
        rmse_before_px: 보정 전 재투영 RMSE(px).
        rmse_after_px: 보정 후 재투영 RMSE(px).
        control: 기준점 오차 요약 (``count``, ``rmse_x``, ``rmse_y``, ``rmse_z``, ``rmse_xy``, ``rmse_3d``, 단위 m). 없으면 ``None``.
        check: 검사점 오차 요약 (형식은 ``control``과 같음). 없으면 ``None``.
        gcps: GCP별 오차 표 (``name``, ``role``, ``dx``, ``dy``, ``dz``, ``dxy``, ``reproj_px`` 등).
        ortho: 보정 결과로 다시 만든 정사 모자이크.
        warnings: 보정 경고 문장 목록.
        raw: ``report.json``의 ``refine`` 항목 (보정 취소 시 빈 dict).
    """

    mode: str
    rmse_before_px: Optional[float]
    rmse_after_px: Optional[float]
    control: Optional[dict[str, Any]]
    check: Optional[dict[str, Any]]
    gcps: list[dict[str, Any]]
    ortho: OrthoResult
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)

    @staticmethod
    def from_report(report: dict[str, Any], workspace: Path) -> "RefineResult":
        r = report.get("refine") or {}
        summ = r.get("gcp_summary") or {}
        return RefineResult(
            mode=r.get("mode", "base"),
            rmse_before_px=(r.get("before") or {}).get("rmse_px"),
            rmse_after_px=(r.get("after") or {}).get("rmse_px"),
            control=summ.get("control"),
            check=summ.get("check"),
            gcps=list(r.get("gcps", [])),
            ortho=OrthoResult.from_report(report, workspace),
            warnings=list(r.get("warnings", [])),
            raw=r,
        )


__all__ = ["ScanResult", "PreviewResult", "AlignResult", "OrthoResult", "RefineResult"]
