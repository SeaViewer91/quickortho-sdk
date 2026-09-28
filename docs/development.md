# 개발 참여

## 1. 개발 환경

필요 도구: Python 3.10~3.14, Git

```bash
git clone https://github.com/SeaViewer91/quickortho-sdk
cd quickortho-sdk
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
```

## 2. 저장소 구조

```text
src/quickortho/
├── __init__.py        공개 API 목록 (__all__)
├── _version.py        버전 (릴리스 때 수정)
├── project.py         Project, scan, preview, process
├── options.py         OrthoOptions, SfmOptions, PreviewOptions
├── results.py         결과 객체
├── events.py          Event, CancelToken, print_progress
├── geodata.py         산출물 데이터: read_orthomosaic, CameraPose, Cameras, PointCloud
├── doctor.py          환경 진단
├── textout.py         명령줄 사람용 출력 (--format text)
├── gcp.py             GCP, Mark, TiePoint, load_gcps
├── errors.py          예외
├── cli.py             명령줄·serve
└── _core/             내부 처리 코어 (공개 API 아님)
    ├── scan.py        EXIF·XMP 스캔, 카메라 선별
    ├── preview.py     빠른 미리보기
    ├── sfm.py         특징점·매칭·SfM (pycolmap)
    ├── surface.py     GPS 좌표 정렬, 간이 DSM
    ├── ortho.py       정사투영, COG
    ├── pipeline.py    정렬·정사 모자이크 단계, 보고서 조합
    ├── project.py     project/ 폴더 저장·불러오기
    ├── refine.py      번들 조정, GCP 보정
    ├── marking.py     보정용 조회 (오차 통계, 위치 예측, GCP 파일)
    ├── geo.py         좌표계 변환
    └── protocol.py    이벤트 출력(Emitter), 프로토콜 버전
tests/
├── synthetic.py       합성 드론 영상 생성기
├── conftest.py        공통 fixture (합성 영상 세트, 처리된 워크스페이스)
├── test_api.py        공개 API 전체 흐름 시험
├── test_outputs.py    산출물 변수·카메라·좌표 변환·CLI 시험
├── test_grid_dsm.py   출력 좌표계·격자 고정·평면/외부 DSM 시험
└── test_*.py          코어 단위 시험
examples/              예제
docs/                  문서, 릴리스 노트
packaging/pyinstaller/ 데스크톱 앱용 단일 폴더 번들 스펙
.github/workflows/     ci.yml (3 OS 시험), release.yml (태그 릴리스)
```

## 3. 코드 규칙

- **공개 API와 코어를 나눔**: 사용자가 쓰는 것은 `quickortho/` 최상위 모듈, 처리 구현은 `_core/`에 둠.
  공개 API에 새 이름을 넣으면 `__init__.py`의 `__all__`과 [API 레퍼런스](api-reference.md)에 함께 추가함
- **코어는 SDK 예외만 던짐**: 입력·상태 문제는 `InputError`·`ProjectError` 등 `code`가 있는 예외로 올림. 새 `code`를 만들면
  `errors.py` docstring, API 레퍼런스의 예외 표, 문제 해결 문서에 추가함
- **진행 이벤트**: 코어 함수는 `Emitter`(`out`)를 받아 `out.stage()`, `out.progress()`, `out.log()`로 알림.
  반복 구간에서는 주기적으로 `progress`를 내야 중단 요청이 반영됨
- **표준 출력 금지**: 표준 출력은 CLI의 JSON-lines 전용임. 코어에서 `print()`를 쓰지 않음.
  외부 라이브러리가 표준 출력에 쓰는 것도 막음 (예: COLMAP 번들 조정 요약은 끔)
- **메모리 기준**: 맥북 에어 M1 8GB에서 피크 약 4GB 이하. 큰 배열은 타일·창 단위로 처리함
- **문장**: 사용자에게 보이는 메시지·문서·공개 API docstring은 한글 건조체(~함, ~임)로 씀
- **의존성 라이선스**: GPL·AGPL 의존성은 추가하지 않음. SuperPoint/SuperGlue 공식 가중치(비상업 전용)도 쓰지 않음

## 4. 시험

```bash
pytest -q                     # 전체 (2코어 기준 약 1분)
pytest -q tests/test_api.py   # 공개 API만
pytest -q -k refine           # 이름으로 고르기
```

### 합성 드론 영상

실제 드론 영상은 위치 정보가 들어 있어 저장소에 넣지 않음. 대신 `tests/synthetic.py`가 SfM까지 돌려 볼 수 있는
합성 영상 세트를 만듦 (12장, 960×720, 약 2초).

- 무늬 있는 기복 지형(가우시안 언덕)을 연직 카메라로 3줄 × 4장 격자 비행 촬영
- 줄마다 방향이 뒤집히고(yaw 90°/270°) 자세가 ±3° 흔들림.
  모든 영상의 자세가 같으면(순수 평행 이동) 초점거리를 추정할 수 없는 임계 배치가 되어 SfM 결과가 틀어지므로 일부러 흔듦
- EXIF GPS·35mm 환산 초점거리, DJI XMP(상대고도, 짐벌 자세)를 넣음
- `gps_bias`로 GPS에 일정한 오차를 넣어 GCP 보정 효과를 시험함
- `Scene.project()`로 지형 위 점의 사진 좌표를 정확히 구할 수 있어, GCP 표시를 자동으로 만듦

```python
import sys; sys.path.insert(0, "tests")
import synthetic
scene = synthetic.make_scene(Path("/tmp/syn/images"), gps_bias=(3.0, -2.0, 4.0))
```

### 실제 영상 확인

릴리스 전에는 실제 드론 영상으로 한 번 이상 처리해 이전 버전 결과와 비교함 (정합 수, 재투영 오차, GPS 잔차, 출력 크기).
SfM은 스레드 실행 순서에 따라 결과가 조금씩 달라지므로 값이 완전히 같지는 않음 (정합 수는 같고, 오차는 수 % 이내).

## 5. CI

`.github/workflows/ci.yml`은 main push·PR마다 다음을 실행함.

1. wheel·sdist 빌드 (순수 파이썬 `py3-none-any`인지 확인)
2. 빌드한 **wheel을 설치해서** 시험 (소스 폴더가 아닌 설치본으로 시험해야 패키징 누락을 잡음)

| 러너 | Python |
|---|---|
| ubuntu-22.04 | 3.10 |
| ubuntu-24.04 | 3.14 |
| macos-14 (Apple Silicon) | 3.10 |
| macos-15 (Apple Silicon) | 3.14 |
| windows-2022 | 3.10 |
| windows-2025 | 3.14 |

지원 범위의 최저·최고 Python 버전을 OS마다 두 이미지 버전에서 시험함. 중간 버전(3.11~3.13)은 양 끝이 통과하면 통과하는 것으로 봄.

## 6. 버전 정책

- [유의적 버전](https://semver.org/lang/ko/)을 따름. 1.0 전에는 부 버전(0.x)에서 공개 API가 바뀔 수 있음
- 호환성 변경은 릴리스 노트의 "호환성 변경" 항목에 적고, 가능하면 한 부 버전 동안 경고(`DeprecationWarning`)를 낸 뒤 바꿈
- 다음은 별도 버전 번호를 가짐. 호환되지 않게 바뀔 때만 올림
  - `report.json`의 `report_version` (`_core/pipeline.py`의 `REPORT_VERSION`)
  - serve 프로토콜 `protocol` (`_core/protocol.py`의 `PROTOCOL_VERSION`)
  - `edits.json`의 `version` (`_core/project.py`의 `EDITS_VERSION`)

## 7. 릴리스 절차

1. `src/quickortho/_version.py`의 버전을 올림
2. `docs/release-notes/v<버전>.md`에 릴리스 노트를 작성함 (없으면 릴리스 워크플로가 실패함)
3. README·설치 문서의 wheel 주소 예시 버전을 바꿈
4. 커밋·push 후 CI가 통과하는지 확인함
5. 태그를 push함

   ```bash
   git tag v<버전>
   git push origin v<버전>
   ```

6. `release` 워크플로가 전체 시험 → 태그·버전 일치 확인 → GitHub Releases에 wheel·sdist를 게시함.
   버전에 `a`, `b`, `rc`가 들어가면 사전 릴리스로 게시함

## 8. 데스크톱 앱용 번들 (PyInstaller)

QuickOrtho 데스크톱 앱은 파이썬 없이 동작해야 하므로, SDK를 PyInstaller로 묶은 단일 폴더(onedir) 실행 파일을 포함함.

```bash
pip install pyinstaller <SDK wheel 또는 .>
cd packaging/pyinstaller
pyinstaller -y quickortho-engine.spec        # → dist/quickortho-engine/
dist/quickortho-engine/quickortho-engine version
```

- onefile 대신 onedir를 쓰는 이유: onefile은 실행할 때마다 임시 폴더에 압축을 풀어 기동이 수 초 느려짐
- 번들 크기는 약 600MB(압축 시 약 210MB)임. pycolmap, OpenCV, GDAL, BLAS 라이브러리가 대부분임
- 앱은 번들을 `serve` 모드로 띄워 [serve 프로토콜](cli.md#4-serve-상주-모드)로 통신함
- PyInstaller는 GPL이지만 번들 예외 조항이 있어 결과물의 라이선스를 제약하지 않음
