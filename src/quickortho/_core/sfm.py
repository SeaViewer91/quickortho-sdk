"""SfM 단계: 특징점 추출 → GPS 기반 공간 매칭 → 전역(GLOMAP)/증분 매핑.

설계 기준은 맥북 에어 M1 8GB(CPU 전용)이다.
- 특징점은 긴 변 max_image_size로 축소한 영상에서, 업샘플 없이(first_octave=0) 추출한다.
- 매칭은 GPS가 가까운 k개 영상하고만 수행한다(전수 매칭 회피).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pycolmap

from .protocol import Emitter

pycolmap.logging.minloglevel = 2  # COLMAP 내부 INFO 로그 억제 (stdout은 JSON-lines 전용)


@dataclass
class SfmOptions:
    max_image_size: int = 2000
    max_num_features: int = 4096
    num_threads: int = -1
    num_neighbors: int = 15
    max_neighbor_distance_m: float = 500.0
    exhaustive_below: int = 40  # 이 장수 이하이거나 GPS가 없으면 전수 매칭
    min_registered_ratio: float = 0.8  # 전역 매핑 정합률이 이보다 낮으면 증분 매핑 재시도


class _RowCounter:
    """진행률 표시용으로 COLMAP DB의 행 수를 읽는다.

    연결을 단계 내내 열어 둔다. 읽기 연결을 매번 열고 닫으면 SQLite가 마지막 연결 종료로 판단해
    WAL 정리를 시도할 수 있고, 이것이 COLMAP 쓰기와 겹치면 비정상 종료가 날 수 있다.
    """

    def __init__(self, db_path: Path, table: str) -> None:
        self.db_path = db_path
        self.table = table
        self.con: sqlite3.Connection | None = None

    def __call__(self) -> int | None:
        try:
            if self.con is None:
                if not self.db_path.exists():
                    return None
                self.con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=0.2)
            return self.con.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
        except sqlite3.Error:
            return None

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None


def _run_with_progress(
    fn: Callable[[], None], poll: "_RowCounter", total: int, stage: str, out: Emitter
) -> None:
    """COLMAP 함수를 별도 스레드에서 실행하고, DB 행 수를 폴링해 진행률을 낸다."""
    error: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - 메인 스레드로 전달
            error.append(exc)

    th = threading.Thread(target=target, daemon=True)
    th.start()
    last = -1
    while th.is_alive():
        th.join(1.0)
        n = poll()
        if n is not None and n != last:
            out.progress(stage, min(n, total), total)
            last = n
    poll.close()
    if error:
        raise error[0]
    out.progress(stage, total, total)


def run_sfm(
    image_dir: Path,
    image_names: list[str],
    work_dir: Path,
    has_gps: bool,
    opts: SfmOptions,
    out: Emitter,
) -> tuple[pycolmap.Reconstruction, dict]:
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "database.db"
    if db_path.exists():
        db_path.unlink()
    stats: dict = {"num_input_images": len(image_names)}

    # 1) 특징점 추출
    out.stage("features", f"특징점 추출 ({len(image_names)}장)")
    t0 = time.perf_counter()
    ex = pycolmap.FeatureExtractionOptions()
    ex.max_image_size = opts.max_image_size
    ex.num_threads = opts.num_threads
    ex.sift.first_octave = 0
    ex.sift.max_num_features = opts.max_num_features
    reader = pycolmap.ImageReaderOptions()
    reader.camera_model = "OPENCV"
    _run_with_progress(
        lambda: pycolmap.extract_features(
            db_path,
            image_dir,
            image_names=image_names,
            camera_mode=pycolmap.CameraMode.AUTO,  # 같은 카메라끼리 내부표정 공유
            reader_options=reader,
            extraction_options=ex,
            device=pycolmap.Device.cpu,
        ),
        _RowCounter(db_path, "descriptors"),
        len(image_names),
        "features",
        out,
    )
    stats["time_features_s"] = time.perf_counter() - t0

    # 2) 매칭
    t0 = time.perf_counter()
    mo = pycolmap.FeatureMatchingOptions()
    mo.num_threads = opts.num_threads
    n = len(image_names)
    if has_gps and n > opts.exhaustive_below:
        k = min(opts.num_neighbors, n - 1)
        expected_pairs = n * k // 2
        out.stage("matching", f"GPS 기반 공간 매칭 (영상당 이웃 {k}장)")
        po = pycolmap.SpatialPairingOptions()
        po.max_num_neighbors = k
        po.max_distance = opts.max_neighbor_distance_m
        po.ignore_z = True
        match_fn = lambda: pycolmap.match_spatial(  # noqa: E731
            db_path, matching_options=mo, pairing_options=po, device=pycolmap.Device.cpu
        )
        stats["matching"] = "spatial"
    else:
        expected_pairs = n * (n - 1) // 2
        out.stage("matching", "전수 매칭")
        match_fn = lambda: pycolmap.match_exhaustive(  # noqa: E731
            db_path, matching_options=mo, device=pycolmap.Device.cpu
        )
        stats["matching"] = "exhaustive"
    _run_with_progress(
        match_fn, _RowCounter(db_path, "matches"), max(expected_pairs, 1), "matching", out
    )
    db = pycolmap.Database.open(db_path)
    stats["verified_pairs"] = db.num_verified_image_pairs()
    db.close()
    stats["time_matching_s"] = time.perf_counter() - t0

    # 3) 매핑: 전역(GLOMAP) 우선, 정합률이 낮으면 증분 매핑으로 재시도
    t0 = time.perf_counter()
    out.stage("mapping", "카메라 위치·자세 추정 (전역 SfM)")
    gopts = pycolmap.GlobalPipelineOptions()
    gopts.num_threads = opts.num_threads
    recs = pycolmap.global_mapping(db_path, image_dir, work_dir / "sparse_global", options=gopts)
    best = _largest(recs)
    stats["mapper"] = "global"
    if best is None or best.num_reg_images() < opts.min_registered_ratio * n:
        got = 0 if best is None else best.num_reg_images()
        out.log(f"전역 SfM 정합 {got}/{n}장 → 증분 SfM으로 재시도", level="warn")
        out.stage("mapping", "카메라 위치·자세 추정 (증분 SfM)")
        iopts = pycolmap.IncrementalPipelineOptions()
        iopts.num_threads = opts.num_threads
        recs_i = pycolmap.incremental_mapping(
            db_path, image_dir, work_dir / "sparse_incremental", options=iopts
        )
        best_i = _largest(recs_i)
        if best_i is not None and (best is None or best_i.num_reg_images() > best.num_reg_images()):
            best = best_i
            stats["mapper"] = "incremental"
    stats["time_mapping_s"] = time.perf_counter() - t0
    if best is None or best.num_reg_images() < 3:
        raise RuntimeError("SfM 실패: 정합된 영상이 3장 미만임 (중복도·초점 상태 확인 필요)")

    stats["num_registered"] = best.num_reg_images()
    stats["num_points3D"] = best.num_points3D()
    stats["mean_reprojection_error_px"] = best.compute_mean_reprojection_error()
    out.log(
        f"SfM 완료: {best.num_reg_images()}/{n}장 정합, 3D 점 {best.num_points3D()}개, "
        f"재투영 오차 {stats['mean_reprojection_error_px']:.2f}px"
    )
    return best, stats


def _largest(recs: dict) -> pycolmap.Reconstruction | None:
    if not recs:
        return None
    return max(recs.values(), key=lambda r: r.num_reg_images())
