"""공통 fixture: 합성 영상 세트와, 그 세트를 한 번 처리한 워크스페이스."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import synthetic  # noqa: E402

import quickortho as qo  # noqa: E402

# GPS에 일정한 오차를 넣어 두면, GPS만으로 정렬한 결과가 그만큼 어긋나고 GCP 보정으로 바로잡히는지 확인할 수 있음
GPS_BIAS = (3.0, -2.0, 4.0)
FAST_SFM = qo.SfmOptions(max_image_size=1000)


@pytest.fixture(scope="session")
def scene(tmp_path_factory) -> synthetic.Scene:
    return synthetic.make_scene(tmp_path_factory.mktemp("scene") / "images", gps_bias=GPS_BIAS)


@pytest.fixture(scope="session")
def processed(scene, tmp_path_factory):
    """(워크스페이스 경로, 처리 중 받은 이벤트 목록, OrthoResult). 이 워크스페이스는 수정하지 않는다."""
    ws = tmp_path_factory.mktemp("processed") / "ws"
    events: list[qo.Event] = []
    res = qo.Project.create(scene.folder, ws).process(qo.OrthoOptions(sfm=FAST_SFM), on_event=events.append)
    return ws, events, res


@pytest.fixture
def workspace(processed, tmp_path) -> Path:
    """처리된 워크스페이스의 사본 (테스트마다 새로 만듦)."""
    dst = tmp_path / "ws"
    shutil.copytree(processed[0], dst)
    return dst
