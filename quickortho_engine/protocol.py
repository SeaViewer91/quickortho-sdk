"""앱(Tauri)과 엔진 사이의 JSON-lines 이벤트 출력."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


class Emitter:
    """이벤트를 한 줄에 JSON 객체 하나씩 출력한다."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def _emit(self, event: dict[str, Any]) -> None:
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

    def error(self, message: str, detail: str = "") -> None:
        self._emit({"type": "error", "message": message, "detail": detail})
