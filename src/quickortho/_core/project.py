"""정밀 보정용 SfM 프로젝트 저장·불러오기.

정사 모자이크 결과 폴더(`<폴더명>_QuickOrtho/ortho/`) 안에 `project/`를 만든다.

    project/
    ├── meta.json        영상 폴더, 영상별 GPS, 엔진 버전
    ├── base/            SfM + GPS 정렬 직후 재구성 (지역 좌표계, 수정하지 않음)
    ├── base_frame.json  base 좌표계: {"epsg": 32652, "origin": [E, N, Z]}
    ├── refined/         마지막 보정 결과 재구성 (지역 좌표계)
    ├── refined_frame.json
    ├── edits.json       사용자 수정 사항 (삭제한 점, 자동 제거 기준, 수동 타이포인트, GCP)
    └── cache/           사진 보기용 축소 영상·확대 조각

재구성은 수치 안정성을 위해 지역 좌표계(투영 좌표 - origin)로 저장한다.
보정은 항상 base에서 시작해 edits를 처음부터 다시 적용하므로, 여러 번 보정해도 결과가 누적되지 않는다.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pycolmap

EDITS_VERSION = 1


@dataclass
class Frame:
    epsg: int
    origin: np.ndarray  # (3,) 투영 좌표계 원점

    def to_json(self) -> dict:
        return {"epsg": int(self.epsg), "origin": [float(v) for v in self.origin]}

    @staticmethod
    def from_json(d: dict) -> "Frame":
        return Frame(int(d["epsg"]), np.asarray(d["origin"], dtype=np.float64))


def empty_edits() -> dict[str, Any]:
    return {
        "version": EDITS_VERSION,
        "deleted_points": [],  # base 재구성의 3D 점 ID
        "max_reproj_error_px": None,  # 이 값보다 큰 관측을 자동 제거 (None이면 사용 안 함)
        "tiepoints": [],  # [{"id": "T1", "obs": [{"image": 파일명, "x": px, "y": px}]}]
        "gcps": [],  # [{"name", "x", "y", "z", "epsg", "role": "control"|"check", "obs": [...]}]
    }


class Project:
    def __init__(self, ortho_dir: Path) -> None:
        self.ortho_dir = Path(ortho_dir).resolve()
        self.dir = self.ortho_dir / "project"

    # ── 존재 여부 ──
    def exists(self) -> bool:
        return (self.dir / "meta.json").exists() and (self.dir / "base").is_dir()

    def require(self) -> None:
        if not self.exists():
            raise RuntimeError(
                "보정용 프로젝트가 없음. 이 버전에서 정사 모자이크를 다시 생성해야 정밀 보정을 쓸 수 있음"
            )

    # ── 메타 정보 ──
    def save_meta(
        self,
        image_dir: Path,
        gps: dict[str, tuple[float, float, float]],
        engine_version: str,
        render: dict | None = None,
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "image_dir": str(Path(image_dir).resolve()),
            "gps": {k: list(v) for k, v in gps.items()},
            "engine_version": engine_version,
            "render": render or {},
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def meta(self) -> dict:
        return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))

    @property
    def image_dir(self) -> Path:
        return Path(self.meta()["image_dir"])

    # ── 재구성 ──
    def _save_rec(self, name: str, rec_local: pycolmap.Reconstruction, frame: Frame) -> None:
        d = self.dir / name
        d.mkdir(parents=True, exist_ok=True)
        rec_local.write(str(d))
        (self.dir / f"{name}_frame.json").write_text(json.dumps(frame.to_json()), encoding="utf-8")

    def _load_rec(self, name: str) -> tuple[pycolmap.Reconstruction, Frame]:
        rec = pycolmap.Reconstruction(str(self.dir / name))
        frame = Frame.from_json(json.loads((self.dir / f"{name}_frame.json").read_text(encoding="utf-8")))
        return rec, frame

    def save_base(self, rec_abs: pycolmap.Reconstruction, epsg: int, origin: np.ndarray) -> None:
        """투영 좌표계(절대값) 재구성을 지역 좌표계로 바꿔 base로 저장한다. rec_abs는 바꾸지 않는다.

        정사 모자이크를 새로 만들면 이전 보정 결과와 점 ID 기반 수정(삭제한 점)은 무효가 되므로 지운다.
        GCP·수동 타이포인트의 사진 좌표는 재구성과 무관하므로 그대로 둔다.
        """
        shutil.rmtree(self.dir / "refined", ignore_errors=True)
        (self.dir / "refined_frame.json").unlink(missing_ok=True)
        (self.dir / "refined_extra.json").unlink(missing_ok=True)
        shutil.rmtree(self.dir / "cache", ignore_errors=True)
        if self.edits_path.exists():
            edits = self.load_edits()
            edits["deleted_points"] = []
            self.save_edits(edits)
        local = pycolmap.Reconstruction(rec_abs)
        local.transform(pycolmap.Sim3d(1.0, pycolmap.Rotation3d(), -np.asarray(origin, dtype=np.float64)))
        self._save_rec("base", local, Frame(epsg, np.asarray(origin, dtype=np.float64)))

    def base_frame(self) -> Frame:
        return Frame.from_json(json.loads((self.dir / "base_frame.json").read_text(encoding="utf-8")))

    def load_base(self) -> tuple[pycolmap.Reconstruction, Frame]:
        return self._load_rec("base")

    def save_refined(self, rec_local: pycolmap.Reconstruction, frame: Frame, extra: dict) -> None:
        """extra: {"vertical": "gcp"|"gps", "manual_points": {point3D_id: 타이포인트 ID}, ...}"""
        self._save_rec("refined", rec_local, frame)
        (self.dir / "refined_extra.json").write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")

    def refined_extra(self) -> dict:
        p = self.dir / "refined_extra.json"
        if not self.has_refined() or not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def reset_refined(self) -> None:
        shutil.rmtree(self.dir / "refined", ignore_errors=True)
        (self.dir / "refined_frame.json").unlink(missing_ok=True)
        (self.dir / "refined_extra.json").unlink(missing_ok=True)

    def has_refined(self) -> bool:
        return (self.dir / "refined").is_dir() and (self.dir / "refined_frame.json").exists()

    def load_current(self) -> tuple[pycolmap.Reconstruction, Frame, str]:
        """보정 결과가 있으면 그것을, 없으면 base를 불러온다."""
        if self.has_refined():
            rec, frame = self._load_rec("refined")
            return rec, frame, "refined"
        rec, frame = self.load_base()
        return rec, frame, "base"

    def save_base_report(self, report: dict) -> None:
        (self.dir / "base_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    def base_report(self) -> dict:
        p = self.dir / "base_report.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    # ── 수정 사항 ──
    @property
    def edits_path(self) -> Path:
        return self.dir / "edits.json"

    def load_edits(self) -> dict[str, Any]:
        if not self.edits_path.exists():
            return empty_edits()
        e = json.loads(self.edits_path.read_text(encoding="utf-8"))
        base = empty_edits()
        base.update(e)
        return base

    def save_edits(self, edits: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.edits_path.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def cache_dir(self) -> Path:
        d = self.dir / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d


def to_absolute(rec_local: pycolmap.Reconstruction, frame: Frame) -> pycolmap.Reconstruction:
    """지역 좌표계 재구성의 사본을 투영 좌표계(절대값)로 옮긴다."""
    rec = pycolmap.Reconstruction(rec_local)
    rec.transform(pycolmap.Sim3d(1.0, pycolmap.Rotation3d(), frame.origin))
    return rec
