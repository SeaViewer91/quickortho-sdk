"""SfM까지 돌려 볼 수 있는 합성 드론 영상 세트.

무늬가 있는 기복 지형(가우시안 언덕)을 연직 카메라로 격자 비행하며 촬영한 JPG를 만든다.
EXIF GPS와 DJI XMP(상대고도, 짐벌 자세)를 넣으므로 실제 드론 영상과 같은 경로로 처리된다.
CI(macOS·Windows·Ubuntu)에서 실제 영상 없이 SfM → 정사투영 → GCP 보정 전체를 검증하는 데 쓴다.

좌표 규약: 지역 좌표 (E, N, Z) [m], 원점은 LON0/LAT0의 UTM 좌표.
카메라는 연직(nadir)이며, 실제 비행처럼 줄마다 방향이 뒤집히고(yaw 0°/180°) 자세가 조금씩 흔들린다(±3°).
자세가 모두 같으면(순수 평행 이동) 초점거리를 추정할 수 없는 임계 배치가 되어 SfM 결과가 틀어진다.
사진 좌표 원점은 좌상단 화소의 왼쪽 위 모서리(COLMAP 규약).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from pyproj import Transformer

LON0, LAT0 = 129.0, 35.1
EPSG = 32652  # UTM 52N
W, H = 960, 720
F_PX = 800.0
ALT = 100.0
TEX_RES = 0.08  # 무늬 격자 해상도(m)
EXTENT = (-120.0, -110.0, 180.0, 150.0)  # 무늬 영역 (xmin, ymin, xmax, ymax), 지역 좌표


@dataclass
class Scene:
    folder: Path
    cameras: dict[str, tuple[float, float, float]]  # 파일명 → 카메라 중심 (지역 E, N, Z)
    rotations: dict[str, np.ndarray]  # 파일명 → R (카메라 좌표 → 지역 좌표)
    origin: tuple[float, float]  # 지역 좌표 원점의 UTM (E, N)

    def height(self, e, n):
        return terrain(np.asarray(e, dtype=np.float64), np.asarray(n, dtype=np.float64))

    def project(self, name: str, e: float, n: float, z: float) -> tuple[float, float] | None:
        c = np.array(self.cameras[name])
        xc = self.rotations[name].T @ (np.array([e, n, z]) - c)
        if xc[2] <= 0:
            return None
        u = W / 2 + F_PX * xc[0] / xc[2]
        v = H / 2 + F_PX * xc[1] / xc[2]
        if 20 <= u < W - 20 and 20 <= v < H - 20:
            return float(u), float(v)
        return None

    def utm(self, e: float, n: float) -> tuple[float, float]:
        return self.origin[0] + e, self.origin[1] + n


def terrain(e: np.ndarray, n: np.ndarray) -> np.ndarray:
    z = 15.0 * np.exp(-(((e - 40) / 35) ** 2 + ((n - 20) / 30) ** 2))
    z += 8.0 * np.exp(-(((e + 30) / 25) ** 2 + ((n - 60) / 25) ** 2))
    z += 0.03 * e
    return z


def _texture(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    xmin, ymin, xmax, ymax = EXTENT
    w = int((xmax - xmin) / TEX_RES)
    h = int((ymax - ymin) / TEX_RES)
    img = np.zeros((h, w, 3), np.float32)
    for scale, amp in ((6, 0.35), (24, 0.35), (96, 0.3)):
        small = rng.random((h // scale + 2, w // scale + 2, 3)).astype(np.float32)
        img += amp * cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    # 모서리가 뚜렷한 도형 (특징점용)
    canvas = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    for _ in range(2500):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(6, 40))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            cv2.circle(canvas, (x, y), r, color, -1)
        else:
            cv2.rectangle(canvas, (x, y), (x + r, y + int(rng.integers(6, 40))), color, -1)
    return canvas


def rotation(yaw_deg: float, tilt_x_deg: float, tilt_y_deg: float) -> np.ndarray:
    """카메라 좌표(x 오른쪽, y 아래, z 시선) → 지역 좌표(E, N, 위) 회전. yaw는 북쪽에서 시계 방향."""
    nadir = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])  # yaw 0: 영상 위쪽 = 북쪽
    a = np.radians(-yaw_deg)
    rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    bx, by = np.radians(tilt_x_deg), np.radians(tilt_y_deg)
    rx = np.array([[1, 0, 0], [0, np.cos(bx), -np.sin(bx)], [0, np.sin(bx), np.cos(bx)]])
    ry = np.array([[np.cos(by), 0, np.sin(by)], [0, 1, 0], [-np.sin(by), 0, np.cos(by)]])
    return rz @ nadir @ rx @ ry


def _render(tex: np.ndarray, c: tuple[float, float, float], R: np.ndarray) -> np.ndarray:
    xmin, ymin, xmax, ymax = EXTENT
    u, v = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    rays = np.stack([(u - W / 2) / F_PX, (v - H / 2) / F_PX, np.ones_like(u)], axis=-1) @ R.T
    z = np.zeros(u.shape)
    for _ in range(8):  # 광선과 지형의 교점 (고정점 반복)
        t = (z - c[2]) / rays[..., 2]
        e = c[0] + t * rays[..., 0]
        n = c[1] + t * rays[..., 1]
        z = terrain(e, n)
    col = ((e - xmin) / TEX_RES).astype(np.float32)
    row = ((ymax - n) / TEX_RES).astype(np.float32)
    return cv2.remap(tex, col, row, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def _dms(deg: float):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600, 5)
    return (float(d), float(m), float(s))


def _xmp(rel_alt: float, yaw: float) -> bytes:
    attrs = {
        "RelativeAltitude": f"+{rel_alt:.2f}",
        "GimbalPitchDegree": "-90.0", "GimbalYawDegree": f"{yaw:+.1f}", "GimbalRollDegree": "+0.0",
        "FlightYawDegree": f"{yaw:+.1f}",
    }
    body = " ".join(f'drone-dji:{k}="{v}"' for k, v in attrs.items())
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" {body}/>'
        "</rdf:RDF></x:xmpmeta>"
    ).encode()


def _write_jpeg(path: Path, rgb: np.ndarray, lon: float, lat: float, rel_alt: float, yaw: float) -> None:
    exif = Image.Exif()
    exif[271] = "DJI"
    exif[272] = "SYN-TEST"
    ifd = exif.get_ifd(0x8769)
    ifd[37386] = 4.5
    ifd[41989] = int(round(F_PX / W * 36))  # 35mm 환산 초점거리
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = _dms(lat)
    gps[3] = "E"
    gps[4] = _dms(lon)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=95, exif=exif.tobytes())
    data = buf.getvalue()
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + _xmp(rel_alt, yaw)
    seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    path.write_bytes(data[:2] + seg + data[2:])


def make_scene(
    folder: Path, rows: int = 3, cols: int = 4, seed: int = 0,
    gps_bias: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Scene:
    """rows×cols 격자 비행 영상 세트를 만든다 (전방 중복 약 70%, 측방 약 60%).

    gps_bias: EXIF GPS에 더할 오차 (E, N, 고도) [m]. GCP 보정 시험용.
    """
    folder.mkdir(parents=True, exist_ok=True)
    tex = _texture(seed)
    to_utm = Transformer.from_crs(4326, EPSG, always_xy=True)
    to_ll = Transformer.from_crs(EPSG, 4326, always_xy=True)
    e0, n0 = to_utm.transform(LON0, LAT0)
    gw = ALT * W / F_PX  # 지면 폭 (평지 기준)
    gh = ALT * H / F_PX
    step_e, step_n = 0.3 * gh, 0.4 * gw  # yaw 90°라 영상 세로(gh)가 동서 방향
    rng = np.random.default_rng(seed + 1)
    cams: dict[str, tuple[float, float, float]] = {}
    rots: dict[str, np.ndarray] = {}
    k = 0
    for r in range(rows):
        yaw = 90.0 if r % 2 == 0 else 270.0  # 동서 방향 왕복 비행
        order = range(cols) if r % 2 == 0 else reversed(range(cols))
        for c in order:
            ce = c * step_e + rng.normal(0, 0.5)
            cn = r * step_n + rng.normal(0, 0.5)
            cz = ALT + rng.normal(0, 0.3)
            R = rotation(yaw + rng.normal(0, 2.0), rng.uniform(-3, 3), rng.uniform(-3, 3))
            name = f"DJI_{k:04d}.JPG"
            lon, lat = to_ll.transform(e0 + ce + gps_bias[0], n0 + cn + gps_bias[1])
            _write_jpeg(folder / name, _render(tex, (ce, cn, cz), R), lon, lat, cz + gps_bias[2], yaw)
            cams[name] = (ce, cn, cz)
            rots[name] = R
            k += 1
    return Scene(folder, cams, rots, (e0, n0))
