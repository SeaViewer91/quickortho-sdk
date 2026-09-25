import io
import json
from pathlib import Path

from PIL import Image

from quickortho_engine.cli import main
from quickortho_engine.scan import scan_folder, select_mapping_cameras, read_image


def _xmp(**attrs: str) -> bytes:
    body = " ".join(f'drone-dji:{k}="{v}"' for k, v in attrs.items())
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" {body}/>'
        "</rdf:RDF></x:xmpmeta>"
    )
    return xml.encode()


def _dms(deg: float) -> tuple[float, float, float]:
    d = int(deg)
    m = int((deg - d) * 60)
    sec = round((deg - d - m / 60) * 3600, 4)
    return (float(d), float(m), sec)


def make_jpeg(path: Path, *, model: str, focal: float, focal35: int, size=(64, 48),
              lat=35.1, lon=129.0, xmp: bytes | None = None, color=(100, 150, 200)) -> None:
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[271] = "DJI"
    exif[272] = model
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[37386] = focal
    exif_ifd[41989] = focal35
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = _dms(lat)
    gps[3] = "E"
    gps[4] = _dms(lon)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    data = buf.getvalue()
    if xmp:
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
        seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
        data = data[:2] + seg + data[2:]
    path.write_bytes(data)


def test_read_exif_and_xmp(tmp_path: Path):
    make_jpeg(tmp_path / "DJI_0001_W.JPG", model="M3E", focal=12.29, focal35=24,
              xmp=_xmp(RelativeAltitude="+80.10", AbsoluteAltitude="+112.5",
                       GimbalPitchDegree="-90.0", GimbalYawDegree="+15.2", RtkFlag="0"))
    r = read_image(tmp_path / "DJI_0001_W.JPG", tmp_path)
    assert r.model == "M3E"
    assert abs(r.lat - 35.1) < 1e-9 and abs(r.lon - 129.0) < 1e-9
    assert r.rel_alt == 80.1 and r.abs_alt == 112.5
    assert r.gimbal_pitch == -90.0 and r.gimbal_yaw == 15.2
    assert r.rtk_flag == 0
    assert r.flags == []


def test_oblique_and_no_xmp(tmp_path: Path):
    make_jpeg(tmp_path / "a.jpg", model="FC6310S", focal=8.8, focal35=24,
              xmp=_xmp(GimbalPitchDegree="-45.0"))
    assert "oblique" in read_image(tmp_path / "a.jpg", tmp_path).flags
    make_jpeg(tmp_path / "b.jpg", model="FC6310S", focal=8.8, focal35=24)
    assert read_image(tmp_path / "b.jpg", tmp_path).rel_alt is None


def test_wide_camera_selected_per_model(tmp_path: Path):
    for i in range(3):
        make_jpeg(tmp_path / f"DJI_{i:04d}_W.JPG", model="M3E", focal=12.29, focal35=24)
        make_jpeg(tmp_path / f"DJI_{i:04d}_T.JPG", model="M3E", focal=43.0, focal35=162, size=(40, 30))
    # 다른 기종은 그대로 유지되어야 함
    make_jpeg(tmp_path / "P4P_0001.JPG", model="FC6310S", focal=8.8, focal35=24, size=(50, 40))

    result = scan_folder(tmp_path)
    s = result["summary"]
    assert s["read_ok"] == 7 and s["selected"] == 4
    selected = {img["file"] for img in result["images"] if img["selected"]}
    assert selected == {"DJI_0000_W.JPG", "DJI_0001_W.JPG", "DJI_0002_W.JPG", "P4P_0001.JPG"}
    assert len(result["excluded_cameras"]) == 1


def test_cli_emits_json_lines(tmp_path: Path, capsys):
    make_jpeg(tmp_path / "한글 파일.jpg", model="L1D-20c", focal=10.26, focal35=28)
    assert main(["scan", str(tmp_path)]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "stage"
    assert events[-1]["type"] == "result"
    summary = events[-1]["data"]["summary"]
    assert summary["selected"] == 1 and summary["rolling_shutter_warning"] is True


def test_cli_error_event(tmp_path: Path, capsys):
    assert main(["scan", str(tmp_path / "없는폴더")]) == 1
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last["type"] == "error"
