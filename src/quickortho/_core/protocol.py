"""처리 이벤트 출력.

코어 처리 함수는 진행 상황을 :class:`Emitter`로 알린다. 출력 대상에 따라 구현을 바꿔 끼운다.

- :class:`Emitter`: 한 줄에 JSON 객체 하나씩 스트림(기본 stdout)에 쓴다. CLI·serve 모드용.
- :class:`NullEmitter`: 아무것도 출력하지 않는다. 이벤트를 받을 곳이 없을 때 쓴다.
- ``quickortho.events._CallbackEmitter``: 공개 SDK의 콜백·중단 토큰 연결용.

serve 프로토콜 버전(PROTOCOL_VERSION)은 이벤트 형식이나 요청 형식이 호환되지 않게 바뀔 때만 올린다.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

PROTOCOL_VERSION = 1


class Emitter:
    """이벤트를 한 줄에 JSON 객체 하나씩 출력한다."""

    def __init__(self, stream: TextIO | None = None, job: int | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._job = job  # serve 모드에서 이벤트가 어느 작업의 것인지 표시

    def _emit(self, event: dict[str, Any]) -> None:
        if self._job is not None:
            event = {"job": self._job, **event}
        self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._stream.flush()

    def stage(self, name: str, message: str = "") -> None:
        self._emit({"type": "stage", "name": name, "message": message})

    def progress(self, stage: str, current: int, total: int) -> None:
        self._emit({"type": "progress", "stage": stage, "current": current, "total": total})

    def log(self, message: str, level: str = "info") -> None:
        self._emit({"type": "log", "level": level, "message": message})

    def result(self, command: str, data: Any) -> None:
        self._emit({"type": "result", "command": command, "data": data})

    def error(self, message: str, detail: str = "", code: str | None = None) -> None:
        ev: dict[str, Any] = {"type": "error", "message": message, "detail": detail}
        if code is not None:
            ev["code"] = code
        self._emit(ev)


class NullEmitter(Emitter):
    """이벤트를 버린다."""

    def __init__(self) -> None:
        super().__init__(stream=None)

    def _emit(self, event: dict[str, Any]) -> None:
        return None
