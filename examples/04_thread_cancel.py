"""별도 스레드에서 처리하고, 진행률은 큐로 받고, Ctrl+C로 중단한다.

GUI·웹 서버처럼 처리 중에도 응답해야 하는 프로그램의 기본 구조임.
콜백은 처리 스레드에서 호출되므로, 콜백에서는 큐에 넣기만 하고 표시는 메인 스레드에서 함.

실행:
    python 04_thread_cancel.py <영상 폴더> <워크스페이스>
"""

import queue
import sys
import threading

import quickortho as qo


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    project = qo.Project.create(sys.argv[1], sys.argv[2])
    events: "queue.Queue[qo.Event | None]" = queue.Queue()
    token = qo.CancelToken()
    outcome: dict = {}

    def worker() -> None:
        try:
            outcome["result"] = project.process(on_event=events.put, cancel=token)
        except qo.Cancelled:
            outcome["cancelled"] = True
        except qo.QuickOrthoError as exc:
            outcome["error"] = exc
        finally:
            events.put(None)  # 끝 표시

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    try:
        while True:
            ev = events.get()
            if ev is None:
                break
            if ev.type == "stage":
                print(f"\n[{ev.stage}] {ev.message}")
            elif ev.type == "progress" and ev.fraction is not None:
                print(f"\r  {ev.fraction:6.1%}", end="", flush=True)
            elif ev.type == "log":
                print(f"\n  {ev.message}")
    except KeyboardInterrupt:
        print("\n중단 요청함. 현재 COLMAP 계산(특징점 추출·매칭·SfM)이 끝나는 대로 멈춤...")
        token.cancel()
        th.join()

    if "result" in outcome:
        print(f"\n완료: {outcome['result'].orthomosaic}")
        return 0
    if outcome.get("cancelled"):
        print("\n중단됨. 이전 정사 모자이크가 있었다면 그대로 남아 있음")
        return 130
    exc = outcome.get("error")
    print(f"\n실패 [{exc.code}] {exc.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
