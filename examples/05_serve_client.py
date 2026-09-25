"""상주 엔진(serve) 프로세스를 띄워 작업을 맡기고, 필요하면 즉시 중단한다.

CancelToken은 COLMAP 계산이 끝나야 중단되지만, 별도 프로세스로 돌리면 프로세스를 종료해 즉시 멈출 수 있음.
처리 중 메모리를 많이 쓰는 작업을 웹 서버 프로세스와 분리하는 효과도 있음.
QuickOrtho 데스크톱 앱이 쓰는 방식과 같음. 프로토콜은 docs/cli.md 참고.

실행:
    python 05_serve_client.py <영상 폴더> <워크스페이스> [중단할 초]
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator


class EngineProcess:
    """quickortho serve 프로세스 하나. 작업은 한 번에 하나씩 처리함."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._job = 0
        self.start()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "quickortho", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )
        ready = json.loads(self._proc.stdout.readline())
        if ready.get("type") != "ready" or not ready.get("ok"):
            raise RuntimeError(f"엔진 시작 실패: {ready}")
        if ready.get("protocol") != 1:
            raise RuntimeError(f"지원하지 않는 프로토콜 버전: {ready.get('protocol')}")

    def run(self, argv: list[str]) -> Iterator[dict]:
        """작업을 보내고 이벤트를 차례로 돌려줌. 마지막은 done 이벤트."""
        assert self._proc is not None and self._proc.stdin is not None
        self._job += 1
        job = self._job
        self._proc.stdin.write(json.dumps({"job": job, "argv": argv}, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        for line in self._proc.stdout:
            ev = json.loads(line)
            if ev.get("job") != job:
                continue
            yield ev
            if ev["type"] == "done":
                return
        raise RuntimeError("엔진 프로세스가 종료됨")

    def kill_and_restart(self) -> None:
        """진행 중인 작업을 즉시 중단함 (프로세스 종료 후 새로 띄움)."""
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
        self.start()

    def close(self) -> None:
        if self._proc is not None:
            self._proc.stdin.close()
            self._proc.wait(timeout=10)


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        return 2
    images, ws = sys.argv[1], sys.argv[2]
    cancel_after = float(sys.argv[3]) if len(sys.argv) == 4 else None

    engine = EngineProcess()
    if cancel_after is not None:
        timer = threading.Timer(cancel_after, lambda: (print("\n>> 즉시 중단"), engine._proc.kill()))
        timer.start()
    t0 = time.perf_counter()
    try:
        for ev in engine.run(["ortho", images, "-o", ws]):
            if ev["type"] == "stage":
                print(f"[{time.perf_counter() - t0:6.1f}s] {ev['message']}")
            elif ev["type"] == "result":
                print(f"완료: {ev['data']['outputs']['orthomosaic']}")
            elif ev["type"] == "error":
                print(f"실패 [{ev.get('code')}] {ev['message']}")
    except RuntimeError:
        print(f"[{time.perf_counter() - t0:6.1f}s] 작업이 중단됨. 엔진을 다시 띄움")
        engine.kill_and_restart()
    engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
