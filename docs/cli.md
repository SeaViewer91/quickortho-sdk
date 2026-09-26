# 명령줄과 serve 프로토콜

SDK를 설치하면 `quickortho` 명령이 생김 (`quickortho-engine`은 데스크톱 앱 호환용 같은 명령임).
`python -m quickortho`로도 실행할 수 있음.

명령줄 도구의 용도:

- 셸 스크립트·작업 스케줄러에서 처리 실행
- 파이썬이 아닌 언어(Node.js, C#, Go 등)에서 프로세스로 호출
- 상주 모드(`serve`)로 띄워 두고 여러 작업을 차례로 처리

## 1. 출력 형식

`--format`으로 고름. 명령줄 어느 위치에 써도 됨 (`quickortho --format text ortho ...`, `quickortho ortho ... --format=text`).
환경변수 `QUICKORTHO_FORMAT`으로 기본값을 바꿀 수 있음.

| 값 | 출력 |
|---|---|
| `auto` (기본) | 표준 출력이 **터미널이면 `text`, 파이프·파일이면 `json`** |
| `text` | 사람용: 단계, 진행 막대, 결과 요약. 오류는 표준 오류로 `오류 [code]: 메시지` |
| `json` | 프로그램용: 한 줄에 JSON 이벤트 하나 (아래 표) |

다른 프로그램에서 호출하면 표준 출력이 파이프이므로 자동으로 `json`이 됨. 확실히 하려면 `--format json`을 붙임.
`serve`는 항상 `json`임.

`text` 출력 예:

```text
$ quickortho ortho flight_0925 -o out
[00:00] ▶ 영상 13장 스캔 시작
      [████████████████████████████████████████] 100%  13/13
[00:00] ▶ 특징점 추출 (13장)
      [██████████████████······················]  46%  6/13
...
  정합           13/13장, 재투영 0.88 px
  좌표계         EPSG:32652
  GSD            7.9 cm
  크기           3811 × 3688 px
  시간           41초, 최대 메모리 0.76 GB
  정사 모자이크  out/orthomosaic.tif
```

### JSON-lines 규칙

- 표준 출력에 **한 줄에 JSON 객체 하나**씩 이벤트를 씀 (UTF-8)
- 결과는 `result` 이벤트의 `data`에서 읽음
- 종료 코드: `0` 성공, `1` 처리 실패(`error` 이벤트 출력), `2` 인자 오류 (`text`·`json` 모두 같음).
  `doctor`는 실패 항목이 있으면 `error` 이벤트 없이 결과(`ok: false`)를 출력하고 `1`로 끝남
- COLMAP 내부 로그 등 그 밖의 출력은 표준 오류로 나감

| `type` | 필드 | 뜻 |
|---|---|---|
| `stage` | `name`, `message` | 단계 시작 |
| `progress` | `stage`, `current`, `total` | 진행률 |
| `log` | `level`(`info`·`warn`), `message` | 로그 |
| `result` | `command`, `data` | 명령 결과 (성공 시 마지막에 한 번) |
| `error` | `message`, `detail`(스택 트레이스), `code` | 실패 (예외 `code`와 같음, SDK 예외가 아니면 `internal_error`) |

예:

```text
$ quickortho ortho flight_0925 -o out
{"type": "stage", "name": "scan", "message": "영상 13장 스캔 시작"}
{"type": "progress", "stage": "scan", "current": 13, "total": 13}
{"type": "stage", "name": "features", "message": "특징점 추출 (13장)"}
...
{"type": "log", "level": "info", "message": "SfM 완료: 13/13장 정합, 3D 점 7624개, 재투영 오차 0.88px"}
...
{"type": "result", "command": "ortho", "data": { ...report.json과 같음... }}
```

셸에서 결과만 뽑기 (jq):

```bash
quickortho ortho flight_0925 -o out | tail -n 1 | jq '.data.outputs.orthomosaic'
```

## 2. 명령 목록

| 명령 | SDK 대응 | `result.data` |
|---|---|---|
| `version` | `qo.__version__` | `{"engine", "sdk", "protocol", "python", "os", "arch"}` |
| `doctor [폴더]` | `qo.doctor()` | 환경 진단 `{"ok", "sdk", "checks"}`. 실패 항목이 있으면 종료 코드 1 |
| `export <워크스페이스> [--cameras-csv 파일] [--cameras-json 파일] [--points-ply 파일]` | `cameras().to_csv()` 등 | 쓴 파일 경로 |
| `scan <폴더> [--recursive]` | `qo.scan()` | `ScanResult.raw` |
| `preview <폴더> -o <결과 폴더>` | `qo.preview()` | `preview.json` 내용 |
| `ortho <폴더> -o <워크스페이스>` | `Project.create().process()` | `report.json` 내용 |
| `align <폴더> -o <워크스페이스>` | `Project.create().align()` | 정렬 보고서 |
| `render <워크스페이스>` | `Project.open().orthomosaic()` | `report.json` 내용 |
| `project-info <워크스페이스>` | `Project.info()` | 프로젝트 정보. 정렬 전 폴더면 오류 없이 `{"exists": false, "epsg_presets": [...]}` |
| `tiepoints <워크스페이스>` | `Project.tiepoint_stats()` | 오차 통계 |
| `predict <워크스페이스> --spec <JSON>` | `Project.predict()` | 예측 결과 (정사 모자이크 필요) |
| `gcp-parse <파일>` | `qo.read_gcp_file()` | 파일 해석 결과 |
| `edits-save <워크스페이스> --edits <JSON>` | (수정 사항 **전체 교체**) | 저장된 수정 사항 |
| `refine <워크스페이스> [--reset]` | `Project.refine()` / `reset_refinement()` | `report.json` 내용 |
| `serve` | 상주 모드 (4절) | — |

### 명령별 옵션

`preview`:

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `-o`, `--output` | (필수) | 결과 폴더 |
| `--max-size` | 2048 | 결과 PNG 긴 변(px) |
| `--skip-quicklook` | 끔 | 간이 모자이크 없이 촬영 범위·중복도만 계산 |

`ortho`, `align`, `render` (해당 단계에 쓰는 것만 적용됨):

| 옵션 | 기본값 | 명령 | 설명 |
|---|---|---|---|
| `-o`, `--output` | (필수) | ortho, align | 워크스페이스 |
| `--gsd` | 없음 | ortho, render | 출력 GSD(m). 지정하면 `--gsd-scale`보다 우선 |
| `--gsd-scale` | 2.0 | ortho, render | 원본 GSD 대비 출력 배율 |
| `--max-image-size` | 2000 | ortho, align | 특징점 추출용 영상 긴 변(px) |
| `--max-features` | 4096 | ortho, align | 영상당 최대 특징점 수 |
| `--threads` | -1 | ortho, align | 스레드 수 (-1은 전체) |
| `--keep-work` | 끔 | ortho, align | 중간 산출물 보존 |
| `--epsg` | 없음 | ortho, align | 결과 좌표계 EPSG (투영 좌표계, 기본 UTM) |
| `--bounds XMIN YMIN XMAX YMAX` | 없음 | ortho, render | 결과 범위 (결과 좌표계) |
| `--grid-origin X Y` | 0 0 | ortho, render | 화소 격자 기준점 |
| `--dsm` | sparse | ortho, render | `sparse`, `plane`, 또는 외부 DSM GeoTIFF 경로 |
| `--dsm-z` | 없음 | ortho, render | `--dsm plane`의 높이(m) |
| `--no-dsm-vertical-align` | 끔 | ortho, render | 외부 DSM 높이 기준 자동 보정 끄기 |

`export`: 카메라 CSV는 `name,x,y,z,omega,phi,kappa`, JSON은 영상별 `K`, `dist`, `R`, `t`, `center`, `opk_deg` 등 전체,
PLY는 희소 점군(좌표 double, 색 포함). 형식은 [산출물을 변수로 쓰기](outputs.md) 참고.

`predict --spec` JSON: `{"marks": [{"image", "x", "y"}], "world": {"x", "y", "z", "epsg", "z_from_dsm"}, "chips": true}`

`gcp-parse` 옵션: `--encoding cp949`, `--delimiter ","`(`"\t"`, `";"`, `"whitespace"`), `--ortho <워크스페이스>`(좌표계 추정용)

`edits-save --edits`는 [수정 사항 형식](refinement.md#5-수정-사항-형식-editsjson) 전체를 받음. 빠진 항목은 비움
(SDK의 `set_edits()`처럼 일부만 바꾸지 않음).

### 셸 사용 예

```bash
# 현장: 누락 확인
quickortho preview /data/flight -o /data/flight_out/preview --skip-quicklook | tail -n 1 | jq '.data.gaps'

# 정렬 한 번, 해상도 두 가지
quickortho align /data/flight -o /data/flight_out
quickortho render /data/flight_out --gsd-scale 4
quickortho render /data/flight_out --gsd-scale 1

# 서버 사양에 맞춰 품질 높이기
quickortho ortho /data/flight -o /data/flight_out --max-image-size 3200 --max-features 8192

# 국내 좌표계, 고정 격자 (시기별 비교용)
quickortho ortho /data/flight -o /data/out_0925 --epsg 5186 --gsd 0.05 --bounds 204000 336000 204600 336500

# 수면 위주 영상: 해수면 높이 평면으로 정사투영
quickortho render /data/flight_out --dsm plane --dsm-z 0

# 외부 DEM 사용
quickortho render /data/flight_out --dsm /data/dem_5m.tif

# 카메라 자세·점군 내보내기, 환경 진단
quickortho export /data/flight_out --cameras-csv cams.csv --points-ply points.ply
quickortho doctor /data
```

Windows PowerShell에서 JSON 인자는 작은따옴표로 감쌈.

```powershell
quickortho predict D:\out --spec '{"marks": [], "world": {"x": 204512.3, "y": 336201.8, "z": 12.3, "epsg": 5186}}'
```

## 3. 다른 언어에서 호출하기

한 번만 실행하는 작업은 명령을 프로세스로 띄우고 표준 출력을 한 줄씩 읽으면 됨.

Node.js:

```javascript
import { spawn } from "node:child_process";
import readline from "node:readline";

const p = spawn("quickortho", ["ortho", "/data/flight", "-o", "/data/out"]);
for await (const line of readline.createInterface({ input: p.stdout })) {
  const ev = JSON.parse(line);
  if (ev.type === "progress") console.log(ev.stage, ev.current, ev.total);
  if (ev.type === "result") console.log("완료", ev.data.outputs.orthomosaic);
  if (ev.type === "error") console.error(ev.code, ev.message);
}
```

C#:

```csharp
var psi = new ProcessStartInfo("quickortho", "ortho D:\\flight -o D:\\out") {
    RedirectStandardOutput = true, StandardOutputEncoding = Encoding.UTF8 };
using var p = Process.Start(psi)!;
while (p.StandardOutput.ReadLine() is { } line) {
    using var ev = JsonDocument.Parse(line);
    Console.WriteLine(ev.RootElement.GetProperty("type").GetString());
}
```

가상환경에 설치했다면 `quickortho` 대신 가상환경의 실행 파일 경로(`.venv/bin/quickortho`, Windows는 `.venv\Scripts\quickortho.exe`)를 씀.

## 4. serve (상주 모드)

`quickortho serve`는 프로세스를 한 번 띄워 두고 표준 입력으로 작업 요청을 받아 차례로 처리함.
무거운 라이브러리를 미리 불러 두므로 작업마다 프로세스를 새로 띄우는 것보다 첫 이벤트가 빠름(1~3초 절약).

### 4.1 흐름

```text
호출 측                                     serve 프로세스
   │  (프로세스 시작)                              │
   │ ◀──── {"type":"ready","ok":true,"protocol":1,"version":"0.1.0","warmup_s":1.2}
   │ ───▶ {"job":1,"argv":["ortho","/data/a","-o","/data/a_out"]}
   │ ◀──── {"job":1,"type":"stage",...}
   │ ◀──── {"job":1,"type":"progress",...}
   │ ◀──── {"job":1,"type":"result","command":"ortho","data":{...}}
   │ ◀──── {"type":"done","job":1,"code":0}
   │ ───▶ {"job":2,"argv":["render","/data/a_out","--gsd-scale","4"]}
   │  ...
   │ (표준 입력 닫기) ───▶ 프로세스 종료
```

### 4.2 규칙

- **시작**: 첫 줄로 `ready` 이벤트를 씀. `ok`가 `false`면 `error`에 원인(라이브러리 누락 등)이 있음
- **요청**: 한 줄에 JSON 하나 `{"job": <정수>, "argv": [<명령과 인자>]}`. `argv`는 2절의 명령줄 인자와 같음
- **작업 이벤트**: 모든 이벤트에 `job` 필드가 붙음
- **끝**: 작업마다 마지막에 `{"type": "done", "job": n, "code": 0|1}`을 씀
- **순서**: 요청은 받은 순서대로 한 번에 하나씩 처리함. 처리 중에 보낸 요청은 대기함
- **잘못된 요청**: 프로세스가 죽지 않고 `error`와 `done`(`code: 1`)을 씀
  - `argv`의 명령·인자가 틀림: `error.code`는 `invalid_argument`, 이벤트에 `job`이 붙음
  - 줄이 JSON이 아니거나 `job`·`argv`가 없음: `error.code`는 `invalid_request`, `job`을 알 수 없으므로 `error`에는 `job`이 없고
    `done`은 `{"type": "done", "job": null, "code": 1}`임
- **중단**: 프로토콜에는 중단 요청이 없음. 호출 측이 프로세스를 종료하고 새로 띄움 (진행 중 결과물은 이전 상태로 남음)
- **종료**: 표준 입력을 닫으면 현재 작업을 마치고 종료함
- **`serve` 안에서 `serve`**: 거부함

### 4.3 프로토콜 버전

`ready`와 `version` 결과의 `protocol` 값(현재 1)은 이벤트·요청 형식이 **호환되지 않게** 바뀔 때만 올림.
호출 측은 시작할 때 `protocol`을 확인하고, 모르는 값이면 사용을 멈추는 것을 권장함.
필드가 추가되는 것은 호환되는 변경이므로 모르는 필드는 무시함.

### 4.4 구현 예

파이썬 클라이언트 전체 예제는 [examples/05_serve_client.py](../examples/05_serve_client.py)에 있음.
QuickOrtho 데스크톱 앱(Rust)도 같은 프로토콜로 엔진을 씀.
