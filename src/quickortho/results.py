"""처리 결과 객체.

모든 결과 객체는 두 가지 방식으로 받을 수 있음.

1. **결과 객체로 받기**: 자주 쓰는 값을 속성으로, 원본 dict 전체를 ``raw``(또는 ``report``)로 가짐
2. **여러 변수로 풀어서 받기**: OpenCV 함수처럼 핵심 산출물이 차례로 나옴. 다른 라이브러리의 입력으로 바로 씀

    res = project.process()                     # 1
    image, transform, crs = project.process()   # 2

풀었을 때 나오는 값:

=================  ===========================================================
결과                풀기
=================  ===========================================================
``ScanResult``     ``images, summary``
``PreviewResult``  ``quicklook, coverage, transform, crs``
``AlignResult``    ``cameras, points, crs``
``OrthoResult``    ``image, transform, crs``
``RefineResult``   ``image, transform, crs, gcps``
=================  ===========================================================

래스터 값은 numpy 배열(채널이 마지막 축), ``transform``은 ``affine.Affine``, ``crs``는 ``pyproj.CRS``임.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np

from .geodata import Bounds, Cameras, ChannelOrder, PointCloud


@dataclass(frozen=True)
class ScanResult:
    """영상 폴더 스캔 결과 (:func:`quickortho.scan`). 풀면 ``images, summary``.

    Attributes:
        folder: 스캔한 폴더.
        images: 영상별 정보 목록. 각 항목은 dict이며 주요 키는 ``file``, ``width``, ``height``, ``make``, ``model``,
            ``lat``, ``lon``, ``abs_alt``, ``rel_alt``, ``gimbal_yaw``, ``gimbal_pitch``, ``selected``, ``flags``.
        cameras: 카메라(기종·초점거리·해상도 조합)별 요약. 키는 카메라 식별 문자열.
        summary: 전체 요약 (``total_files``, ``selected``, ``no_gps``, ``oblique``, ``rolling_shutter_warning`` 등).
        excluded_cameras: 자동 제외한 카메라와 사유 (예: 망원 카메라).
        failed: 읽기 실패한 파일과 오류 메시지.
        raw: 원본 dict.
        multispectral: 다중분광 영상 폴더이면 요약 (``model``, ``bands``(이름·파장), ``reference_band``,
            ``captures``, ``panel_captures``, ``incomplete_captures``, ``has_dls``). RGB 폴더면 ``None``.
            다중분광 폴더의 ``images``에는 밴드 영상이 모두 들어가며 ``band``·``capture`` 키가 있고,
            정렬에 쓰는 기준 밴드만 ``selected``임.
    """

    folder: Path
    images: list[dict[str, Any]]
    cameras: dict[str, dict[str, Any]]
    summary: dict[str, Any]
    excluded_cameras: dict[str, str]
    failed: list[dict[str, str]]
    raw: dict[str, Any] = field(repr=False)
    multispectral: Optional[dict[str, Any]] = None

    def __iter__(self) -> Iterator[Any]:
        yield self.images
        yield self.summary

    @property
    def selected_images(self) -> list[dict[str, Any]]:
        """처리에 쓸 영상(매핑용 카메라로 선별된 영상)만."""
        return [im for im in self.images if im.get("selected")]

    @property
    def num_with_gps(self) -> int:
        """선별된 영상 중 GPS 정보가 있는 영상 수."""
        return sum(1 for im in self.selected_images if im.get("lat") is not None and im.get("lon") is not None)

    def positions(self, selected_only: bool = True) -> np.ndarray:
        """촬영 위치 배열 (N, 3) [경도, 위도, 고도]. 고도는 절대고도, 없으면 상대고도, 둘 다 없으면 NaN.
        GPS가 없는 영상은 NaN 행. 순서는 ``images``(또는 ``selected_images``)와 같음."""
        ims = self.selected_images if selected_only else self.images
        out = np.full((len(ims), 3), np.nan)
        for i, im in enumerate(ims):
            alt = im.get("abs_alt") if im.get("abs_alt") is not None else im.get("rel_alt")
            lon, lat = im.get("lon"), im.get("lat")
            out[i] = [np.nan if lon is None else lon, np.nan if lat is None else lat, np.nan if alt is None else alt]
        return out

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
            multispectral=d.get("multispectral"),
        )


@dataclass(frozen=True)
class PreviewResult:
    """빠른 미리보기 결과 (:func:`quickortho.preview`). 풀면 ``quicklook, coverage, transform, crs``.

    ``quicklook``(간이 모자이크 RGBA, 없으면 ``None``)과 ``coverage``(중복 매수 uint16 격자)는
    같은 격자라서 ``transform``·``crs``를 함께 씀.

    Attributes:
        output_dir: 결과 폴더.
        quicklook_path: 간이 모자이크 PNG 경로. ``quicklook=False``로 실행했으면 ``None``.
        coverage_path: 중복도 지도(색 입힌) PNG 경로.
        geojson: 촬영 범위·누락 구역·저중복 구역·촬영 위치 GeoJSON 경로 (WGS84).
        corners_lonlat: 두 PNG의 네 모서리 경위도 [좌상, 우상, 우하, 좌하]. 지도에 겹쳐 그릴 때 사용.
        num_images: 미리보기에 쓴 영상 수.
        num_gaps: 누락 구역 수.
        coverage_stats: 조사 면적, 누락 면적, 저중복 면적, 중복도 중앙값 등.
        warnings: 경고 문장 목록.
        raw: 원본 dict (``preview.json``과 같음).
        multispectral: 다중분광 폴더이면 스캔 요약에 ``index_catalog``(지수별 ``name``, ``label``, ``bands``,
            ``available``(이 카메라 밴드로 만들 수 있는지), ``default``)를 더한 dict. RGB 폴더면 ``None``.
    """

    output_dir: Path
    quicklook_path: Optional[Path]
    coverage_path: Path
    geojson: Path
    corners_lonlat: list[list[float]]
    num_images: int
    num_gaps: int
    coverage_stats: dict[str, float]
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)
    multispectral: Optional[dict[str, Any]] = None

    # 0.1.0 호환 이름
    @property
    def quicklook(self) -> Optional[Path]:
        """간이 모자이크 PNG 경로 (``quicklook_path``와 같음, 0.1.0 호환)."""
        return self.quicklook_path

    @property
    def coverage(self) -> Path:
        """중복도 지도 PNG 경로 (``coverage_path``와 같음, 0.1.0 호환)."""
        return self.coverage_path

    def __iter__(self) -> Iterator[Any]:
        yield self.read_quicklook()
        yield self.read_coverage()
        yield self.transform
        yield self.crs

    @property
    def crs(self):
        """좌표계 (``pyproj.CRS``, 촬영 위치의 UTM)."""
        from pyproj import CRS

        return CRS.from_epsg(int(self.raw["epsg"]))

    @property
    def transform(self):
        """격자 화소(열, 행) → 지도(x, y) 변환 (``affine.Affine``). quicklook·coverage 공통."""
        from affine import Affine

        cell = float(self.raw["cell_m"])
        b = self.raw.get("bounds")
        if b is None:
            raise ValueError("이 미리보기 결과에는 격자 범위(bounds)가 없음. SDK 0.2.0 이상으로 다시 실행해야 함")
        return Affine(cell, 0.0, b[0], 0.0, -cell, b[3])

    @property
    def bounds(self) -> Bounds:
        """격자 범위 (xmin, ymin, xmax, ymax), 지도 좌표."""
        return tuple(self.raw["bounds"])  # type: ignore[return-value]

    def read_quicklook(self, order: ChannelOrder = "RGBA") -> Optional[np.ndarray]:
        """간이 모자이크 ``(H, W, C)`` uint8. 없으면 ``None``."""
        if self.quicklook_path is None:
            return None
        from PIL import Image

        from .geodata import _reorder

        with Image.open(self.quicklook_path) as im:
            return _reorder(np.asarray(im.convert("RGBA")), order)

    def read_coverage(self) -> np.ndarray:
        """중복 매수 격자 ``(H, W)`` uint16 (0 = 촬영 안 됨)."""
        from .geodata import read_raster

        arr, _, _ = read_raster(self.output_dir / "coverage.tif", channels_last=False)
        return arr[0]

    def read_gap_mask(self) -> np.ndarray:
        """누락 구역 격자 ``(H, W)`` bool."""
        from .geodata import read_raster

        arr, _, _ = read_raster(self.output_dir / "coverage.tif", channels_last=False)
        return arr[1] > 0

    def _shapes(self, kind: str, crs: str) -> list:
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform as sh_transform

        gj = json.loads(Path(self.geojson).read_text(encoding="utf-8"))
        geoms = [shape(f["geometry"]) for f in gj["features"] if f["properties"].get("kind") == kind]
        if crs == "lonlat":
            return geoms
        tr = Transformer.from_crs(4326, int(self.raw["epsg"]), always_xy=True)
        return [sh_transform(tr.transform, g) for g in geoms]

    def gaps(self, crs: str = "map") -> list:
        """누락 구역 shapely Polygon 목록. ``crs="map"``은 지도 좌표(UTM), ``"lonlat"``은 경위도."""
        return self._shapes("gap", crs)

    def footprints(self, crs: str = "map") -> list:
        """영상별 촬영 범위 shapely Polygon 목록 (``footprint_names``와 같은 순서)."""
        return self._shapes("footprint", crs)

    @property
    def footprint_names(self) -> list[str]:
        gj = json.loads(Path(self.geojson).read_text(encoding="utf-8"))
        return [f["properties"]["file"] for f in gj["features"] if f["properties"].get("kind") == "footprint"]

    def low_overlap(self, crs: str = "map") -> list:
        """저중복(1장) 구역 shapely Polygon 목록."""
        return self._shapes("low_overlap", crs)

    @staticmethod
    def from_dict(d: dict[str, Any], output_dir: Path) -> "PreviewResult":
        o = d["outputs"]
        return PreviewResult(
            output_dir=Path(output_dir),
            quicklook_path=Path(o["quicklook"]) if o.get("quicklook") else None,
            coverage_path=Path(o["coverage"]),
            geojson=Path(o["geojson"]),
            corners_lonlat=d["corners_lonlat"],
            num_images=d["num_images"],
            num_gaps=d["gaps"],
            coverage_stats=d["coverage"],
            warnings=d.get("warnings", []),
            raw=d,
            multispectral=d.get("multispectral"),
        )


@dataclass(frozen=True)
class AlignResult:
    """정렬(SfM + GPS 좌표 정렬) 결과 (:meth:`quickortho.Project.align`). 풀면 ``cameras, points, crs``.

    Attributes:
        num_images: SfM에 넣은 영상 수.
        num_registered: 위치·자세가 추정된(정합된) 영상 수.
        num_points: 희소 3D 점 수.
        reprojection_error_px: 평균 재투영 오차(px).
        epsg: 결과 좌표계 EPSG (UTM).
        gps_residual_m: 정렬 후 카메라 위치와 GPS의 수평 RMS 차이(m).
        warnings: 경고 문장 목록.
        raw: 원본 dict (``project/align_report.json``과 같음).
        workspace: 워크스페이스 경로 (카메라·점군을 읽을 때 씀).
    """

    num_images: int
    num_registered: int
    num_points: int
    reprojection_error_px: float
    epsg: int
    gps_residual_m: float
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)
    workspace: Optional[Path] = field(default=None, repr=False)

    def __iter__(self) -> Iterator[Any]:
        yield self.cameras
        yield self.points
        yield self.crs

    @property
    def crs(self):
        """좌표계 (``pyproj.CRS``)."""
        from pyproj import CRS

        return CRS.from_epsg(self.epsg)

    def _load(self):
        if self.workspace is None:
            raise ValueError("워크스페이스 정보가 없어 카메라·점군을 읽을 수 없음")
        from ._core.project import Project as _Store

        return _Store(self.workspace).load_base()

    @cached_property
    def cameras(self) -> Cameras:
        """정합된 영상의 카메라 자세 목록 (:class:`~quickortho.CameraPose`, 이름순)."""
        from .geodata import cameras_from_reconstruction

        rec, frame = self._load()
        return cameras_from_reconstruction(rec, frame.origin, frame.epsg)

    @cached_property
    def points(self) -> PointCloud:
        """희소 점군 (:class:`~quickortho.PointCloud`)."""
        from .geodata import points_from_reconstruction

        rec, frame = self._load()
        return points_from_reconstruction(rec, frame.origin, frame.epsg)

    @staticmethod
    def from_dict(d: dict[str, Any], workspace: Optional[Path] = None) -> "AlignResult":
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
            workspace=Path(workspace) if workspace else None,
        )


@dataclass(frozen=True)
class MultispectralInfo:
    """다중분광 정사 모자이크 정보 (:attr:`OrthoResult.multispectral`).

    Attributes:
        model: 카메라 기종 (예: ``"MicaSense RedEdge-P"``).
        bands: 정사 모자이크의 밴드 이름 (파장 순, 팬크로매틱·열적외 제외). 배열의 마지막 축 순서와 같음.
        wavelengths_nm: 밴드 중심 파장(nm). 모르면 ``None``.
        reference_band: 정렬(SfM)에 쓴 기준 밴드 이름.
        values: ``"reflectance"``(반사율, 0~1 부근) 또는 ``"radiance"``(방사휘도 W/m²/sr/nm).
        radiometry: 반사율 환산 방식 (``"dls+panel"``, ``"dls"``, ``"panel"``, ``"radiance"``).
        indices: 지수 이름 → GeoTIFF 경로 (``indices/<이름>.tif``, float32 단일 밴드). 등록 순서.
        index_previews: 지수 이름 → 색 띠 미리보기 PNG 경로.
        indices_skipped: 골랐지만 카메라 밴드가 없어 만들지 못한 지수 (예: Blue가 없는 카메라의 EVI).
        index_legend: 지수별 범례 ``{"label", "min", "max", "colors"}`` (미리보기 색 범위와 색 띠 양끝·가운데 hex).
        band_alignment_px: 밴드별 정합 잔차(화소, rig 미세 조정 후 남은 이동량의 크기).
        capture_inconsistency_pct: 밴드별 캡처 간 반사율 불일치(%) ``(보정 전, 보정 후)``. 블록 조정을 안 했으면 ``None``.
        workspace: 워크스페이스 경로.
        report: 보고서의 ``multispectral`` 항목 전체.

    0.4.0 결과(지수를 다중밴드 ``indices.tif`` 하나로 만듦)를 열면 ``indices``는 빈 dict이고 ``index_names``에만 이름이 있음.
    :meth:`read_indices`는 두 형식 모두 읽음.
    """

    model: str
    bands: list[str]
    wavelengths_nm: list[Optional[float]]
    reference_band: str
    values: str
    radiometry: str
    indices: dict[str, Path]
    index_previews: dict[str, Path]
    indices_skipped: list[str]
    index_legend: dict[str, dict[str, Any]]
    band_alignment_px: dict[str, float]
    capture_inconsistency_pct: Optional[dict[str, tuple[float, float]]]
    workspace: Optional[Path] = field(default=None, repr=False)
    report: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def index_names(self) -> list[str]:
        """만든 지수 이름 목록."""
        return list(self.report.get("indices", list(self.indices)))

    @property
    def index_preview(self) -> Optional[Path]:
        """NDVI 미리보기 PNG (없으면 첫 지수, 지수가 없으면 ``None``). 0.4.0 호환용."""
        if self.index_previews:
            return self.index_previews.get("NDVI") or next(iter(self.index_previews.values()))
        legacy = self.workspace / "ndvi.png" if self.workspace else None
        return legacy if legacy is not None and legacy.exists() else None

    def read_indices(self, index: Optional[str] = None, *, scale: float = 1.0, bounds: Optional[Bounds] = None):
        """지수를 ``(array, transform, crs)``로 읽음. :func:`quickortho.read_indices` 참고."""
        from .geodata import read_indices

        if self.workspace is None:
            from .errors import ProjectError

            raise ProjectError("워크스페이스를 알 수 없음", "not_rendered")
        return read_indices(self.workspace, index, scale=scale, bounds=bounds)

    @staticmethod
    def from_report(report: dict[str, Any], workspace: Optional[Path] = None) -> Optional["MultispectralInfo"]:
        m = report.get("multispectral")
        if not m:
            return None
        o = report.get("outputs", {})
        rad = m.get("radiometry", {})
        resid = (rad.get("band_alignment") or {}).get("residual") or {}
        norm = rad.get("capture_normalization") or {}
        incons = None
        if norm.get("applied"):
            incons = {b: (v["inconsistency_before_pct"], v["inconsistency_after_pct"])
                      for b, v in norm.get("bands", {}).items()}
        tifs = o.get("indices") if isinstance(o.get("indices"), dict) else {}  # 0.4.0은 다중밴드 파일 경로 문자열
        return MultispectralInfo(
            model=m.get("model", ""),
            bands=[b["name"] for b in m.get("bands", [])],
            wavelengths_nm=[b.get("wavelength_nm") for b in m.get("bands", [])],
            reference_band=m.get("reference_band", ""),
            values=m.get("values", "reflectance"),
            radiometry=rad.get("mode", ""),
            indices={k: Path(v) for k, v in tifs.items()},
            index_previews={k: Path(v) for k, v in (o.get("index_previews") or {}).items()},
            indices_skipped=list(m.get("indices_skipped", [])),
            index_legend=dict(m.get("index_legend", {})),
            band_alignment_px={b: float(np.hypot(*v["shift_px"])) for b, v in resid.items()},
            capture_inconsistency_pct=incons,
            workspace=Path(workspace) if workspace is not None else None,
            report=m,
        )


@dataclass(frozen=True)
class OrthoResult:
    """정사 모자이크 결과 (:meth:`quickortho.Project.orthomosaic`, :meth:`~quickortho.Project.process`).
    풀면 ``image, transform, crs``.

    ``image``는 정사 모자이크 전체를 ``(H, W, 4)`` uint8 RGBA로 읽은 배열임. 다중분광이면 ``(H, W, 밴드)`` float32
    반사율이고 자료 없는 곳은 NaN임 (밴드 순서는 ``multispectral.bands``). 결과가 크면 메모리를 많이 쓰므로
    ``read(scale=0.25)``나 ``read(bounds=...)``로 필요한 만큼만 읽는 것을 권장함.

    Attributes:
        orthomosaic: 정사 모자이크 GeoTIFF(COG) 경로. RGB는 RGBA uint8, 다중분광은 밴드별 float32(nodata NaN).
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
        multispectral: 다중분광 결과이면 :class:`MultispectralInfo` (밴드, 반사율 방식, 지수 경로, 정합·불일치 통계).
            RGB 결과면 ``None``.
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
    multispectral: Optional[MultispectralInfo] = None

    def __iter__(self) -> Iterator[Any]:
        image, transform, crs = self.read()
        yield image
        yield transform
        yield crs

    @property
    def indices(self) -> dict[str, Path]:
        """(다중분광) 지수 이름 → GeoTIFF 경로. RGB 결과나 지수를 만들지 않았으면 빈 dict."""
        return dict(self.multispectral.indices) if self.multispectral else {}

    @property
    def indices_skipped(self) -> list[str]:
        """(다중분광) 골랐지만 카메라 밴드가 없어 만들지 못한 지수."""
        return list(self.multispectral.indices_skipped) if self.multispectral else []

    @property
    def total_time_s(self) -> float:
        """정렬 + 마지막 정사 모자이크 생성에 걸린 시간(초)."""
        return float(self.report.get("timings_s", {}).get("total_s", 0.0))

    @property
    def peak_memory_mb(self) -> float:
        """처리 중 최대 메모리 사용량(MB)."""
        return float(self.report.get("peak_memory_mb", 0.0))

    @cached_property
    def _header(self) -> tuple:
        import rasterio
        from pyproj import CRS

        with rasterio.open(self.orthomosaic) as ds:
            return ds.transform, CRS.from_user_input(ds.crs.to_wkt()), tuple(ds.bounds)

    @property
    def transform(self):
        """화소(열, 행) → 지도(x, y) 변환 (``affine.Affine``). ``x, y = transform * (col, row)``."""
        return self._header[0]

    @property
    def crs(self):
        """좌표계 (``pyproj.CRS``)."""
        return self._header[1]

    @property
    def bounds(self) -> Bounds:
        """결과 범위 (xmin, ymin, xmax, ymax), 지도 좌표."""
        return self._header[2]

    def read(self, *, scale: float = 1.0, bounds: Optional[Bounds] = None, order: Optional[ChannelOrder] = None):
        """정사 모자이크를 ``(image, transform, crs)``로 읽음. :func:`quickortho.read_orthomosaic` 참고."""
        from .geodata import read_orthomosaic

        return read_orthomosaic(self.orthomosaic, scale=scale, bounds=bounds, order=order)

    def read_dsm(self, *, scale: float = 1.0, bounds: Optional[Bounds] = None):
        """간이 DSM을 ``(z, transform, crs)``로 읽음. DSM은 정사 모자이크와 해상도·범위가 다름."""
        from .geodata import read_dsm

        return read_dsm(self.dsm, scale=scale, bounds=bounds)

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
            multispectral=MultispectralInfo.from_report(report, workspace),
        )


@dataclass(frozen=True)
class RefineResult:
    """정밀 보정 결과 (:meth:`quickortho.Project.refine`). 풀면 ``image, transform, crs, gcps``.

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

    def __iter__(self) -> Iterator[Any]:
        image, transform, crs = self.ortho.read()
        yield image
        yield transform
        yield crs
        yield self.gcps

    @property
    def residuals(self) -> np.ndarray:
        """GCP 오차 배열 (N, 3) [dx, dy, dz] (m, 추정 - 측량). 삼각측량하지 못한 점은 NaN. 순서는 ``gcps``와 같음."""
        return np.array([[np.nan if r.get(k) is None else r[k] for k in ("dx", "dy", "dz")] for r in self.gcps],
                        dtype=float).reshape(-1, 3)

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


__all__ = ["ScanResult", "PreviewResult", "AlignResult", "OrthoResult", "RefineResult", "MultispectralInfo"]
