# 진행률과 중단

처리에는 수십 초에서 수십 분이 걸림. 이 문서는 진행 상황을 받아 보여 주는 방법, 처리를 중단하는 방법,
GUI·웹 서버·배치 프로그램에서 SDK를 어떤 구조로 감쌀지를 정리함.

## 1. 진행 이벤트 받기

오래 걸리는 함수는 모두 `on_event` 인자로 콜백을 받음. 콜백은 `qo.Event` 하나를 인자로 받음.

```python
import quickortho as qo

def on_event(ev: qo.Event) -> None:
    if ev.type == "stage":
        print(f"[{ev.stage}] {ev.message}")
    elif ev.type == "progress":
        print(f"  {ev.stage} {ev.current}/{ev.total} ({ev.fraction:.0%})")
    elif ev.type == "log":
        print(("경고: " if ev.level == "warn" else "") + ev.message)

qo.process("flight", "out", on_event=on_event)
```

### 이벤트 종류

| `type` | 채워지는 속성 | 언제 |
|---|---|---|
| `stage` | `stage`, `message` | 단계가 시작될 때 한 번 |
| `progress` | `stage`, `current`, `total` | 단계 진행 중 (단계마다 수십~수백 회) |
| `log` | `message`, `level` | 결과 요약·경고 (예: "SfM 완료: 18/18장 정합 ...") |

### 단계 순서

`process()` 기준으로 단계는 다음 순서로 한 번씩 시작함.

```text
scan → features → matching → mapping → georef → dsm → ortho → finalize
```

- `mapping`은 전역 SfM이 실패해 증분 SfM으로 다시 시도하면 한 번 더 시작함
- `orthomosaic()`만 부르면 `dsm → ortho → finalize`
- `refine()`은 `refine → refine → dsm → ortho → finalize` (준비, 번들 조정)
- `preview()`는 `scan → footprints → quicklook`(quicklook은 `quicklook=True`일 때만)

### 전체 진행률 계산

단계마다 걸리는 시간이 크게 다르므로, 전체 진행률 막대가 필요하면 단계별 가중치를 두는 것을 권장함.
아래 가중치는 Mavic 2 Pro 18장·M1 8GB 측정치(각 단계가 전체 시간에서 차지한 비율, %)를 반올림한 값임.
영상이 많아지면 `matching`·`mapping` 비중이 커지므로 대략값으로만 씀.

```python
WEIGHTS = {"scan": 1, "features": 16, "matching": 6, "mapping": 12, "georef": 1,
           "dsm": 1, "ortho": 40, "finalize": 24}
ORDER = list(WEIGHTS)

class Overall:
    def __init__(self):
        self.done = 0.0      # 끝난 단계들의 가중치 합
        self.stage = None
        self.frac = 0.0

    def __call__(self, ev: qo.Event):
        if ev.type == "stage" and ev.stage in WEIGHTS:
            if self.stage and self.stage != ev.stage:
                self.done += WEIGHTS[self.stage]
            self.stage, self.frac = ev.stage, 0.0
        elif ev.type == "progress" and ev.stage == self.stage:
            self.frac = ev.fraction or 0.0

    @property
    def value(self) -> float:
        cur = WEIGHTS.get(self.stage, 0) * self.frac
        return min(1.0, (self.done + cur) / sum(WEIGHTS.values()))
```

`mapping`·`georef` 단계는 progress 이벤트가 없고, `dsm`·`finalize`는 단계가 끝날 때 한 번만 나오므로 그동안 값이 멈춰 있음.
처리 함수가 반환되면 1.0으로 표시함.

### 콜백 작성 규칙

- 콜백은 **처리 스레드에서 동기로** 호출됨. 콜백이 오래 걸리면 처리도 그만큼 늦어짐.
  화면 갱신·네트워크 전송은 큐에 넣고 다른 스레드에서 처리함
- 콜백에서 예외가 나면 처리도 그 예외로 멈춤. 콜백 안에서는 예외를 삼키는 것이 안전함
- `features`·`matching` 진행률은 COLMAP DB를 1초마다 읽어 계산하므로 1초 단위로 갱신됨.
  `mapping`(SfM)·`georef`는 진행률 없이 stage 이벤트만 나옴

## 2. 중단하기: CancelToken

```python
import threading
import quickortho as qo

token = qo.CancelToken()
th = threading.Thread(target=lambda: qo.process("flight", "out", cancel=token))
th.start()
...
token.cancel()      # 다른 스레드에서 호출
th.join()           # 처리 스레드에서 qo.Cancelled 발생 후 종료
```

- 처리 코드는 이벤트를 낼 때마다 토큰을 확인하고, 중단이 요청되었으면 `qo.Cancelled`를 발생시킴
- 이미 취소된 토큰으로 호출하면 처리를 시작하기 전에 즉시 `Cancelled`를 발생시킴
- 콜백 안에서 `token.cancel()`을 불러도 됨 (예: 특정 조건에서 스스로 멈추기)

### 중단이 반영되는 시점

| 구간 | 반영 시점 |
|---|---|
| `scan`, `ortho`, `quicklook` | 진행률 1% 단위 (예: 타일 500개면 타일 5개마다) |
| `dsm` | DSM 계산이 끝난 뒤 (보통 1초 이내) |
| `features`, `matching` | **현재 COLMAP 호출이 끝난 뒤**. COLMAP 계산은 중간에 끊을 수 없음 |
| `mapping` | SfM 계산이 끝난 뒤 |
| `georef` | 좌표 정렬이 끝난 뒤 (1초 이내) |
| `finalize` | COG 변환이 끝난 뒤, 결과 파일을 바꾸기 전 |

측정 예: Mavic 2 Pro 13장 처리 중 2초 만에 중단을 요청하자, 특징점 추출(약 14초)이 끝난 13.9초에 `Cancelled`가 발생함.
수백 장 처리에서는 특징점 추출·매칭이 수 분 걸릴 수 있으므로, **즉시 중단이 필요하면 프로세스 방식(4절)을 씀**.

### 중단 후 상태

- `orthomosaic()`·`refine()`: 정사 모자이크·DSM은 임시 파일에 먼저 쓰므로, 중단해도 **이전 결과물이 그대로 남고** 임시 파일은 지워짐
- `align()`·`process()`: 정렬 단계 끝에 이전 결과물을 지우므로, `process()`를 정사 모자이크 단계에서 중단하면 정사 모자이크가 없는 상태가 됨
  (정렬 결과는 남아 있으므로 `orthomosaic()`만 다시 실행하면 됨)
- 정렬(`align`) 도중 중단하면 작업 폴더(`work/`)는 지워지고, 이전 정렬 결과가 있었다면 그대로 남음.
  워크스페이스가 이전 정렬 기준이므로 `align()`을 다시 실행하는 것이 안전함

## 3. 스레드로 감싸기 (GUI·간단한 서버)

처리 중에도 화면이나 요청에 응답해야 하면 처리를 별도 스레드에서 돌림.
전체 예제는 [examples/04_thread_cancel.py](../examples/04_thread_cancel.py) 참고.

```python
import queue, threading
import quickortho as qo

class Job:
    def __init__(self, images, workspace):
        self.events: "queue.Queue[qo.Event]" = queue.Queue()
        self.token = qo.CancelToken()
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, args=(images, workspace), daemon=True)
        self.thread.start()

    def _run(self, images, workspace):
        try:
            self.result = qo.process(images, workspace, on_event=self.events.put, cancel=self.token)
        except qo.QuickOrthoError as exc:     # Cancelled 포함
            self.error = exc

    def cancel(self):
        self.token.cancel()
```

주의:

- 한 프로세스에서 여러 작업을 동시에 돌리면 CPU·메모리를 나눠 쓰므로 모두 느려짐. 작업 대기열을 두고 한 번에 하나씩 처리하는 것을 권장함
- 파이썬 스레드는 프로세스 메모리를 공유하므로, 처리 중 메모리 부족이 서버 전체에 영향을 줌

## 4. 프로세스로 감싸기 (웹 서버·즉시 중단)

처리를 별도 프로세스로 돌리면 다음 이점이 있음.

- 프로세스를 종료해 **즉시 중단**할 수 있음 (COLMAP 계산 중이어도)
- 처리 중 메모리 사용·비정상 종료가 웹 서버 프로세스에 영향을 주지 않음
- 파이썬이 아닌 언어(Node.js, C#, Rust 등)에서도 쓸 수 있음

SDK는 이를 위해 상주 모드 `quickortho serve`를 제공함. 한 번 띄워 두면 무거운 라이브러리를 미리 불러 둔 상태로
JSON-lines 요청을 차례로 처리함. QuickOrtho 데스크톱 앱도 이 방식을 씀.

```python
import json, subprocess, sys

proc = subprocess.Popen([sys.executable, "-m", "quickortho", "serve"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
ready = json.loads(proc.stdout.readline())            # {"type": "ready", "ok": true, "protocol": 1, ...}

proc.stdin.write(json.dumps({"job": 1, "argv": ["ortho", "flight", "-o", "out"]}) + "\n")
proc.stdin.flush()
for line in proc.stdout:
    ev = json.loads(line)
    ...                                               # stage·progress·log·result·error
    if ev["type"] == "done":
        break

# 즉시 중단: proc.kill() 후 새 프로세스를 띄움
```

- 프로토콜 전체는 [명령줄과 serve 프로토콜](cli.md#4-serve-상주-모드) 참고
- 재사용 가능한 클라이언트 클래스는 [examples/05_serve_client.py](../examples/05_serve_client.py) 참고
- 한 serve 프로세스는 작업을 한 번에 하나씩 처리함. 동시에 여러 작업을 돌리려면 프로세스를 여러 개 띄움

### 웹 API 구성 예

```text
[HTTP 요청] → [웹 서버: 작업 등록·조회·취소 API] → [작업 대기열] → [워커: serve 프로세스 N개]
                         ↑                                                  │
                         └──────────── 진행 이벤트(SSE/WebSocket) ◀──────────┘
```

- 워커 수는 서버 코어·메모리에 맞춰 정함 (작업 하나가 모든 코어를 씀. 8코어 32GB 서버 기준 2개 정도에서 시작)
- 작업 취소는 해당 워커 프로세스를 종료하고 새로 띄움
- 결과 파일은 워크스페이스 경로를 작업 ID로 나눠 보관하고, 보관 기간을 정해 지움
- 이 구성을 이후 버전(0.4.0 예정)에서 SDK의 작업 실행기(Job Runner)로 제공할 예정임

## 5. 로그로 남기기

콜백 없이도 모든 이벤트가 `logging`의 `"quickortho"` 로거에 기록되므로, 서버에서는 로깅 설정만으로 처리 기록을 남길 수 있음.

```python
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("quickortho").setLevel(logging.INFO)   # progress까지 보려면 DEBUG
```
