"""정밀 보정 화면용 조회 기능: 프로젝트 정보, 타이포인트 오차, 점 위치 예측·사진 조각, GCP 파일 읽기."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pycolmap
import rasterio

from .geo import crs_info, transform_xy
from .project import EDITS_VERSION, Frame, Project
from .refine import observation_errors, project, resolve_marks, triangulate
from .surface import Dsm

EPSG_PRESETS = [
    (5186, "GRS80 중부원점 (EPSG:5186)"),
    (5187, "GRS80 동부원점 (EPSG:5187)"),
    (5185, "GRS80 서부원점 (EPSG:5185)"),
    (5188, "GRS80 동해원점 (EPSG:5188)"),
    (5179, "UTM-K GRS80 (EPSG:5179)"),
    (5174, "Bessel 보정 중부원점 (EPSG:5174)"),
    (4326, "WGS84 경위도 (EPSG:4326)"),
    (32652, "WGS84 UTM 52N (EPSG:32652)"),
    (32651, "WGS84 UTM 51N (EPSG:32651)"),
]


# ───────────────────────── 프로젝트 정보 ─────────────────────────


def project_info(ortho_dir: Path) -> dict[str, Any]:
    proj = Project(ortho_dir)
    if not proj.exists():
        return {"exists": False, "epsg_presets": _presets()}
    rec, frame, source = proj.load_current()
    base, base_frame = proj.load_base()
    reg = set(base.reg_image_ids())
    images = []
    for iid in sorted(base.images, key=lambda i: base.image(i).name):
        im = base.image(iid)
        cam = im.camera
        images.append({"name": im.name, "registered": iid in reg, "width": cam.width, "height": cam.height})
    edits = proj.load_edits()
    gcp_lonlat = []
    for g in edits["gcps"]:
        lon, lat = transform_xy([g["x"]], [g["y"]], int(g["epsg"]), 4326)
        gcp_lonlat.append([float(lon[0]), float(lat[0])])
    return {
        "exists": True,
        "image_dir": str(proj.image_dir),
        "source": source,
        "frame": {**frame.to_json(), "name": crs_info(frame.epsg)["name"]},
        "base_frame": base_frame.to_json(),
        "vertical": proj.refined_extra().get("vertical", "gps"),
        "images": images,
        "edits": edits,
        "gcp_lonlat": gcp_lonlat,
        "epsg_presets": _presets(),
    }


def _presets() -> list[dict]:
    return [{"epsg": e, "label": label} for e, label in EPSG_PRESETS]


def save_edits(ortho_dir: Path, edits: dict) -> dict:
    proj = Project(ortho_dir)
    proj.require()
    clean = {
        "version": EDITS_VERSION,
        "deleted_points": sorted({int(v) for v in edits.get("deleted_points", [])}),
        "max_reproj_error_px": (
            float(edits["max_reproj_error_px"]) if edits.get("max_reproj_error_px") not in (None, "") else None
        ),
        "tiepoints": [
            {"id": str(t["id"]), "obs": [_clean_obs(o) for o in t.get("obs", [])]} for t in edits.get("tiepoints", [])
        ],
        "gcps": [
            {
                "name": str(g["name"]),
                "x": float(g["x"]), "y": float(g["y"]), "z": float(g["z"]),
                "epsg": int(g["epsg"]),
                "role": "check" if g.get("role") == "check" else "control",
                "obs": [_clean_obs(o) for o in g.get("obs", [])],
            }
            for g in edits.get("gcps", [])
        ],
    }
    proj.save_edits(clean)
    return clean


def _clean_obs(o: dict) -> dict:
    return {"image": str(o["image"]), "x": float(o["x"]), "y": float(o["y"])}


# ───────────────────────── 타이포인트 오차 ─────────────────────────


def tiepoint_stats(ortho_dir: Path, top: int = 300, max_map: int = 15000) -> dict[str, Any]:
    """현재 재구성의 점별·영상별 오차와, base 기준 자동 제거 기준별 제거량 미리보기."""
    proj = Project(ortho_dir)
    proj.require()
    rec, frame, source = proj.load_current()
    manual = {int(k): v for k, v in proj.refined_extra().get("manual_points", {}).items()}
    t = observation_errors(rec)

    # 점별 오차 (관측 오차의 RMS)
    uniq, inv = np.unique(t.point_ids, return_inverse=True)
    sq = np.bincount(inv, weights=t.err**2)
    cnt = np.bincount(inv)
    p_rmse = np.sqrt(sq / np.maximum(cnt, 1))
    p_max = np.zeros(len(uniq))
    np.maximum.at(p_max, inv, t.err)
    xyz = np.array([rec.point3D(int(p)).xyz for p in uniq]).reshape(-1, 3) + frame.origin
    lon, lat = transform_xy(xyz[:, 0], xyz[:, 1], frame.epsg, 4326)

    order = np.argsort(-p_rmse)
    worst = [
        {
            "id": int(uniq[i]),
            "error_px": round(float(p_rmse[i]), 3),
            "max_px": round(float(p_max[i]), 3),
            "track": int(cnt[i]),
            "lon": float(lon[i]), "lat": float(lat[i]), "z": round(float(xyz[i, 2]), 2),
            "manual": manual.get(int(uniq[i])),
        }
        for i in order[:top]
    ]
    # 지도 표시용: 오차 큰 점은 모두, 나머지는 균등 추출
    if len(uniq) > max_map:
        rng = np.random.default_rng(0)
        rest = rng.choice(order[top:], size=max_map - top, replace=False)
        pick = np.concatenate([order[:top], rest])
    else:
        pick = order
    map_pts = [[round(float(lon[i]), 7), round(float(lat[i]), 7), round(float(p_rmse[i]), 2), int(uniq[i])] for i in pick]

    # 영상별
    per_image = []
    for iid in rec.reg_image_ids():
        m = t.image_ids == iid
        e = t.err[m]
        per_image.append({
            "name": rec.image(iid).name,
            "num_obs": int(m.sum()),
            "rmse_px": round(float(np.sqrt(np.mean(e**2))), 3) if len(e) else None,
        })
    per_image.sort(key=lambda r: -(r["rmse_px"] or 0))

    # 자동 제거 미리보기 (base 기준, 보정은 base에서 다시 시작하므로)
    base, _ = proj.load_base()
    bt = observation_errors(base)
    edits = proj.load_edits()
    if edits["deleted_points"]:
        keep = ~np.isin(bt.point_ids, np.asarray(edits["deleted_points"], dtype=np.int64))
        be = bt.err[keep]
    else:
        be = bt.err
    thresholds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    preview = []
    for th in thresholds:
        k = be <= th
        preview.append({
            "threshold_px": th,
            "removed_obs": int((~k).sum()),
            "removed_ratio": float((~k).mean()) if len(be) else 0.0,
            "rmse_px_after": float(np.sqrt(np.mean(be[k] ** 2))) if k.any() else 0.0,
        })
    # 권장 기준: 관측을 10% 이하로 지우는 가장 작은 값 (특징점 추출 해상도에 따라 오차 수준이 달라서 분포로 정함)
    ok = [p["threshold_px"] for p in preview if p["removed_ratio"] <= 0.10]
    recommended = min(ok) if ok else thresholds[-1]
    edges = np.concatenate([np.arange(0, 5.01, 0.25), [np.inf]])
    hist, _ = np.histogram(t.err, bins=edges)

    return {
        "source": source,
        "summary": {
            "num_points": int(len(uniq)),
            "num_observations": int(len(t.err)),
            "mean_px": float(t.err.mean()) if len(t.err) else 0.0,
            "rmse_px": float(np.sqrt(np.mean(t.err**2))) if len(t.err) else 0.0,
            "p95_px": float(np.percentile(t.err, 95)) if len(t.err) else 0.0,
        },
        "histogram": {"edges": [float(e) for e in edges[:-1]], "counts": [int(c) for c in hist]},
        "threshold_preview": preview,
        "recommended_px": recommended,
        "worst": worst,
        "per_image": per_image,
        "map_points": map_pts,
        "deleted_points": edits["deleted_points"],
    }


# ───────────────────────── 점 위치 예측 ─────────────────────────


def load_dsm(path: Path) -> Dsm:
    with rasterio.open(path) as ds:
        z = ds.read(1).astype(np.float32)
        tr = ds.transform
    res = float(tr.a)
    return Dsm(z=z, x0=float(tr.c) + res / 2, y0=float(tr.f) - res / 2, res=res, num_points=0)


def ray_dsm(rec: pycolmap.Reconstruction, iid: int, xy: np.ndarray, dsm: Dsm, frame: Frame) -> np.ndarray | None:
    """사진 한 점의 광선과 DSM의 교점 (지역 좌표)."""
    im = rec.image(iid)
    ray_c = im.camera.cam_ray_from_img(xy.reshape(1, 2))[0]
    M = im.cam_from_world().matrix()
    d = M[:, :3].T @ ray_c
    c = im.projection_center()
    if d[2] >= -1e-6:
        return None
    z = float(np.median(dsm.z)) - frame.origin[2]
    for _ in range(20):
        t = (z - c[2]) / d[2]
        p = c + t * d
        z_new = float(dsm.sample(np.array([p[0] + frame.origin[0]]), np.array([p[1] + frame.origin[1]]))[0]) - frame.origin[2]
        if abs(z_new - z) < 0.01:
            z = z_new
            break
        z = 0.5 * (z + z_new)
    t = (z - c[2]) / d[2]
    return c + t * d


def predict(ortho_dir: Path, spec: dict, chip_size: int = 512, max_chips: int = 12) -> dict[str, Any]:
    """찍은 점(marks)이나 측량 좌표(world)로 3D 위치를 구하고, 그 점이 보이는 사진과 사진 좌표를 예측한다.

    spec: {"marks": [{"image","x","y"}], "world": {"x","y","z","epsg","z_from_dsm"} | None, "chips": bool}
    world.z_from_dsm가 참이면(지도에서 고른 위치 등) 높이를 DSM에서 읽는다.
    """
    proj = Project(ortho_dir)
    proj.require()
    rec, frame, _ = proj.load_current()
    marks = resolve_marks(rec, spec.get("marks", []))
    X = None
    method = None
    if len(marks) >= 2:
        X = triangulate(rec, marks)
        method = "triangulated"
    if X is None and spec.get("world"):
        w = spec["world"]
        x, y = transform_xy([float(w["x"])], [float(w["y"])], int(w["epsg"]), frame.epsg)
        X = np.array([x[0], y[0], float(w["z"])]) - frame.origin
        method = "survey"
        if w.get("z_from_dsm") or proj.refined_extra().get("vertical") != "gcp":
            # GPS 고도와 측량 표고의 기준면 차이를 피하려고 높이는 DSM에서 읽는다
            dsm = load_dsm(proj.ortho_dir / "dsm.tif")
            X[2] = float(dsm.sample(np.array([x[0]]), np.array([y[0]]))[0]) - frame.origin[2]
            method = "survey_dsm"
    if X is None and marks:
        dsm = load_dsm(proj.ortho_dir / "dsm.tif")
        X = ray_dsm(rec, marks[0][0], marks[0][1], dsm, frame)
        method = "dsm"
    if X is None:
        return {"method": None, "candidates": []}

    marked = {iid for iid, _ in marks}
    cands = []
    for iid in rec.reg_image_ids():
        im = rec.image(iid)
        uv = project(rec, iid, X)
        if uv is None:
            continue
        w, h = im.camera.width, im.camera.height
        if not (0 <= uv[0] < w and 0 <= uv[1] < h):
            continue
        dist = float(np.hypot((uv[0] - w / 2) / w, (uv[1] - h / 2) / h))
        cands.append({"image": im.name, "x": float(uv[0]), "y": float(uv[1]), "center_dist": dist,
                      "marked": iid in marked, "width": w, "height": h})
    cands.sort(key=lambda c: (not c["marked"], c["center_dist"]))

    residuals = {}
    for iid, xy in marks:
        uv = project(rec, iid, X)
        residuals[rec.image(iid).name] = float(np.linalg.norm(uv - xy)) if uv is not None else None

    abs_x = X + frame.origin
    lon, lat = transform_xy([abs_x[0]], [abs_x[1]], frame.epsg, 4326)
    result = {
        "method": method,
        "point": {"x": float(abs_x[0]), "y": float(abs_x[1]), "z": float(abs_x[2]), "epsg": frame.epsg,
                  "lon": float(lon[0]), "lat": float(lat[0])},
        "residuals_px": residuals,
        "candidates": cands,
    }
    if spec.get("chips"):
        todo = cands[:max_chips]
        chips = make_chips(proj, [(c["image"], c["x"], c["y"]) for c in todo], chip_size)
        for c in todo:
            c["chip"] = chips.get(c["image"])
    return result


def make_chips(proj: Project, items: list[tuple[str, float, float]], size: int) -> dict[str, dict]:
    """원본 해상도 사진 조각을 cache에 JPEG로 저장한다. 반환: 영상 → {path, x0, y0, size}"""
    from PIL import Image

    cache = proj.cache_dir
    image_dir = proj.image_dir

    def one(item):
        name, x, y = item
        x0 = int(round(x)) - size // 2
        y0 = int(round(y)) - size // 2
        key = hashlib.md5(f"{name}:{x0}:{y0}:{size}".encode()).hexdigest()[:16]
        path = cache / f"chip_{key}.jpg"
        if not path.exists():
            with Image.open(image_dir / name) as im:
                crop = im.crop((x0, y0, x0 + size, y0 + size)).convert("RGB")
            crop.save(path, quality=88)
        return name, {"path": str(path), "x0": x0, "y0": y0, "size": size}

    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, items))


# ───────────────────────── GCP 파일 ─────────────────────────

_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_KEYS = {
    "name": ("name", "id", "point", "pt", "no", "점명", "번호", "측점", "기준점", "명칭"),
    "e": ("e", "east", "easting", "lon", "long", "longitude", "경도", "동거"),
    "n": ("n", "north", "northing", "lat", "latitude", "위도", "남북"),
    "x": ("x",),
    "y": ("y",),
    "z": ("z", "h", "height", "elev", "elevation", "alt", "altitude", "표고", "높이", "고도", "el"),
}


def _decode(data: bytes, encoding: str | None) -> tuple[str, str]:
    for enc in ([encoding] if encoding else ["utf-8-sig", "cp949", "latin-1"]):
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _split(lines: list[str], delimiter: str) -> list[list[str]]:
    if delimiter == "whitespace":
        return [ln.split() for ln in lines]
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)]


def _sniff(lines: list[str]) -> str:
    best, best_score = "whitespace", -1.0
    for d in [",", "\t", ";", "whitespace"]:
        rows = _split(lines[:50], d)
        counts = [len(r) for r in rows if r]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        if mode < 3:
            continue
        score = counts.count(mode) / len(counts) + (0.01 if d != "whitespace" else 0)
        if score > best_score:
            best, best_score = d, score
    return best


def _is_num(s: str) -> bool:
    return bool(_NUM.match(s.replace(",", ""))) if s else False


def parse_gcp_file(
    path: Path, encoding: str | None = None, delimiter: str | None = None, ortho_dir: Path | None = None
) -> dict[str, Any]:
    """CSV·TXT 측량 성과 파일을 읽어 표 미리보기와 열·좌표계 추정값을 돌려준다."""
    data = Path(path).read_bytes()
    text, enc = _decode(data, encoding)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise RuntimeError("파일에 내용이 없음")
    delim = delimiter or _sniff(lines)
    rows = _split(lines, delim)
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]

    header = None
    if rows and sum(_is_num(c) for c in rows[0]) < sum(_is_num(c) for c in rows[min(1, len(rows) - 1)]):
        header = rows[0]
        rows = rows[1:]
    numeric = [all(_is_num(r[i]) for r in rows if r[i]) and any(r[i] for r in rows) for i in range(ncol)]

    guess = _guess_columns(header, rows, numeric)
    if ortho_dir is not None and Project(ortho_dir).exists() and guess.get("x") is not None:
        guess.update(_guess_crs(Project(ortho_dir), rows, guess))
    return {
        "encoding": enc,
        "delimiter": delim,
        "header": header,
        "num_columns": ncol,
        "numeric": numeric,
        "rows": rows[:500],
        "num_rows": len(rows),
        "guess": guess,
        "epsg_presets": _presets(),
    }


def _guess_columns(header, rows, numeric) -> dict:
    ncol = len(numeric)
    g: dict[str, Any] = {"name": None, "x": None, "y": None, "z": None}
    if header:
        low = [h.strip().lower() for h in header]
        found = {}
        for key, words in _KEYS.items():
            for i, h in enumerate(low):
                if h in words or any(h.startswith(w) and len(w) > 1 for w in words):
                    found.setdefault(key, i)
        g["name"] = found.get("name")
        if "e" in found and "n" in found:
            g["x"], g["y"] = found["e"], found["n"]
        elif "x" in found and "y" in found:
            g["x"], g["y"] = found["x"], found["y"]
        g["z"] = found.get("z")
    nums = [i for i in range(ncol) if numeric[i]]
    if g["name"] is None:
        text_cols = [i for i in range(ncol) if not numeric[i]]
        g["name"] = text_cols[0] if text_cols else (0 if nums and nums[0] == 0 and len(nums) >= 4 else None)
    rest = [i for i in nums if i != g["name"]]
    if g["x"] is None or g["y"] is None:
        if len(rest) >= 2:
            g["x"], g["y"] = rest[0], rest[1]
    if g["z"] is None:
        left = [i for i in rest if i not in (g["x"], g["y"])]
        g["z"] = left[0] if left else None
    return g


def _guess_crs(proj: Project, rows, g) -> dict:
    """프로젝트 위치와 가장 가까워지는 좌표계와 X/Y 순서를 고른다."""
    frame = proj.base_frame()
    clon, clat = transform_xy([frame.origin[0]], [frame.origin[1]], frame.epsg, 4326)
    try:
        a = np.array([float(r[g["x"]].replace(",", "")) for r in rows if r[g["x"]] and r[g["y"]]])
        b = np.array([float(r[g["y"]].replace(",", "")) for r in rows if r[g["x"]] and r[g["y"]]])
    except ValueError:
        return {}
    if len(a) == 0:
        return {}
    ma, mb = float(np.median(a)), float(np.median(b))
    best = None
    for epsg, _ in EPSG_PRESETS:
        for swap in (False, True):
            e, n = (mb, ma) if swap else (ma, mb)
            if epsg == 4326 and not (-180 <= e <= 180 and -90 <= n <= 90):
                continue
            try:
                lon, lat = transform_xy([e], [n], epsg, 4326)
            except Exception:  # noqa: BLE001
                continue
            if not (np.isfinite(lon[0]) and np.isfinite(lat[0])):
                continue
            d = _haversine_km(float(lon[0]), float(lat[0]), float(clon[0]), float(clat[0]))
            if best is None or d < best[0]:
                best = (d, epsg, swap)
    if best is None or best[0] > 30:
        return {"epsg": None, "distance_km": None if best is None else round(best[0], 2)}
    d, epsg, swap = best
    out = {"epsg": epsg, "distance_km": round(d, 3)}
    if swap:
        out["x"], out["y"] = g["y"], g["x"]
    return out


def _haversine_km(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def read_gcp_rows(parsed_rows: list[list[str]], mapping: dict) -> list[dict]:
    """열 지정에 따라 GCP 목록을 만든다 (앱에서 불러올 때 사용)."""
    out = []
    for k, r in enumerate(parsed_rows):
        try:
            x = float(r[mapping["x"]].replace(",", ""))
            y = float(r[mapping["y"]].replace(",", ""))
            z = float(r[mapping["z"]].replace(",", "")) if mapping.get("z") is not None else 0.0
        except (ValueError, IndexError):
            continue
        name = r[mapping["name"]] if mapping.get("name") is not None else f"GCP{k + 1}"
        out.append({"name": name, "x": x, "y": y, "z": z})
    return out
