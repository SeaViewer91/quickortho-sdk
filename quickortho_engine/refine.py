"""정밀 보정: 타이포인트 정리·수동 타이포인트·GCP로 SfM 결과를 다시 조정하고 정사 모자이크를 다시 만든다.

보정은 항상 project/base(최초 SfM + GPS 정렬 결과)에서 시작해 edits.json을 처음부터 적용한다.

1. 사용자가 삭제한 3D 점 제거
2. 재투영 오차가 기준보다 큰 관측 자동 제거
3. 수동 타이포인트 추가 (2장 이상에 찍은 점을 삼각측량)
4. 좌표 기준 결정
   - 기준점(control) GCP 3점 이상: 닮음변환으로 GCP 좌표계에 맞춘 뒤, GCP를 고정점으로 번들 조정
   - 기준점 1~2점: 번들 조정 → GPS 정렬 → GCP 평균 차이만큼 평행 이동
   - 기준점 없음: 번들 조정 → GPS 정렬
5. 검사점(check) GCP 오차 계산, 결과 저장, DSM·정사 모자이크 재생성
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pycolmap

from . import __version__
from .geo import Projector, crs_info, is_geographic, transform_xy
from .project import Frame, Project, to_absolute
from .protocol import Emitter
from .surface import align_to_gps

MIN_MARKS = 2


# ───────────────────────── 관측·오차 ─────────────────────────


@dataclass
class ObsTable:
    point_ids: np.ndarray  # (N,) int64
    image_ids: np.ndarray  # (N,) int64
    p2d_idx: np.ndarray  # (N,) int64
    err: np.ndarray  # (N,) 재투영 오차 px


def observation_errors(rec: pycolmap.Reconstruction) -> ObsTable:
    """모든 관측의 재투영 오차를 영상 단위로 벡터 계산한다."""
    pids, iids, idxs, errs = [], [], [], []
    for iid in rec.reg_image_ids():
        im = rec.image(iid)
        obs = im.get_observation_points2D()
        if not obs:
            continue
        idx = np.asarray(im.get_observation_point2D_idxs(), dtype=np.int64)
        pid = np.array([p.point3D_id for p in obs], dtype=np.int64)
        uv = np.array([p.xy for p in obs], dtype=np.float64)
        xyz = np.array([rec.point3D(int(q)).xyz for q in pid], dtype=np.float64)
        M = im.cam_from_world().matrix()
        xc = xyz @ M[:, :3].T + M[:, 3]
        proj = im.camera.img_from_cam(xc)
        e = np.linalg.norm(proj - uv, axis=1)
        e[~np.isfinite(e)] = 1e6  # 카메라 뒤에 있는 점
        pids.append(pid)
        iids.append(np.full(len(pid), iid, dtype=np.int64))
        idxs.append(idx)
        errs.append(e)
    if not pids:
        z = np.zeros(0, dtype=np.int64)
        return ObsTable(z, z, z, np.zeros(0))
    return ObsTable(np.concatenate(pids), np.concatenate(iids), np.concatenate(idxs), np.concatenate(errs))


def reproj_stats(rec: pycolmap.Reconstruction, exclude: set[int] | None = None) -> dict[str, float]:
    t = observation_errors(rec)
    if exclude:
        keep = ~np.isin(t.point_ids, np.fromiter(exclude, dtype=np.int64))
        t = ObsTable(t.point_ids[keep], t.image_ids[keep], t.p2d_idx[keep], t.err[keep])
    e = t.err
    if len(e) == 0:
        return {"num_points": 0, "num_observations": 0, "mean_px": 0.0, "rmse_px": 0.0, "p95_px": 0.0, "max_px": 0.0}
    return {
        "num_points": int(len(np.unique(t.point_ids))),
        "num_observations": int(len(e)),
        "mean_px": float(e.mean()),
        "rmse_px": float(np.sqrt(np.mean(e**2))),
        "p95_px": float(np.percentile(e, 95)),
        "max_px": float(e.max()),
    }


def filter_large_errors(rec: pycolmap.Reconstruction, max_px: float, protect: set[int] | None = None) -> int:
    """재투영 오차가 max_px보다 큰 관측을 지운다. 관측이 1개만 남은 점은 함께 지워진다."""
    t = observation_errors(rec)
    bad = t.err > max_px
    if protect:
        bad &= ~np.isin(t.point_ids, np.fromiter(protect, dtype=np.int64))
    n = 0
    for pid, iid, idx in zip(t.point_ids[bad], t.image_ids[bad], t.p2d_idx[bad]):
        if not rec.exists_point3D(int(pid)):
            continue
        p2d = rec.image(int(iid)).points2D[int(idx)]
        if not p2d.has_point3D() or p2d.point3D_id != pid:
            continue
        rec.delete_observation(int(iid), int(idx))
        n += 1
    rec.update_point_3d_errors()
    return n


# ───────────────────────── 점 찍기·삼각측량 ─────────────────────────


def resolve_marks(rec: pycolmap.Reconstruction, marks: list[dict]) -> list[tuple[int, np.ndarray]]:
    """[{"image": 파일명, "x", "y"}] → 정합된 영상의 (image_id, xy) 목록."""
    out = []
    seen = set()
    for m in marks:
        im = rec.find_image_with_name(str(m["image"]))
        if im is None or not im.has_pose or im.image_id in seen:
            continue
        seen.add(im.image_id)
        out.append((im.image_id, np.array([float(m["x"]), float(m["y"])])))
    return out


def triangulate(rec: pycolmap.Reconstruction, obs: list[tuple[int, np.ndarray]]) -> np.ndarray | None:
    if len(obs) < 2:
        return None
    mats, rays = [], []
    for iid, xy in obs:
        im = rec.image(iid)
        mats.append(im.cam_from_world().matrix())
        rays.append(im.camera.cam_ray_from_img(xy.reshape(1, 2))[0])
    X = pycolmap.triangulate_multi_view_point(mats, np.asarray(rays))
    if X is None:
        return None
    X = np.asarray(X, dtype=np.float64).reshape(3)
    # 모든 영상의 앞쪽에 있어야 함
    for iid, _ in obs:
        M = rec.image(iid).cam_from_world().matrix()
        if (M[:, :3] @ X + M[:, 3])[2] <= 0:
            return None
    return X


def project(rec: pycolmap.Reconstruction, iid: int, X: np.ndarray) -> np.ndarray | None:
    im = rec.image(iid)
    M = im.cam_from_world().matrix()
    xc = M[:, :3] @ X + M[:, 3]
    if xc[2] <= 0:
        return None
    uv = im.camera.img_from_cam(xc.reshape(1, 3))[0]
    return uv if np.all(np.isfinite(uv)) else None


def mark_residuals(rec: pycolmap.Reconstruction, obs: list[tuple[int, np.ndarray]], X: np.ndarray) -> list[float]:
    res = []
    for iid, xy in obs:
        uv = project(rec, iid, X)
        res.append(float(np.linalg.norm(uv - xy)) if uv is not None else float("nan"))
    return res


def add_point(rec: pycolmap.Reconstruction, X: np.ndarray, obs: list[tuple[int, np.ndarray]]) -> int:
    """관측을 영상의 2D 점으로 추가하고 3D 점을 만든다."""
    track = pycolmap.Track()
    for iid, xy in obs:
        im = rec.image(iid)
        im.points2D.append(pycolmap.Point2D(xy.reshape(2, 1)))
        track.add_element(iid, len(im.points2D) - 1)
    return int(rec.add_point3D(X, track))


# ───────────────────────── 조정 ─────────────────────────


def bundle_adjust(
    rec: pycolmap.Reconstruction, constant_points: set[int] | None = None, refine_intrinsics: bool = False
) -> dict:
    """번들 조정. GCP 없이 연직 촬영 블록에서 초점거리까지 풀면 초점거리-고도 상관으로
    높이가 크게 흔들리므로(돔 현상), 카메라 내부표정은 GCP가 있을 때만 조정한다."""
    opts = pycolmap.BundleAdjustmentOptions()
    opts.refine_focal_length = refine_intrinsics
    opts.refine_extra_params = refine_intrinsics
    opts.refine_principal_point = False
    opts.print_summary = False  # stdout은 앱과의 통신 채널이라 출력 금지
    cfg = pycolmap.BundleAdjustmentConfig()
    for iid in rec.reg_image_ids():
        cfg.add_image(iid)
    if constant_points:
        for pid in constant_points:
            cfg.add_constant_point(pid)
    else:
        cfg.fix_gauge(pycolmap.BundleAdjustmentGauge.TWO_CAMS_FROM_WORLD)
    summary = pycolmap.create_default_bundle_adjuster(opts, cfg, rec).solve()
    rec.update_point_3d_errors()
    return {"termination": str(getattr(summary, "termination_type", ""))}


def refine_intrinsics_ok(rec: pycolmap.Reconstruction, gcp_local: np.ndarray, min_points: int = 4,
                         min_relief: float = 0.02) -> bool:
    """카메라 내부표정까지 조정해도 되는지 판단한다.

    평탄한 곳의 GCP만으로는 초점거리와 촬영고도를 구분할 수 없어 높이가 오히려 틀어질 수 있으므로,
    기준점이 4점 이상이고 GCP 높이 차가 촬영고도(GCP 기준)의 2% 이상일 때만 허용한다.
    """
    if len(gcp_local) < min_points:
        return False
    cz = np.median([rec.image(i).projection_center()[2] for i in rec.reg_image_ids()])
    height = float(cz - np.median(gcp_local[:, 2]))
    relief = float(np.ptp(gcp_local[:, 2]))
    return height > 0 and relief >= min_relief * height


def umeyama(src: np.ndarray, dst: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """dst ≈ s·R·src + t 를 만족하는 닮음변환 (Umeyama 1991)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    xs, xd = src - mu_s, dst - mu_d
    cov = xd.T @ xs / len(src)
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1
    R = U @ S @ Vt
    var_s = (xs**2).sum() / len(src)
    s = float(np.trace(np.diag(D) @ S) / var_s)
    t = mu_d - s * R @ mu_s
    return s, R, t


def apply_sim3(rec: pycolmap.Reconstruction, s: float, R: np.ndarray, t: np.ndarray) -> None:
    rec.transform(pycolmap.Sim3d(float(s), pycolmap.Rotation3d(np.asarray(R, dtype=np.float64)), np.asarray(t, dtype=np.float64)))


# ───────────────────────── GCP ─────────────────────────


def target_epsg(gcps: list[dict], base_epsg: int) -> int:
    """GCP가 투영 좌표계면 그 좌표계를, 경위도거나 없으면 기존(UTM) 좌표계를 쓴다."""
    epsgs = {int(g["epsg"]) for g in gcps}
    if len(epsgs) == 1:
        e = epsgs.pop()
        if not is_geographic(e):
            return e
    return base_epsg


def gcp_in_frame(g: dict, frame: Frame) -> np.ndarray:
    x, y = transform_xy([float(g["x"])], [float(g["y"])], int(g["epsg"]), frame.epsg)
    return np.array([x[0], y[0], float(g["z"])]) - frame.origin


def _rms(v: list[float]) -> float | None:
    v = [x for x in v if x is not None and math.isfinite(x)]
    return float(math.sqrt(sum(x * x for x in v) / len(v))) if v else None


def gcp_table(rec: pycolmap.Reconstruction, gcps: list[dict], frame: Frame) -> tuple[list[dict], dict]:
    """GCP별 측량값과 추정값 차이(m), 사진 위 재투영 오차(px)."""
    rows = []
    for g in gcps:
        obs = resolve_marks(rec, g.get("obs", []))
        survey = gcp_in_frame(g, frame)
        row: dict[str, Any] = {
            "name": g["name"],
            "role": g.get("role", "control"),
            "num_marks": len(g.get("obs", [])),
            "num_used": len(obs),
            "dx": None, "dy": None, "dz": None, "dxy": None, "d3": None,
            "reproj_px": None,
        }
        X = triangulate(rec, obs)
        if X is not None:
            d = X - survey
            row.update(dx=float(d[0]), dy=float(d[1]), dz=float(d[2]),
                       dxy=float(np.hypot(d[0], d[1])), d3=float(np.linalg.norm(d)))
        if obs:
            r = mark_residuals(rec, obs, survey)
            row["reproj_px"] = _rms(r)
            row["marks"] = [
                {"image": rec.image(iid).name, "reproj_px": v if math.isfinite(v) else None}
                for (iid, _), v in zip(obs, r)
            ]
        rows.append(row)

    def summary(role: str) -> dict | None:
        rs = [r for r in rows if r["role"] == role and r["dx"] is not None]
        if not rs:
            return None
        return {
            "count": len(rs),
            "rmse_x": _rms([r["dx"] for r in rs]),
            "rmse_y": _rms([r["dy"] for r in rs]),
            "rmse_z": _rms([r["dz"] for r in rs]),
            "rmse_xy": _rms([r["dxy"] for r in rs]),
            "rmse_3d": _rms([r["d3"] for r in rs]),
        }

    return rows, {"control": summary("control"), "check": summary("check")}


# ───────────────────────── 실행 ─────────────────────────


def run_refine(ortho_dir: Path, out: Emitter, reset: bool = False) -> dict:
    from .pipeline import OrthoOptions, PeakMemory, render_products

    t_start = time.perf_counter()
    proj = Project(ortho_dir)
    proj.require()
    meta = proj.meta()
    image_dir = Path(meta["image_dir"])
    if not image_dir.is_dir():
        raise RuntimeError(f"원본 영상 폴더를 찾을 수 없음: {image_dir}")
    gps = {k: tuple(v) for k, v in meta["gps"].items()}
    edits = proj.load_edits()
    base_report = proj.base_report()
    timings: dict[str, float] = {}
    warnings: list[str] = []

    with PeakMemory() as mem:
        out.stage("refine", "보정 준비")
        rec, base_frame = proj.load_base()
        before = reproj_stats(rec)
        out.log(f"보정 전 재투영 오차 RMSE {before['rmse_px']:.3f} px, 점 {before['num_points']}개")

        if reset:
            proj.reset_refined()
            frame = base_frame
            mode = "base"
            info: dict[str, Any] = {"mode": mode}
            after = before
            gcp_rows, gcp_sum = gcp_table(rec, edits["gcps"], base_frame) if edits["gcps"] else ([], {})
        else:
            t0 = time.perf_counter()
            # 1) 삭제한 점
            n_deleted = 0
            for pid in edits.get("deleted_points", []):
                if rec.exists_point3D(int(pid)):
                    rec.delete_point3D(int(pid))
                    n_deleted += 1
            # 2) 자동 제거
            thr = edits.get("max_reproj_error_px")
            n_filtered = filter_large_errors(rec, float(thr)) if thr else 0
            if n_deleted or n_filtered:
                out.log(f"삭제 {n_deleted}점, 오차 {thr} px 초과 관측 {n_filtered}개 제거")

            # 3) 수동 타이포인트
            manual: dict[int, str] = {}
            for tp in edits.get("tiepoints", []):
                obs = resolve_marks(rec, tp.get("obs", []))
                X = triangulate(rec, obs)
                if X is None:
                    warnings.append(f"타이포인트 {tp['id']}: 정합된 사진 2장 이상에 찍어야 함 (건너뜀)")
                    continue
                manual[add_point(rec, X, obs)] = tp["id"]
            protect = set(manual)

            # 4) 좌표 기준
            gcps = edits.get("gcps", [])
            epsg_t = target_epsg(gcps, base_frame.epsg)
            control = [g for g in gcps if g.get("role", "control") == "control"]
            usable = []
            for g in control:
                obs = resolve_marks(rec, g.get("obs", []))
                if len(obs) >= MIN_MARKS and triangulate(rec, obs) is not None:
                    usable.append(g)
                else:
                    warnings.append(f"GCP {g['name']}: 정합된 사진 {MIN_MARKS}장 이상에 표시해야 기준점으로 쓸 수 있음")
            if gcps:
                xy = np.array([transform_xy([g["x"]], [g["y"]], int(g["epsg"]), epsg_t) for g in gcps]).reshape(-1, 2)
                origin_t = np.array([round(float(xy[:, 0].mean())), round(float(xy[:, 1].mean())), 0.0])
            else:
                origin_t = base_frame.origin
            frame = Frame(epsg_t, origin_t)
            info = {"epsg": epsg_t, "crs_name": crs_info(epsg_t)["name"]}

            out.stage("refine", "번들 조정")
            if len(usable) >= 3:
                src = np.array([triangulate(rec, resolve_marks(rec, g["obs"])) for g in usable])
                dst = np.array([gcp_in_frame(g, frame) for g in usable])
                s, R, t = umeyama(src, dst)
                apply_sim3(rec, s, R, t)
                gcp_pids = {add_point(rec, gcp_in_frame(g, frame), resolve_marks(rec, g["obs"])) for g in usable}
                intr = refine_intrinsics_ok(rec, dst)
                bundle_adjust(rec, constant_points=gcp_pids, refine_intrinsics=intr)
                if thr:
                    n2 = filter_large_errors(rec, float(thr), protect=protect | gcp_pids)
                    if n2:
                        bundle_adjust(rec, constant_points=gcp_pids, refine_intrinsics=intr)
                        n_filtered += n2
                for pid in gcp_pids:
                    rec.delete_point3D(pid)
                mode = "gcp"
                info.update(mode=mode, num_control=len(usable), intrinsics_refined=intr)
                vertical = "gcp"
            else:
                bundle_adjust(rec)
                if thr:
                    n2 = filter_large_errors(rec, float(thr), protect=protect)
                    if n2:
                        bundle_adjust(rec)
                        n_filtered += n2
                resid, n_al = align_to_gps(rec, gps, Projector(epsg_t), frame.origin)
                info.update(gps_residual_rms_m=resid, num_aligned=n_al)
                if usable:
                    tri = np.array([triangulate(rec, resolve_marks(rec, g["obs"])) for g in usable])
                    dst = np.array([gcp_in_frame(g, frame) for g in usable])
                    shift = (dst - tri).mean(0)
                    apply_sim3(rec, 1.0, np.eye(3), shift)
                    mode = "gcp_shift"
                    info.update(mode=mode, num_control=len(usable), shift_m=[float(v) for v in shift])
                    warnings.append("기준점이 3점 미만이라 평행 이동만 적용함. 회전·축척까지 보정하려면 3점 이상 필요")
                    vertical = "gcp"
                else:
                    mode = "gps"
                    info.update(mode=mode)
                    vertical = "gps"
            timings["adjust_s"] = time.perf_counter() - t0

            # 5) 결과 평가
            after = reproj_stats(rec)
            manual_rows = []
            for pid, tid in manual.items():
                if not rec.exists_point3D(pid):
                    continue
                p = rec.point3D(pid)
                errs = []
                for el in p.track.elements:
                    im = rec.image(el.image_id)
                    uv = project(rec, el.image_id, p.xyz)
                    xy = im.points2D[el.point2D_idx].xy
                    errs.append({"image": im.name, "reproj_px": float(np.linalg.norm(uv - xy)) if uv is not None else None})
                manual_rows.append({"id": tid, "point_id": pid, "error_px": float(p.error), "marks": errs})
            gcp_rows, gcp_sum = gcp_table(rec, gcps, frame)
            info.update(
                deleted_points=n_deleted,
                filtered_observations=n_filtered,
                max_reproj_error_px=thr,
                manual_tiepoints=manual_rows,
            )
            proj.save_refined(rec, frame, {
                "vertical": vertical,
                "mode": mode,
                "manual_points": {str(k): v for k, v in manual.items()},
            })

        out.log(f"보정 후 재투영 오차 RMSE {after['rmse_px']:.3f} px, 점 {after['num_points']}개")
        for role, label in (("control", "기준점"), ("check", "검사점")):
            s_ = gcp_sum.get(role) if gcp_sum else None
            if s_:
                out.log(f"{label} {s_['count']}점 RMSE 수평 {s_['rmse_xy']:.3f} m, 수직 {s_['rmse_z']:.3f} m")

        # 6) 정사 모자이크 재생성
        render = meta.get("render", {})
        opts = OrthoOptions(gsd_m=render.get("gsd_m"), gsd_scale=render.get("gsd_scale", 2.0))
        products = render_products(to_absolute(rec, frame), image_dir, proj.ortho_dir, frame.epsg, opts, out, timings)

    timings["total_s"] = time.perf_counter() - t_start
    report = dict(base_report)
    report.update(products)
    report["engine_version"] = __version__
    if mode != "base":
        report["georef"] = {
            **{k: v for k, v in base_report.get("georef", {}).items() if k != "note"},
            "epsg": frame.epsg,
            "mode": mode,
            **({"gps_residual_rms_m": info["gps_residual_rms_m"]} if "gps_residual_rms_m" in info else {}),
            "note": {
                "gcp": "GCP로 보정함. 정확도는 검사점 오차로 판단함",
                "gcp_shift": "GCP 3점 미만이라 평행 이동만 보정함",
                "gps": "절대 위치 정확도는 GNSS 수준(수 m)임. 정밀 위치가 필요하면 GCP 필요",
            }[mode],
        }
    report["refine"] = {
        **info,
        "before": before,
        "after": after,
        "gcps": gcp_rows,
        "gcp_summary": gcp_sum,
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "peak_memory_mb": round(mem.peak / 1024 / 1024, 1),
        "warnings": warnings,
    }
    if mode == "base":
        report.pop("refine")
    report["warnings"] = list(base_report.get("warnings", [])) + warnings
    (proj.ortho_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
