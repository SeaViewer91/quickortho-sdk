"""진행 이벤트와 작업 중단.

SDK의 처리 함수는 모두 ``on_event``(콜백)와 ``cancel``(:class:`CancelToken`) 인자를 받음.

- ``on_event``: 처리 중 :class:`Event`가 생길 때마다 호출됨. 처리와 같은 스레드에서 호출되므로 오래 걸리는 일을
  하면 처리도 그만큼 늦어짐. UI·네트워크 전송은 큐에 넣고 다른 스레드에서 처리하는 것을 권장함.
- ``cancel``: 다른 스레드에서 :meth:`CancelToken.cancel`을 호출하면, 처리 스레드가 다음 확인 지점에서
  :class:`~quickortho.errors.Cancelled`를 발생시킴.

콜백과 별개로 모든 이벤트는 표준 ``logging``의 ``"quickortho"`` 로거로도 기록됨.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ._core.protocol import Emitter
from .errors import Cancelled

logger = logging.getLogger("quickortho")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
           "error": logging.ERROR}


@dataclass(frozen=True)
class Event:
    """처리 이벤트.

    Attributes:
        type: ``"stage"``(단계 시작), ``"progress"``(진행률), ``"log"``(로그) 중 하나.
        stage: 단계 이름. 목록은 :data:`STAGES` 참고. log 이벤트에서는 ``None``.
        message: 단계 설명 또는 로그 문장 (한국어).
        current: 진행률 이벤트의 현재 값.
        total: 진행률 이벤트의 전체 값.
        level: 로그 수준 (``"info"``, ``"warn"`` 등). log 이벤트에서만 값이 있음.
    """

    type: str
    stage: Optional[str] = None
    message: str = ""
    current: Optional[int] = None
    total: Optional[int] = None
    level: Optional[str] = None

    @property
    def fraction(self) -> Optional[float]:
        """진행률(0.0~1.0). progress 이벤트가 아니면 ``None``."""
        if self.type != "progress" or not self.total:
            return None
        return max(0.0, min(1.0, (self.current or 0) / self.total))

    def to_dict(self) -> dict[str, Any]:
        """serve 프로토콜과 같은 형식의 dict."""
        if self.type == "stage":
            return {"type": "stage", "name": self.stage, "message": self.message}
        if self.type == "progress":
            return {"type": "progress", "stage": self.stage, "current": self.current, "total": self.total}
        return {"type": "log", "level": self.level, "message": self.message}


#: 단계 이름과 뜻. 이벤트의 ``stage`` 값은 이 중 하나임.
STAGES: dict[str, str] = {
    "scan": "EXIF/XMP 스캔",
    "footprints": "촬영 범위 계산 (미리보기)",
    "quicklook": "간이 모자이크 생성 (미리보기)",
    "features": "특징점 추출",
    "matching": "특징점 매칭",
    "mapping": "카메라 위치·자세 추정 (SfM)",
    "georef": "GPS 좌표 정렬",
    "dsm": "간이 DSM 생성",
    "ortho": "정사투영",
    "finalize": "COG 변환·미리보기 생성",
    "refine": "정밀 보정 (번들 조정)",
}

EventCallback = Callable[[Event], None]


class CancelToken:
    """작업 중단 요청을 전달하는 토큰. 스레드 안전함.

    예::

        token = CancelToken()
        th = threading.Thread(target=lambda: project.process(cancel=token))
        th.start()
        ...
        token.cancel()   # 처리 스레드에서 Cancelled 발생
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """중단을 요청함. 여러 번 호출해도 됨."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """중단이 요청되었는지 여부."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """중단이 요청되었으면 :class:`~quickortho.errors.Cancelled`를 발생시킴."""
        if self._event.is_set():
            raise Cancelled()


class _CallbackEmitter(Emitter):
    """코어의 Emitter 호출을 Event 콜백·로거·중단 확인으로 바꾼다."""

    def __init__(self, on_event: EventCallback | None, cancel: CancelToken | None) -> None:
        super().__init__(stream=None)
        self._on_event = on_event
        self._cancel = cancel

    def _emit(self, event: dict[str, Any]) -> None:
        if self._cancel is not None:
            self._cancel.raise_if_cancelled()
        t = event.get("type")
        if t == "stage":
            ev = Event("stage", stage=event.get("name"), message=event.get("message", ""))
            logger.info("[%s] %s", ev.stage, ev.message)
        elif t == "progress":
            ev = Event("progress", stage=event.get("stage"), current=event.get("current"), total=event.get("total"))
            logger.debug("[%s] %s/%s", ev.stage, ev.current, ev.total)
        elif t == "log":
            level = event.get("level", "info")
            ev = Event("log", message=event.get("message", ""), level=level)
            logger.log(_LEVELS.get(level, logging.INFO), ev.message)
        else:  # result·error는 SDK 경로에서 쓰지 않음
            return
        if self._on_event is not None:
            self._on_event(ev)


def make_emitter(on_event: EventCallback | None = None, cancel: CancelToken | None = None) -> Emitter:
    """SDK 내부용: 콜백과 중단 토큰을 코어 Emitter로 감싼다."""
    return _CallbackEmitter(on_event, cancel)


def print_progress(event: Event) -> None:
    """콘솔에 진행 상황을 한 줄씩 출력하는 간단한 콜백. 예제·스크립트용.

    예::

        project.process(on_event=quickortho.print_progress)
    """
    if event.type == "stage":
        print(f"▶ {event.message or STAGES.get(event.stage or '', event.stage)}", flush=True)
    elif event.type == "progress":
        frac = event.fraction
        if frac is not None and (event.current == event.total or (event.current or 0) % max(1, (event.total or 1) // 10) == 0):
            print(f"  {event.stage}: {event.current}/{event.total} ({frac:.0%})", flush=True)
    elif event.type == "log":
        prefix = "⚠ " if event.level in ("warn", "warning", "error") else "  "
        print(f"{prefix}{event.message}", flush=True)


__all__ = ["Event", "EventCallback", "CancelToken", "STAGES", "print_progress"]
