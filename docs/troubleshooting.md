# 문제 해결

## 1. 오류 코드별 원인과 조치

예외의 `code`(명령줄은 `error` 이벤트의 `code`)로 찾음.

### `folder_not_found` — 영상 폴더 없음

- 경로 오타, 상대 경로의 기준(현재 작업 폴더) 확인
- Windows에서 백슬래시를 파이썬 문자열에 쓸 때는 `r"D:\flight"`처럼 raw 문자열을 씀

### `no_images` — JPG 영상 없음

- 현재 버전은 JPG(`.jpg`, `.jpeg`)만 읽음. DNG·TIF·HEIC는 JPG로 변환해야 함
- 영상이 하위 폴더에 있으면 그 폴더를 직접 지정함 (정렬은 하위 폴더를 읽지 않음)

### `too_few_images` — 처리 대상 영상 3장 미만

- 폴더 영상이 3장 미만이거나, 카메라 자동 선별로 대부분이 빠진 경우임
- `qo.scan(folder).excluded_cameras`로 제외된 카메라를 확인함. 같은 기종의 망원 영상만 있는 폴더라면 광각 영상이 빠진 것임

### `too_few_gps` — GPS 있는 영상 3장 미만

- EXIF에 GPS가 없는 영상임. 드론 앱의 위치 기록 설정, 영상 편집 프로그램이 메타데이터를 지웠는지 확인함
- `qo.scan(folder).images`의 `flags`에 `no_gps`가 있는 영상을 확인함

### `no_gps` — (미리보기) GPS 있는 영상 없음

위와 같음.

### `sfm_failed` — 정합된 영상 3장 미만

원인 후보:

- 중복도 부족: `preview()`로 중복도와 누락을 확인함. 전방 70%, 측방 60% 이상을 권장함
- 특징점이 없는 지표: 수면, 모래사장, 눈, 균질한 농경지
- 초점 흐림·움직임 흐림, 과노출
- 영상 수가 적고 서로 떨어져 있음

조치:

- `SfmOptions(max_image_size=3000, max_num_features=8192)`로 특징점을 늘림
- `SfmOptions(exhaustive_below=200)`으로 전수 매칭을 강제함 (영상이 많으면 느림)
- `OrthoOptions(keep_work=True)`로 `work/database.db`를 남겨 COLMAP GUI로 매칭 상태를 확인함

### `georef_too_few` — GPS 있는 정합 영상 3장 미만

정합된 영상 중 GPS가 있는 영상이 3장 미만임. GPS 없는 영상만 정합되었거나 정합 자체가 적은 경우임.

### `georef_mismatch` — GPS와 SfM 배치 불일치

RANSAC(허용 오차 10m)으로 GPS와 카메라 배치를 맞추지 못함.

- 서로 다른 비행(다른 장소)의 영상이 한 폴더에 섞였는지 확인함
- GPS가 크게 튄 영상이 많은 경우 (실내 이륙, 전파 방해)
- 한 줄로만 비행해 카메라가 일직선에 가까우면 회전이 정해지지 않을 수 있음 → 두 줄 이상 비행

### `dsm_failed` — DSM을 만들 수 없음

- `dsm="sparse"`(기본): 재투영 오차 2px 이하·관측 3장 이상인 3D 점이 10개 미만임. 정합 품질이 매우 낮은 경우이므로
  `sfm_failed`와 같은 조치를 하거나, 수면 등 특징점이 없는 곳이면 `dsm="plane", dsm_z=...`로 평면을 씀
- `dsm="plane"`·외부 DSM: 3D 점이 하나도 없어 지면 높이를 정할 수 없음 → `dsm_z`를 지정함
- 외부 DSM: 촬영 범위를 전혀 덮지 않음 → 좌표계 정보와 범위를 확인함

### `file_not_found` — 파일 없음

`qo.read_raster()`, `qo.read_orthomosaic("…/파일.tif")` 등에 준 파일이 없음.

### `not_aligned` — 정렬 결과 없음

- `Project.open()`에 정렬하지 않은 폴더를 줌 → `Project.create()` 후 `align()` 또는 `process()`
- 워크스페이스 경로가 결과 폴더의 상위 폴더인지 확인함 (데스크톱 앱 결과는 `<영상 폴더>_QuickOrtho/ortho/`를 지정)

### `not_rendered` — 정사 모자이크·DSM 없음

`read_orthomosaic()`, `read_dsm()`, `predict()`, `z` 없는 `image_to_ground()`는 정사 모자이크(DSM)가 필요함.
정렬만 했다면 `orthomosaic()`을 먼저 실행함.

### `invalid_argument` — 옵션 값 오류

`OrthoOptions`의 값이 잘못됨: GSD 0 이하, 경위도 EPSG(4326 등)를 결과 좌표계로 지정, `bounds`의 최솟값이 최댓값보다 큼,
없는 외부 DSM 경로, 지원하지 않는 채널 순서 등. 메시지에 문제 항목이 나옴.

### `image_dir_missing` — 원본 영상 폴더 없음

정렬 후 영상 폴더를 옮기거나 지움. 영상 폴더를 원래 경로로 되돌리거나, 새 경로로 정렬부터 다시 함.
`project/meta.json`의 `image_dir`에 기록된 경로를 확인할 수 있음.

### `invalid_edits` — 수정 사항 형식 오류

GCP에 `name`, `x`, `y`, `z`, `epsg` 중 빠진 키가 있거나, 숫자 자리에 문자가 있음. 메시지에 문제 키가 나옴.

### `empty_file`, `invalid_argument` — GCP 파일 문제

- 파일이 비었거나 주석(`#`)만 있음
- X·Y 열을 찾지 못함 → `columns={"name": 0, "x": 2, "y": 1, "z": 3}`로 지정
- 좌표계를 추정하지 못함 → `epsg=5186` 등으로 지정. 추정은 촬영 위치와 30km 이내일 때만 성공함

### `cancelled`

`CancelToken`으로 중단함. 오류가 아님. `orthomosaic()`·`refine()`을 중단했으면 이전 결과물이 그대로 남아 있고,
`process()`를 중단했으면 정사 모자이크가 없을 수 있음 (정렬까지 끝났으면 `orthomosaic()`만 다시 실행).

### `internal_error` (명령줄)

SDK 예외가 아닌 예외임. `error` 이벤트의 `detail`(스택 트레이스)을 확인함. 흔한 원인:

- 디스크 부족 (`OSError: No space left on device`)
- 결과 파일이 다른 프로그램(QGIS 등)에 열려 있어 교체하지 못함 (Windows에서 `PermissionError`)
- 메모리 부족 (`MemoryError`, 또는 프로세스가 강제 종료됨) → [성능과 옵션 조정](performance.md#2-메모리)

## 2. 결과 품질 문제

| 증상 | 원인 | 조치 |
|---|---|---|
| 모자이크 일부가 빠짐 | 해당 영상이 정합되지 않음 | `report.json`의 `sfm.num_registered`·경고 확인, 특징점 늘리기 |
| 건물 옆면이 보이고 경계가 겹침 | 간이 DSM의 한계 | 알려진 한계임. 현장 확인·일반 매핑 용도로 씀 |
| 가장자리가 늘어나거나 번짐 | 가장자리는 점군이 적어 DSM이 외삽됨 | 촬영 범위를 대상보다 넓게 촬영 |
| 위치가 수 m 어긋남 | GPS 오차 | GCP 보정 |
| 영상 사이 밝기 차이 | 노출 변화, 태양 방향 | 수동 노출로 촬영. 현재 버전은 색 보정을 하지 않음 |
| 직선이 흔들림 (Mavic 2) | 롤링 셔터 | 저속 비행·정지 촬영 |
| 재투영 오차가 2px 넘음 | 흐림·롤링 셔터·움직이는 물체 | `tiepoint_stats()`로 분석 후 오차 큰 관측 정리 |

## 3. 설치·실행 문제

### 먼저 `quickortho doctor`

설치·실행 문제는 `quickortho doctor <작업 폴더>`로 먼저 진단함. 라이브러리 버전, 좌표 변환, 메모리·디스크, 쓰기 권한,
특징점 추출까지 한 번에 확인하고, 문제 항목을 ✗로 표시함.

### pip 설치 중 `No matching distribution found for pycolmap`

- macOS: 14 이상 Apple Silicon인지, Python이 arm64인지 확인함 (`python -c "import platform; print(platform.machine())"`)
- Linux: glibc 2.28 이상인지 확인함 (`ldd --version`). CentOS 7·Ubuntu 18.04는 지원하지 않음
- Python 버전이 3.10~3.14인지 확인함

### `import quickortho`에서 `ImportError: ... rasterio` / GDAL 충돌

conda의 GDAL과 pip의 rasterio가 섞인 경우임. 새 가상환경(venv)에서 설치하거나, conda 환경에서도 rasterio를 pip로 설치함.

### Windows에서 한글이 깨짐

- 명령줄 출력은 항상 UTF-8임. PowerShell에서 보이는 글자가 깨지면 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`
- 프로그램에서 읽을 때는 표준 출력을 UTF-8로 디코딩함 (파이썬 `encoding="utf-8"`, C# `StandardOutputEncoding = Encoding.UTF8`)

### 처음 실행이 느림

`import quickortho`는 pycolmap·GDAL 등 큰 라이브러리를 불러오므로 첫 실행에 수 초 걸림. 여러 작업을 처리하는 서버는
`quickortho serve`로 한 번 띄워 두고 재사용함.

### 처리가 멈춘 것처럼 보임

- `mapping`(SfM) 단계는 진행률 이벤트 없이 수십 초~수 분 걸릴 수 있음
- 맥북 에어는 팬이 없어 오래 처리하면 발열로 느려짐
- `logging.getLogger("quickortho").setLevel(logging.DEBUG)`로 진행 이벤트를 모두 기록해 어느 단계인지 확인함

## 4. 문제 보고

[Issues](https://github.com/SeaViewer91/quickortho-sdk/issues)에 다음을 함께 적음.

- `quickortho version` 출력
- 실패한 명령과 `error` 이벤트 전체 (또는 예외 `code`, `message`, 스택 트레이스)
- 가능하면 `report.json`과 `qo.scan(folder).summary`
- 영상은 위치 정보가 들어 있으므로 공개 이슈에 올리지 않음
