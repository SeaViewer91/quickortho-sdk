# 문제 해결

## 1. 오류 코드별 원인과 조치

예외의 `code`(명령줄은 `error` 이벤트의 `code`)로 찾음.

### `folder_not_found` — 영상 폴더 없음

- 경로 오타, 상대 경로의 기준(현재 작업 폴더) 확인
- Windows에서 백슬래시를 파이썬 문자열에 쓸 때는 `r"D:\flight"`처럼 raw 문자열을 씀

### `no_images` — 영상 없음

- JPG(`.jpg`, `.jpeg`)와 다중분광 밴드 TIFF(MicaSense, Pix4D 카메라 XMP가 있는 것)만 읽음. DNG·일반 TIF·HEIC는 JPG로 변환해야 함
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
`project/meta.json`의 `image_dir`에 기록된 경로를 확인할 수 있음. 다중분광은 `multispectral.source_dir`(원본 밴드 폴더)도
있어야 함 (정사 모자이크를 다시 만들 때 모든 밴드를 읽음). 다중분광 원본 폴더에 밴드 영상이 하나도 없을 때도 이 코드임.

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

- 디스크 부족 (`OSError: No space left on device`). 0.3.0부터 원본 해상도가 기본이라 결과가 0.2.x의 약 4배임.
  영상이 수백 장이면 결과가 수 GB이고, 작업 중에는 중간 GeoTIFF와 COG가 함께 있으므로 결과 예상 크기의 2배 이상 여유가 필요함.
  공간이 부족하면 `gsd_scale=2`로 만듦
- 결과 파일이 다른 프로그램(QGIS 등)에 열려 있어 교체하지 못함 (Windows에서 `PermissionError`)
- 메모리 부족 (`MemoryError`, 또는 프로세스가 강제 종료됨) → [성능과 옵션 조정](performance.md#2-메모리)

## 2. 결과 품질 문제

| 증상 | 원인 | 조치 |
|---|---|---|
| 모자이크 일부가 빠짐 | 해당 영상이 정합되지 않음 | `report.json`의 `sfm.num_registered`·경고 확인, 특징점 늘리기 |
| 건물 옆면이 보임 | 간이 DSM의 한계 | 알려진 한계임. 현장 확인·일반 매핑 용도로 씀 |
| 물체가 영상 경계선에서 약간 끊겨 보임 (수면, 갈대, 지붕선) | 심라인 합성에서 경계 양쪽 영상의 투영 위치가 DSM 높이 오차만큼 다름 | 0.2.x의 겹쳐 흐려지는 현상 대신 생기는 알려진 한계임. 바다·호수는 0.5.0의 수면 처리(`water`, 기본 auto)가 줄여 줌. 화면 대부분이 수면이면 `dsm="plane"`, 정확한 지형이 있으면 외부 DSM을 씀 |
| 물체가 여러 개로 겹쳐 흐리게 보임 (0.2.x 결과) | 영상 가중 평균 + DSM 높이 오차 | 0.3.0으로 다시 만듦 (`orthomosaic()`) |
| 가장자리가 늘어나거나 번짐 | 가장자리는 점군이 적어 DSM이 외삽됨 | 촬영 범위를 대상보다 넓게 촬영 |
| 위치가 수 m 어긋남 | GPS 오차 | GCP 보정 |
| 영상 경계를 따라 계단·다각형 모양 밝기 단차 | 노출 변화, 태양 방향에 따른 지면 반사 차이 | 0.3.1부터 자동 보정(`color_balance`, 기본 켬). 0.3.0 결과면 다시 만듦. 보정 후에도 남으면 수동 노출로 촬영 |
| 건물 옆면·물체 주변에 다른 색이 번짐 | (보정을 켠 경우) 영상마다 보이는 내용이 다른 곳의 밝기 차이 | 보통 이상값으로 빠짐. 남으면 `color_balance=False`로 비교해 보고 문제로 알려 줌 |
| 고층 건물 지붕선이 이음매에서 어긋남 | 간이 DSM 높이 오차 (밝기 문제 아님) | 알려진 한계임. 정확한 DSM이 있으면 외부 DSM을 씀 |
| 직선이 흔들림 (Mavic 2) | 롤링 셔터 | 저속 비행·정지 촬영 |
| 재투영 오차가 2px 넘음 | 흐림·롤링 셔터·움직이는 물체 | `tiepoint_stats()`로 분석 후 오차 큰 관측 정리 |

### 수면

| 증상 | 원인 | 조치 |
|---|---|---|
| 바다·호수가 이음매에서 어긋나는데 `water.active`가 `false` | `auto`는 수면 비율 평균 5% 미만이면 켜지 않음 | `water="on"`으로 다시 만듦. 정렬을 다시 하지 않아도 `orthomosaic()`에서 DSM 수면 평면은 적용됨 |
| `active`인데 `area_m2`가 0 | 후보 안에 3D 점이 많거나(파도·반사 무늬 점), 주변보다 높거나, 물가 점이 5개 미만 | `report.json`의 `water.detail`에서 `point_ratio`·`raised_m`·`shore_points` 확인. 수면을 못 찾았으면 자기 마스크(`set_water_masks`)를 넣음 |
| 청록 지붕·그늘진 도로 일부가 평면이 됨 | 색·질감이 수면과 비슷하고 그 안에 점이 적음 | 물가 띠가 그 면이라 높이도 그 면의 높이라 보통 차이가 작음. 문제가 되면 `water="off"`로 비교 |
| 수면 판별이 수면을 놓침 | 파도·반사광·탁한 물·하늘이 비친 수면 | 자기 마스크(NDWI, 분할 모델)를 `Project.set_water_masks()`로 넣고 `water="on"` |
| 화면 대부분이 바다인 영상이 정합되지 않음 | 특징점이 모자람 (알려진 한계) | 해안선이 영상의 30% 이상 보이게 촬영 계획. 앱과 함께 다음 단계 검토 중 |
| 다중분광에서 "수면 처리를 적용하지 않음" 경고 | 다중분광은 아직 지원하지 않음 | 경고만 남기고 처리는 그대로 함 |

### 다중분광

| 증상 | 원인 | 조치 |
|---|---|---|
| 경고 "패널 촬영이 있지만 … 밴드에 패널 정보가 없어" | 카메라가 일부 밴드에서 패널을 찾지 못해 패널 영역·기준 반사율을 기록하지 않음 | 패널을 그늘 없이 화면 가운데에 크게 다시 촬영. 그대로 쓰면 DLS만으로 환산하므로 반사율 수준이 크게 어긋날 수 있음 |
| `invalid_argument` "입사광 센서(DLS) 값이 없는 캡처" / "패널 촬영 영상이 없어" | `radiometry`로 지정한 방식에 필요한 자료가 없음 | `radiometry="auto"`로 두거나 있는 자료에 맞는 방식을 지정 |
| 캡처 경계마다 반사율 단차 | 구름·DLS 기울기 | `ms_normalize=True`(기본). `report.json`의 `capture_normalization`에서 보정 전·후 불일치를 확인 |
| 밴드가 어긋나 물체 가장자리에 색 테두리 | rig 미세 조정이 수렴하지 않음(무늬 없는 수면 위주 등) | `radiometry.band_alignment.residual`의 `shift_px` 확인. 0.3화소를 넘으면 잘 맞지 않은 것이므로 4절 방법으로 보고함 |
| 반사율이 음수인 화소 | 매우 어두운 곳의 잡음 (자르지 않음) | 평균·지수 계산에는 그대로 쓰고, 표시할 때만 0으로 자름 |
| 같은 폴더의 JPG를 쓰지 않는다는 경고 | 다중분광 영상이 있으면 다중분광만 처리함 | RGB는 다른 폴더로 나눠 처리 |
| 고른 지수 파일이 없음 (`indices_skipped`에 있음) | 카메라에 그 지수에 필요한 밴드가 없음 (예: Blue가 없는 카메라의 EVI, Red edge가 없는 카메라의 NDRE) | 오류가 아니라 건너뜀. 미리 `qo.preview()`의 `index_catalog`나 `qo.indices.available(bands)`로 확인 |
| `invalid_argument` "알 수 없는 지수" | `indices`에 없는 이름 | `qo.indices.names()`의 이름을 씀 (대소문자 무관). 목록에 없는 식은 `qo.indices.compute_raster()`에 `IndexDef`로 계산 |
| 0.4.0 결과의 `indices.tif`·`ndvi.png`가 없어짐 | 0.4.1부터 지수를 지수별 파일(`indices/<이름>.tif`)로 만들고, 다시 만들 때 이전 형식을 지움 | `indices/` 폴더의 파일을 씀. 여러 지수를 한 배열로는 `qo.read_indices(workspace)` |
| 지수 색이 한쪽으로 쏠린 미리보기 PNG | 미리보기 색 범위가 지수마다 고정 (`index_legend`의 `min`~`max`) | 범위 밖은 양끝 색. 분석에는 GeoTIFF 값을 씀 |

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

### `The EPSG code is unknown ... It comes from another PROJ installation`

Windows에 PostGIS·QGIS(OSGeo4W) 등을 설치하면 시스템 환경변수 `PROJ_LIB`·`PROJ_DATA`·`GDAL_DATA`가 그 프로그램의
데이터 폴더를 가리킴. rasterio가 이 값을 따라 버전이 다른 `proj.db`를 읽으면 정사 모자이크 저장 단계에서 이 오류가 남.

- 0.2.1부터 `import quickortho` 시점에 이 환경변수를 rasterio에 들어 있는 데이터 폴더로 바꿈 (이 파이썬 프로세스 안에서만).
  바꾼 변수는 `quickortho version`의 `geo_env_overridden`, `quickortho doctor`의 "좌표계 (GeoTIFF 저장)" 항목에 표시됨
- quickortho보다 rasterio를 먼저 불러오는 프로그램은 이 처리가 늦어 적용되지 않음. `import quickortho`를 먼저 함
- 정렬이 끝난 뒤 난 오류이므로 `Project.open(워크스페이스).orthomosaic()`(명령줄 `quickortho render`)로 정렬 없이 이어서 만듦

### 처리 중 `SQLite error: database is locked`로 프로세스가 종료됨

0.2.0 이하에서 특징점 추출·매칭 진행률을 읽는 연결이 COLMAP의 DB 쓰기와 겹치면 생기던 문제임. 0.2.1에서 잠금을 잡지 않는
방식(`immutable=1`)으로 바꿈. 이 때문에 진행률은 수 장 단위로 늦게 올라갈 수 있음.

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
