"""GCP(지상기준점)와 사진 표시 데이터, GCP 측량 성과 파일 읽기.

GCP 보정 흐름::

    gcps = quickortho.load_gcps("gcp.csv", epsg=5186)        # 측량 성과 읽기
    gcps[0].marks.append(Mark("DJI_0013.JPG", 2310.5, 1520.0)) # 사진에 표시 (사진 2장 이상)
    ...
    project.set_edits(gcps=gcps)
    result = project.refine()

사진 좌표(``Mark.x``, ``Mark.y``)는 원본 사진의 화소 좌표이며, 원점은 좌상단 화소의 왼쪽 위 모서리, x는 오른쪽, y는 아래쪽임.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from ._core import marking
from .errors import InputError


@dataclass
class Mark:
    """사진 위에 표시한 점 하나.

    Attributes:
        image: 사진 파일 이름 (영상 폴더 기준 상대 경로, 예: ``"DJI_0013.JPG"``).
        x: 원본 사진 화소 좌표 x (오른쪽이 +).
        y: 원본 사진 화소 좌표 y (아래쪽이 +).
    """

    image: str
    x: float
    y: float

    def to_dict(self) -> dict[str, Any]:
        return {"image": self.image, "x": float(self.x), "y": float(self.y)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Mark":
        return Mark(str(d["image"]), float(d["x"]), float(d["y"]))


@dataclass
class GCP:
    """지상기준점(GCP) 또는 검사점.

    Attributes:
        name: 점 이름.
        x: 측량 좌표 X (동쪽 방향 값. 경위도 좌표계면 경도).
        y: 측량 좌표 Y (북쪽 방향 값. 경위도 좌표계면 위도).
        z: 측량 표고(m).
        epsg: 측량 좌표계 EPSG (예: 5186 = GRS80 중부원점).
        role: ``"control"``(기준점, 보정에 사용) 또는 ``"check"``(검사점, 정확도 평가에만 사용).
        marks: 이 점을 표시한 사진 좌표 목록. 기준점으로 쓰려면 정합된 사진 2장 이상에 표시해야 함.
    """

    name: str
    x: float
    y: float
    z: float
    epsg: int
    role: Literal["control", "check"] = "control"
    marks: list[Mark] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "x": float(self.x), "y": float(self.y), "z": float(self.z), "epsg": int(self.epsg),
            "role": "check" if self.role == "check" else "control",
            "obs": [m.to_dict() for m in self.marks],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "GCP":
        return GCP(
            name=str(d["name"]), x=float(d["x"]), y=float(d["y"]), z=float(d["z"]), epsg=int(d["epsg"]),
            role="check" if d.get("role") == "check" else "control",
            marks=[Mark.from_dict(o) for o in d.get("obs", [])],
        )


@dataclass
class TiePoint:
    """수동 타이포인트: 같은 지점을 여러 사진에 표시한 것. 정합된 사진 2장 이상에 표시해야 삼각측량됨.

    Attributes:
        id: 점 식별자 (예: ``"T1"``).
        marks: 사진 좌표 목록.
    """

    id: str
    marks: list[Mark] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "obs": [m.to_dict() for m in self.marks]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TiePoint":
        return TiePoint(str(d["id"]), [Mark.from_dict(o) for o in d.get("obs", [])])


def read_gcp_file(
    path: str | Path,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
    workspace: Optional[str | Path] = None,
) -> dict[str, Any]:
    """CSV·TXT 측량 성과 파일을 읽어 표와 열·좌표계 추정값을 돌려줌 (저수준 함수).

    Args:
        path: 측량 성과 파일.
        encoding: 문자 인코딩. ``None``이면 UTF-8(BOM 포함) → CP949 순으로 시도함.
        delimiter: 구분자 (``","``, ``"\\t"``, ``";"``, ``"whitespace"``). ``None``이면 자동 판단함.
        workspace: 정렬을 마친 워크스페이스. 주면 촬영 위치와 비교해 좌표계(EPSG)와 X/Y 순서를 추정함.

    Returns:
        dict: ``encoding``, ``delimiter``, ``header``, ``rows``(최대 500행), ``num_rows``,
        ``guess``(``name``·``x``·``y``·``z`` 열 번호, 추정 ``epsg``, ``distance_km``) 등.

    Raises:
        InputError: 파일이 비어 있음 (code ``empty_file``).
    """
    if delimiter in ("\\t", "tab"):
        delimiter = "\t"
    return marking.parse_gcp_file(Path(path), encoding, delimiter, Path(workspace) if workspace else None)


def load_gcps(
    path: str | Path,
    epsg: Optional[int] = None,
    *,
    columns: Optional[dict[str, Optional[int]]] = None,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
    workspace: Optional[str | Path] = None,
    check: Optional[list[str]] = None,
) -> list[GCP]:
    """측량 성과 파일을 읽어 :class:`GCP` 목록을 만듦.

    Args:
        path: CSV·TXT 파일.
        epsg: 측량 좌표계 EPSG. ``None``이면 ``workspace``의 촬영 위치로 추정함 (추정 실패 시 오류).
        columns: 열 번호 지정 ``{"name": 0, "x": 1, "y": 2, "z": 3}`` (0부터 셈). ``None``이면 머리글·값으로 추정함.
            ``z``가 ``None``이면 표고를 0으로 둠.
        encoding: 문자 인코딩 (``None``이면 자동).
        delimiter: 구분자 (``None``이면 자동).
        workspace: 좌표계·X/Y 순서 추정에 쓸 정렬된 워크스페이스.
        check: 검사점으로 쓸 점 이름 목록. 나머지는 기준점이 됨.

    Returns:
        GCP 목록 (``marks``는 비어 있음).

    Raises:
        InputError: 파일이 비었거나(``empty_file``), 좌표 열·좌표계를 정할 수 없음(``invalid_argument``).
    """
    parsed = read_gcp_file(path, encoding, delimiter, workspace)
    guess = parsed["guess"]
    cols = dict(columns) if columns else {k: guess.get(k) for k in ("name", "x", "y", "z")}
    if cols.get("x") is None or cols.get("y") is None:
        raise InputError("GCP 파일에서 X·Y 좌표 열을 찾을 수 없음. columns로 지정해야 함", "invalid_argument")
    if epsg is None:
        epsg = guess.get("epsg")
        if epsg is None:
            raise InputError("GCP 좌표계를 추정할 수 없음. epsg를 지정해야 함", "invalid_argument")
    # 전체 행 다시 읽기 (미리보기는 500행으로 잘림)
    rows = parsed["rows"]
    if parsed["num_rows"] > len(rows):
        text, _ = marking._decode(Path(path).read_bytes(), parsed["encoding"])
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        rows = marking._split(lines, parsed["delimiter"])
        if parsed["header"] is not None:
            rows = rows[1:]
    check_set = set(check or [])
    return [
        GCP(name=str(r["name"]), x=r["x"], y=r["y"], z=r["z"], epsg=int(epsg),
            role="check" if str(r["name"]) in check_set else "control")
        for r in marking.read_gcp_rows(rows, cols)
    ]


__all__ = ["Mark", "GCP", "TiePoint", "read_gcp_file", "load_gcps"]
