# API 레퍼런스

대상 버전: 0.1.0

## 목차

1. [가져오기 규칙](#1-가져오기-규칙)
2. [폴더 함수](#2-폴더-함수): `scan`, `preview`, `process`
3. [Project](#3-project)
4. [옵션](#4-옵션): `OrthoOptions`, `SfmOptions`, `PreviewOptions`
5. [결과 객체](#5-결과-객체): `ScanResult`, `PreviewResult`, `AlignResult`, `OrthoResult`, `RefineResult`
6. [진행 이벤트와 중단](#6-진행-이벤트와-중단): `Event`, `EventCallback`, `CancelToken`, `STAGES`, `print_progress`
7. [GCP](#7-gcp): `Mark`, `GCP`, `TiePoint`, `load_gcps`, `read_gcp_file`
8. [예외](#8-예외)
9. [로깅](#9-로깅)
10. [API 안정성](#10-api-안정성)

---

## 1. 가져오기 규칙

```python
import quickortho as qo
```

- 공개 API는 `quickortho` 최상위에서 가져오는 이름뿐임 (`qo.__all__` 참고)
- `quickortho._core` 아래는 내부 구현이며 예고 없이 바뀜. 직접 쓰지 않음
- 경로 인자는 모두 `str` 또는 `pathlib.Path`를 받음. `~`(홈 폴더)는 풀어서 씀
- 오래 걸리는 함수는 모두 키워드 인자 `on_event`(진행 콜백)와 `cancel`(중단 토큰)을 받음
- 모든 함수는 **호출한 스레드에서 끝날 때까지 실행되는 동기 함수**임. 비동기 처리는 스레드·프로세스로 감쌈
  ([진행률과 중단](events-and-cancel.md) 참고)

---

## 2. 폴더 함수

### `qo.scan(folder, *, recursive=False, on_event=None, cancel=None) -> ScanResult`

영상 폴더의 EXIF·DJI XMP를 읽어 위치·고도·짐벌 자세·카메라 정보를 수집하고, 매핑용(광각) 카메라를 자동 선별함.
영상 헤더만 읽으므로 수백 장도 1초 내외임. 파일을 쓰지 않음.

| 인자 | 형식 | 설명 |
|---|---|---|
| `folder` | `str \| Path` | 영상 폴더 |
| `recursive` | `bool` | 하위 폴더까지 읽을지 여부. 기본 `False` |
| `on_event` | `EventCallback \| None` | 진행 이벤트 콜백 |
| `cancel` | `CancelToken \| None` | 중단 토큰 |

- 반환: [`ScanResult`](#scanresult)
- 예외: `InputError`(`folder_not_found`), `Cancelled`
- 읽기에 실패한 파일(손상 등)은 건너뛰고 `ScanResult.failed`에 기록함 (예외를 내지 않음)

```python
scan = qo.scan("flight_0925")
print(scan.summary["total_files"], len(scan.selected_images), scan.num_with_gps)
for im in scan.selected_images[:3]:
    print(im["file"], im["lat"], im["lon"], im["rel_alt"], im["gimbal_pitch"])
```

### `qo.preview(folder, output, options=None, *, on_event=None, cancel=None) -> PreviewResult`

SfM 없이 EXIF·XMP만으로 촬영 범위·중복도·누락 구역·간이 모자이크를 만듦. 수 초 걸림.
지면을 이륙 지점 높이의 평면으로 가정하므로 위치 오차가 수 m 수준이며, 촬영 누락 확인용임.

| 인자 | 형식 | 설명 |
|---|---|---|
| `folder` | `str \| Path` | 영상 폴더 |
| `output` | `str \| Path` | 결과 폴더 (없으면 만듦) |
| `options` | `PreviewOptions \| None` | 미리보기 옵션. 기본값은 `PreviewOptions()` |

- 결과 파일: `quicklook.png`(간이 모자이크, `quicklook=False`면 만들지 않음), `coverage.png`(중복도 지도),
  `preview.geojson`, `preview.json`. 형식은 [결과물과 보고서](report.md#3-미리보기-결과) 참고
- 반환: [`PreviewResult`](#previewresult)
- 예외: `InputError`(`folder_not_found`, `no_gps`), `Cancelled`

### `qo.process(images, workspace, options=None, *, on_event=None, cancel=None) -> OrthoResult`

`qo.Project.create(images, workspace).process(options, on_event=..., cancel=...)`와 같음. 한 줄로 정사 모자이크를 만들 때 씀.

---

## 3. Project

```python
class qo.Project(workspace, images=None)
```

영상 폴더 하나와 처리 결과 워크스페이스 하나를 다룸. 워크스페이스 구조와 상태는 [핵심 개념](concepts.md#1-워크스페이스) 참고.
보통은 생성자 대신 `Project.create()`·`Project.open()`을 씀.

### 생성·열기

#### `Project.create(images, workspace) -> Project` (클래스 메서드)

새 워크스페이스를 준비함. 워크스페이스 폴더가 없으면 만들고, 있으면 그대로 씀(다음 `align()`에서 이전 결과를 덮어씀).

- 예외: `InputError`(`folder_not_found`) — 영상 폴더가 없음

#### `Project.open(workspace) -> Project` (클래스 메서드)

정렬을 마친 기존 워크스페이스를 엶. 영상 폴더 경로는 `project/meta.json`에서 읽음.
QuickOrtho 데스크톱 앱 v0.2.0이 만든 결과 폴더(`<영상 폴더>_QuickOrtho/ortho/`)도 열 수 있음.

- 예외: `ProjectError`(`not_aligned`) — 정렬 결과가 없음

### 속성·상태

| 이름 | 형식 | 설명 |
|---|---|---|
| `workspace` | `Path` | 워크스페이스 절대 경로 |
| `images` | `Path` | 영상 폴더 절대 경로. 알 수 없으면 `ProjectError` |
| `is_aligned` | `bool` | 정렬 결과가 있는지 |
| `is_rendered` | `bool` | 정사 모자이크가 있는지 |
| `is_refined` | `bool` | 정밀 보정 결과가 있는지 |
| `gcps` | `list[GCP]` | 저장된 GCP 목록 (정렬 필요) |
| `tiepoints` | `list[TiePoint]` | 저장된 수동 타이포인트 목록 (정렬 필요) |

#### `report() -> dict`

현재 `report.json` 내용. 없으면 빈 dict. 형식은 [결과물과 보고서](report.md#2-reportjson) 참고.

#### `result() -> OrthoResult | None`

마지막 정사 모자이크 결과. 정사 모자이크가 없으면 `None`.

### 처리 메서드

#### `scan(*, recursive=False, on_event=None, cancel=None) -> ScanResult`

`qo.scan(project.images, ...)`와 같음.

#### `preview(options=None, *, output=None, on_event=None, cancel=None) -> PreviewResult`

`qo.preview(project.images, output, ...)`와 같음. `output`을 주지 않으면 `<workspace>/preview/`에 씀.

#### `align(options=None, *, on_event=None, cancel=None) -> AlignResult`

스캔 → 특징점 추출 → 매칭 → SfM → GPS 좌표 정렬을 실행하고 결과를 `project/`에 저장함.

- `options`: `OrthoOptions`. 이 단계에서는 `sfm`과 `keep_work`만 씀
- 이전 정렬·보정 결과와 정사 모자이크 결과물(`orthomosaic.tif`, `dsm.tif`, `preview.png`, `report.json`)은 지움.
  마지막 정사 모자이크 옵션(`meta.json`의 `render`)도 비우므로, 다시 정렬한 뒤 바로 `refine()`을 부르면 기본 옵션으로 정사 모자이크를 만듦.
  GCP·타이포인트의 사진 표시는 유지함 ([무엇이 언제 지워지는가](concepts.md#무엇이-언제-지워지는가))
- 끝나면 `report.json`에 정렬 보고서만 씀 (정사 모자이크 항목 없음)
- 반환: [`AlignResult`](#alignresult)
- 예외:

| 예외 | code | 원인 |
|---|---|---|
| `InputError` | `folder_not_found` | 영상 폴더가 없음 |
| `InputError` | `no_images` | JPG 영상이 없음 |
| `InputError` | `too_few_images` | 처리 대상 영상이 3장 미만 |
| `InputError` | `too_few_gps` | GPS가 있는 영상이 3장 미만 |
| `AlignmentError` | `sfm_failed` | 정합된 영상이 3장 미만 |
| `AlignmentError` | `georef_too_few` | GPS가 있는 정합 영상이 3장 미만 |
| `AlignmentError` | `georef_mismatch` | GPS 배치와 SfM 카메라 배치가 맞지 않음 |
| `Cancelled` | `cancelled` | 중단됨 |

#### `orthomosaic(options=None, *, on_event=None, cancel=None) -> OrthoResult`

현재 정렬 결과(보정했으면 보정 결과)로 간이 DSM과 정사 모자이크를 만듦. SfM을 다시 하지 않으므로 해상도만 바꿀 때 빠름.

- `options`: `OrthoOptions`. 이 단계에서는 `gsd_m`, `gsd_scale`, `cache_budget_mb`만 씀
- 여기서 쓴 옵션은 `project/meta.json`에 저장되어, 이후 `refine()`이 정사 모자이크를 다시 만들 때도 같은 옵션을 씀
- 결과물은 임시 파일에 먼저 쓰고 마지막에 교체하므로, 중단·실패해도 이전 결과물이 유지됨
- 반환: [`OrthoResult`](#orthoresult)
- 예외: `ProjectError`(`not_aligned`), `InputError`(`image_dir_missing`: 원본 영상 폴더가 없어짐),
  `ProcessingError`(`dsm_failed`: 유효한 3D 점이 10개 미만), `Cancelled`

```python
project = qo.Project.open("out")
for scale in (4, 2, 1):
    r = project.orthomosaic(qo.OrthoOptions(gsd_scale=scale))
    print(scale, r.gsd_m, r.width, r.height)
```

#### `process(options=None, *, on_event=None, cancel=None) -> OrthoResult`

`align()` 후 `orthomosaic()`을 실행함. 예외는 두 메서드의 예외를 합친 것과 같음.
정렬 결과는 메모리에서 바로 넘기므로 두 메서드를 따로 부르는 것보다 조금 빠름.

### 정밀 보정 메서드

보정 흐름과 데이터 형식은 [정밀 보정](refinement.md)에 자세히 정리함.

#### `get_edits() -> dict`

저장된 보정 수정 사항(`project/edits.json`)을 dict로 돌려줌.

```python
{
  "version": 1,
  "deleted_points": [1532, 1771],          # 보정 때 지울 3D 점 ID
  "max_reproj_error_px": 2.0,              # 이보다 큰 관측 자동 제거. None이면 사용 안 함
  "tiepoints": [{"id": "T1", "obs": [{"image": "DJI_0013.JPG", "x": 2310.5, "y": 1520.0}, ...]}],
  "gcps": [{"name": "G1", "x": 204512.3, "y": 336201.8, "z": 12.35, "epsg": 5186,
            "role": "control", "obs": [{"image": "...", "x": ..., "y": ...}]}]
}
```

#### `set_edits(*, gcps=..., tiepoints=..., max_reproj_error_px=..., deleted_points=...) -> dict`

수정 사항 중 **지정한 항목만** 바꿔 저장하고, 검증·정리된 전체 수정 사항을 돌려줌.

| 인자 | 형식 | 설명 |
|---|---|---|
| `gcps` | `Iterable[GCP \| dict] \| None` | GCP 목록 전체를 바꿈. `None`이면 비움 |
| `tiepoints` | `Iterable[TiePoint \| dict] \| None` | 수동 타이포인트 목록 전체를 바꿈. `None`이면 비움 |
| `max_reproj_error_px` | `float \| None` | 자동 제거 기준(px). `None`이면 사용 안 함 |
| `deleted_points` | `Iterable[int] \| None` | 지울 3D 점 ID. `None`이면 비움 |

- 예외: `ProjectError`(`not_aligned`), `InputError`(`invalid_edits`: 필수 키 누락·형식 오류)

```python
project.set_edits(max_reproj_error_px=2.0)          # 자동 제거 기준만 바꿈 (GCP는 그대로)
project.set_edits(gcps=gcps)                         # GCP만 바꿈
project.set_edits(deleted_points=None)               # 삭제 목록 비움
```

#### `refine(*, on_event=None, cancel=None) -> RefineResult`

저장된 수정 사항으로 번들 조정을 하고 DSM·정사 모자이크를 다시 만듦.
항상 최초 정렬 결과(base)에서 시작해 수정 사항을 처음부터 적용하므로 여러 번 실행해도 결과가 누적되지 않음.

좌표 기준은 사용할 수 있는 기준점(정합 사진 2장 이상에 표시한 `role="control"` GCP) 수로 정해짐.

| 기준점 | `mode` | 방식 |
|---|---|---|
| 3점 이상 | `gcp` | 닮음변환으로 GCP 좌표계에 맞춘 뒤 GCP를 고정점으로 번들 조정 |
| 1~2점 | `gcp_shift` | 번들 조정 → GPS 정렬 → GCP 평균 차이만큼 평행 이동 |
| 0점 | `gps` | 내부표정 고정 번들 조정 → GPS 재정렬 |

- 반환: [`RefineResult`](#refineresult)
- 예외: `ProjectError`(`not_aligned`), `InputError`(`image_dir_missing`), `ProcessingError`(`dsm_failed`), `Cancelled`

#### `reset_refinement(*, on_event=None, cancel=None) -> OrthoResult`

보정 결과를 지우고 최초 정렬 결과로 정사 모자이크를 다시 만듦. 수정 사항(`edits.json`)은 남겨 둠.

- 예외: `refine()`과 같음

#### `info() -> dict`

보정 화면 구성용 프로젝트 정보.

| 키 | 설명 |
|---|---|
| `exists` | 항상 `True` (정렬 안 된 워크스페이스는 `ProjectError`) |
| `image_dir` | 영상 폴더 |
| `source` | 현재 재구성: `"base"` 또는 `"refined"` |
| `frame` | 현재 좌표계 `{"epsg", "origin", "name"}` |
| `base_frame` | 정렬 직후 좌표계 `{"epsg", "origin"}` |
| `vertical` | 높이 기준: `"gps"` 또는 `"gcp"` |
| `images` | `[{"name", "registered", "width", "height"}]` |
| `edits` | `get_edits()`와 같음 |
| `gcp_lonlat` | GCP 순서대로 `[경도, 위도]` |
| `epsg_presets` | 자주 쓰는 좌표계 목록 `[{"epsg", "label"}]` (국내 좌표계 포함) |

#### `tiepoint_stats(top=300) -> dict`

현재 재구성의 타이포인트(3D 점) 재투영 오차 통계. 오차 큰 점을 지우거나 자동 제거 기준을 정할 때 씀.

| 키 | 설명 |
|---|---|
| `source` | `"base"` 또는 `"refined"` |
| `summary` | `{"num_points", "num_observations", "mean_px", "rmse_px", "p95_px"}` |
| `histogram` | 관측 오차 분포 `{"edges": [0, 0.25, ..., 5.0], "counts": [...]}` (마지막 칸은 5px 이상) |
| `threshold_preview` | 기준별 제거량 미리보기 `[{"threshold_px", "removed_obs", "removed_ratio", "rmse_px_after"}]` (base 기준) |
| `recommended_px` | 권장 자동 제거 기준: 관측을 10% 이하로 지우는 가장 작은 값 |
| `worst` | 오차 큰 점 `top`개 `[{"id", "error_px", "max_px", "track", "lon", "lat", "z", "manual"}]` |
| `per_image` | 영상별 `[{"name", "num_obs", "rmse_px"}]` (오차 큰 순) |
| `map_points` | 지도 표시용 `[[경도, 위도, 오차px, 점ID], ...]` (최대 15000개) |
| `deleted_points` | 현재 삭제 목록 |

#### `predict(marks=(), world=None, *, chips=False) -> dict`

표시한 점 또는 측량 좌표로 3D 위치를 구하고, 그 점이 보이는 사진과 사진 좌표를 예측함. GCP 표시 도구에서 후보 사진을 고를 때 씀.
DSM을 쓰므로 **정사 모자이크를 만든 워크스페이스**에서만 쓸 수 있음.

| 인자 | 설명 |
|---|---|
| `marks` | 이미 표시한 사진 좌표 (`Mark` 또는 dict). 2장 이상이면 삼각측량, 1장이면 DSM과 교차 |
| `world` | 측량 좌표 `{"x", "y", "z", "epsg", "z_from_dsm"(선택)}`. `marks`로 위치를 못 구할 때 씀 |
| `chips` | `True`면 후보 사진(최대 12장)마다 원본 해상도 512px 조각 JPG를 `project/cache/`에 만듦 |

반환:

| 키 | 설명 |
|---|---|
| `method` | `"triangulated"`, `"survey"`, `"survey_dsm"`(높이를 DSM에서 읽음), `"dsm"`, 위치를 못 구하면 `None` |
| `point` | `{"x", "y", "z", "epsg", "lon", "lat"}` (현재 좌표계) |
| `residuals_px` | 표시한 사진별 재투영 오차 |
| `candidates` | `[{"image", "x", "y", "center_dist", "marked", "width", "height", "chip"?}]` 표시한 사진 먼저, 그다음 사진 중심에 가까운 순 |

`chip`은 `{"path", "x0", "y0", "size"}`이며, 조각 안의 좌표 `(cx, cy)`는 원본 좌표 `(x0 + cx, y0 + cy)`에 해당함.
위치를 구하지 못하면(`method`가 `None`) `{"method": None, "candidates": []}`만 돌려줌.

- 예외: `ProjectError`(`not_aligned`, `not_rendered`: DSM 없음)

- GCP 보정 전(`vertical="gps"`)에는 측량 표고와 GPS 고도의 기준면이 다를 수 있어, `world`로 예측할 때 높이를 DSM에서 읽음

---

## 4. 옵션

모든 옵션은 dataclass이며 `to_dict()`로 dict를 얻을 수 있음. 기본값만으로 동작함.

### `OrthoOptions`

| 필드 | 기본값 | 쓰는 단계 | 설명 |
|---|---|---|---|
| `gsd_m` | `None` | orthomosaic | 출력 GSD(m/화소). 지정하면 `gsd_scale`보다 우선함 |
| `gsd_scale` | `2.0` | orthomosaic | `gsd_m`이 없을 때 원본 GSD에 곱할 배율 |
| `keep_work` | `False` | align | COLMAP DB·희소 재구성 원본을 `<workspace>/work/`에 남김 (문제 분석용) |
| `cache_budget_mb` | `600` | orthomosaic | 정사투영 중 축소 영상 캐시 메모리 상한(MB) |
| `sfm` | `SfmOptions()` | align | SfM 옵션 |

### `SfmOptions`

| 필드 | 기본값 | 설명 |
|---|---|---|
| `max_image_size` | `2000` | 특징점 추출 전 영상 긴 변(px). 키우면 정밀해지지만 느려지고 메모리를 더 씀 |
| `max_num_features` | `4096` | 영상당 최대 특징점 수 |
| `num_threads` | `-1` | 스레드 수. `-1`은 모든 코어 |
| `num_neighbors` | `15` | 공간 매칭에서 영상마다 매칭할 가까운 영상 수 |
| `max_neighbor_distance_m` | `500.0` | 공간 매칭 이웃의 최대 거리(m) |
| `exhaustive_below` | `40` | 영상 수가 이 값 이하면 전수 매칭 |
| `min_registered_ratio` | `0.8` | 전역 SfM 정합률이 이보다 낮으면 증분 SfM으로 다시 시도 |

조정 방법은 [성능과 옵션 조정](performance.md)을 참고함.

### `PreviewOptions`

| 필드 | 기본값 | 설명 |
|---|---|---|
| `max_size` | `2048` | 결과 PNG 긴 변(px) |
| `quicklook` | `True` | `False`면 영상을 디코딩하지 않고 촬영 범위·중복도·누락만 계산함 (1초 내외) |

---

## 5. 결과 객체

모든 결과 객체는 불변(frozen) dataclass이며, 원본 dict를 `raw`(또는 `report`)로 함께 가짐.
원본 dict는 JSON으로 그대로 저장·전송할 수 있음.

### `ScanResult`

| 속성 | 형식 | 설명 |
|---|---|---|
| `folder` | `Path` | 스캔한 폴더 |
| `images` | `list[dict]` | 영상별 정보 (아래 표) |
| `cameras` | `dict[str, dict]` | 카메라별 요약. 키는 `"제조사 모델 \| 초점거리 \| 가로x세로"` |
| `summary` | `dict` | `total_files`, `read_ok`, `read_failed`, `selected`, `no_gps`, `oblique`, `rtk_fixed`, `rolling_shutter_warning` |
| `excluded_cameras` | `dict[str, str]` | 제외한 카메라 → 사유 |
| `failed` | `list[dict]` | 읽기 실패 `[{"file", "error"}]` |
| `selected_images` | `list[dict]` | 처리 대상 영상만 (속성) |
| `num_with_gps` | `int` | 처리 대상 중 GPS 있는 영상 수 (속성) |
| `raw` | `dict` | 원본 |

영상별 정보(`images[i]`):

| 키 | 설명 |
|---|---|
| `file` | 폴더 기준 상대 경로 (`/` 구분) |
| `width`, `height` | 화소 크기 |
| `make`, `model` | EXIF 제조사·모델 (예: `Hasselblad`, `L1D-20c`) |
| `datetime` | 촬영 시각 (EXIF 문자열) |
| `focal_mm`, `focal_35mm` | 초점거리, 35mm 환산 초점거리 |
| `lat`, `lon` | 위도·경도 (EXIF GPS, 없으면 XMP) |
| `abs_alt` | 절대 고도 (XMP `AbsoluteAltitude` 우선, 없으면 EXIF GPS 고도) |
| `rel_alt` | 이륙 지점 기준 고도 (XMP `RelativeAltitude`) |
| `gimbal_yaw`, `gimbal_pitch`, `gimbal_roll` | 짐벌 자세(도). pitch -90이 연직 |
| `flight_yaw` | 기체 방향(도) |
| `rtk_flag` | DJI RTK 상태 (50 = Fix) |
| `camera_key` | 카메라 식별 문자열 |
| `selected` | 처리 대상 여부 |
| `flags` | `"no_gps"`, `"oblique"` 등 |

### `PreviewResult`

| 속성 | 형식 | 설명 |
|---|---|---|
| `output_dir` | `Path` | 결과 폴더 |
| `quicklook` | `Path \| None` | 간이 모자이크 PNG (`quicklook=False`면 `None`) |
| `coverage` | `Path` | 중복도 지도 PNG |
| `geojson` | `Path` | 촬영 범위·누락·저중복·촬영 위치 GeoJSON |
| `corners_lonlat` | `list[list[float]]` | 두 PNG의 네 모서리 경위도 [좌상, 우상, 우하, 좌하] |
| `num_images` | `int` | 쓴 영상 수 |
| `num_gaps` | `int` | 누락 구역 수 |
| `coverage_stats` | `dict` | `survey_area_m2`, `gap_area_m2`, `low_overlap_area_m2`, `overlap_median`, `overlap_p10` |
| `warnings` | `list[str]` | 경고 |
| `raw` | `dict` | `preview.json`과 같음 |

### `AlignResult`

| 속성 | 형식 | 설명 |
|---|---|---|
| `num_images` | `int` | SfM에 넣은 영상 수 |
| `num_registered` | `int` | 정합된 영상 수 |
| `num_points` | `int` | 희소 3D 점 수 |
| `reprojection_error_px` | `float` | 평균 재투영 오차(px) |
| `epsg` | `int` | 좌표계 (UTM) |
| `gps_residual_m` | `float` | 카메라 위치와 GPS의 수평 RMS 차이(m) |
| `warnings` | `list[str]` | 경고 |
| `raw` | `dict` | `project/align_report.json`과 같음 |

### `OrthoResult`

| 속성 | 형식 | 설명 |
|---|---|---|
| `orthomosaic` | `Path` | 정사 모자이크 GeoTIFF (COG, RGBA) |
| `dsm` | `Path` | 간이 DSM GeoTIFF (float32) |
| `preview` | `Path` | 미리보기 PNG |
| `report_path` | `Path` | `report.json` |
| `epsg` | `int` | 결과 좌표계 |
| `gsd_m` | `float` | GSD(m/화소) |
| `width`, `height` | `int` | 화소 크기 |
| `corners_lonlat` | `list[list[float]]` | 네 모서리 경위도 [좌상, 우상, 우하, 좌하] |
| `refined` | `bool` | 보정 결과로 만든 것인지 |
| `warnings` | `list[str]` | 경고 (정렬 경고 + 보정 경고) |
| `total_time_s` | `float` | 정렬 + 마지막 정사 모자이크 시간(초) (속성) |
| `peak_memory_mb` | `float` | 최대 메모리(MB) (속성) |
| `report` | `dict` | `report.json`과 같음 |

### `RefineResult`

| 속성 | 형식 | 설명 |
|---|---|---|
| `mode` | `str` | `"gcp"`, `"gcp_shift"`, `"gps"` |
| `rmse_before_px`, `rmse_after_px` | `float \| None` | 보정 전후 재투영 RMSE |
| `control` | `dict \| None` | 기준점 오차 `{"count", "rmse_x", "rmse_y", "rmse_z", "rmse_xy", "rmse_3d"}` (m) |
| `check` | `dict \| None` | 검사점 오차 (형식은 `control`과 같음) |
| `gcps` | `list[dict]` | GCP별 `{"name", "role", "num_marks", "num_used", "dx", "dy", "dz", "dxy", "d3", "reproj_px", "marks"}` |
| `ortho` | `OrthoResult` | 다시 만든 정사 모자이크 |
| `warnings` | `list[str]` | 보정 경고 (표시가 부족한 GCP 등) |
| `raw` | `dict` | `report.json`의 `refine` 항목 |

`dx`, `dy`, `dz`는 **추정 위치 - 측량 위치**(m)이며, 삼각측량할 수 없는 점은 `None`임.

---

## 6. 진행 이벤트와 중단

자세한 사용 방식은 [진행률과 중단](events-and-cancel.md)에 정리함.

### `Event`

| 속성 | 형식 | 설명 |
|---|---|---|
| `type` | `str` | `"stage"`(단계 시작), `"progress"`(진행률), `"log"`(로그) |
| `stage` | `str \| None` | 단계 이름 (`STAGES`의 키). log 이벤트는 `None` |
| `message` | `str` | 단계 설명·로그 문장 (한국어) |
| `current`, `total` | `int \| None` | progress 이벤트의 현재·전체 값 |
| `level` | `str \| None` | log 이벤트의 수준: `"info"`, `"warn"` |
| `fraction` | `float \| None` | 진행률 0.0~1.0 (progress가 아니면 `None`) (속성) |

`to_dict()`는 serve 프로토콜과 같은 형식의 dict를 돌려줌.

### `EventCallback`

`Callable[[Event], None]`. 처리와 **같은 스레드**에서 동기로 호출됨. 콜백에서 예외가 나면 처리도 그 예외로 멈춤.

### `CancelToken`

| 멤버 | 설명 |
|---|---|
| `cancel()` | 중단 요청. 어느 스레드에서나 호출 가능, 여러 번 호출해도 됨 |
| `cancelled` | 중단 요청 여부 (속성) |
| `raise_if_cancelled()` | 요청되었으면 `Cancelled` 발생 |

토큰 하나를 여러 작업에 넘기면 한 번에 모두 중단할 수 있음. 한 번 취소한 토큰은 되돌릴 수 없으므로 새 작업에는 새 토큰을 씀.

### `STAGES`

단계 이름 → 한국어 설명 dict. 키: `scan`, `footprints`, `quicklook`, `features`, `matching`, `mapping`, `georef`,
`dsm`, `ortho`, `finalize`, `refine`.

### `print_progress(event)`

콘솔에 진행 상황을 출력하는 간단한 콜백. 예제·스크립트용.

---

## 7. GCP

GCP 보정 흐름은 [정밀 보정](refinement.md)을 참고함.

### `Mark(image, x, y)`

사진 위에 표시한 점. `x`, `y`는 **원본 사진 화소 좌표**이며, 원점은 좌상단 화소의 왼쪽 위 모서리, x는 오른쪽, y는 아래쪽임.
좌상단 화소의 중심은 `(0.5, 0.5)`임 (COLMAP 규약).

### `GCP(name, x, y, z, epsg, role="control", marks=[])`

| 필드 | 설명 |
|---|---|
| `name` | 점 이름 |
| `x`, `y` | 측량 좌표. **x는 동쪽 방향 값(Easting), y는 북쪽 방향 값(Northing)**. 경위도 좌표계면 x=경도, y=위도 |
| `z` | 표고(m) |
| `epsg` | 측량 좌표계 EPSG (예: 5186) |
| `role` | `"control"`(기준점) 또는 `"check"`(검사점) |
| `marks` | 사진 표시 목록 (`Mark`) |

국내 측량 성과표는 보통 X가 북쪽(Northing), Y가 동쪽(Easting) 값임. SDK의 `x`, `y`와 반대이므로 주의함.
`load_gcps`에 `workspace`를 주면 촬영 위치와 비교해 순서를 자동 판단함.

`to_dict()` / `GCP.from_dict()`는 `edits.json` 형식(`marks` 대신 `obs` 키)으로 바꿈.

### `TiePoint(id, marks=[])`

같은 지점을 여러 사진에 표시한 수동 타이포인트. 정합된 사진 2장 이상에 표시해야 삼각측량됨.

### `load_gcps(path, epsg=None, *, columns=None, encoding=None, delimiter=None, workspace=None, check=None) -> list[GCP]`

측량 성과 파일(CSV·TXT)을 읽어 `GCP` 목록을 만듦 (`marks`는 비어 있음).

| 인자 | 설명 |
|---|---|
| `path` | 파일 경로 |
| `epsg` | 측량 좌표계. `None`이면 `workspace`의 촬영 위치와 비교해 추정함 (실패하면 `InputError`) |
| `columns` | 열 번호(0부터) `{"name": 0, "x": 2, "y": 1, "z": 3}`. `None`이면 머리글·값으로 추정. `z`가 `None`이면 표고 0 |
| `encoding` | `None`이면 UTF-8(BOM) → CP949 → Latin-1 순으로 시도 |
| `delimiter` | `","`, `"\t"`, `";"`, `"whitespace"`. `None`이면 자동 판단 |
| `workspace` | 정렬된 워크스페이스 (좌표계·X/Y 순서 추정용) |
| `check` | 검사점으로 쓸 점 이름 목록 |

- 머리글 인식: 이름(`name`, `id`, `점명`, `번호`, `측점` 등), 동쪽(`e`, `east`, `경도` 등), 북쪽(`n`, `north`, `위도` 등),
  `x`, `y`, 높이(`z`, `h`, `표고`, `높이` 등). `#`으로 시작하는 줄은 주석으로 건너뜀
- 좌표계 추정 후보: EPSG 5186·5187·5185·5188·5179·5174·4326·32652·32651. 촬영 위치와 30km 넘게 떨어지면 추정 실패로 봄
- 예외: `InputError`(`empty_file`, `invalid_argument`)

### `read_gcp_file(path, encoding=None, delimiter=None, workspace=None) -> dict`

`load_gcps`가 내부에서 쓰는 저수준 함수. 열 선택 UI를 만들 때 씀.
반환: `encoding`, `delimiter`, `header`, `num_columns`, `numeric`(열별 숫자 여부), `rows`(최대 500행), `num_rows`,
`guess`(`name`·`x`·`y`·`z` 열 번호, `epsg`, `distance_km`), `epsg_presets`.

---

## 8. 예외

```text
QuickOrthoError            (모든 SDK 예외의 기반, Exception 상속)
├── InputError             입력이 처리 조건을 만족하지 않음
├── AlignmentError         SfM·좌표 정렬 실패
├── ProcessingError        DSM·정사투영 처리 실패
├── ProjectError           워크스페이스 상태가 요청과 맞지 않음
└── Cancelled              CancelToken으로 중단됨
```

모든 예외는 `code`(고정 문자열)와 `message`(한국어 설명)를 가짐. `code`는 버전이 바뀌어도 유지하므로,
프로그램에서 분기하거나 HTTP 오류로 옮길 때는 `code`를 씀.

| 예외 | code | 뜻 | HTTP 예시 |
|---|---|---|---|
| `InputError` | `folder_not_found` | 영상 폴더 없음 | 404 |
| `InputError` | `no_images` | JPG 영상 없음 | 422 |
| `InputError` | `too_few_images` | 처리 대상 영상 3장 미만 | 422 |
| `InputError` | `too_few_gps` | GPS 있는 영상 3장 미만 | 422 |
| `InputError` | `no_gps` | (미리보기) GPS 있는 영상 없음 | 422 |
| `InputError` | `image_dir_missing` | 정렬 후 원본 영상 폴더가 없어짐 | 409 |
| `InputError` | `empty_file` | GCP 파일이 비어 있음 | 422 |
| `InputError` | `invalid_edits` | 보정 수정 사항 형식 오류 | 422 |
| `InputError` | `invalid_argument` | 인자 오류 (GCP 열·좌표계를 정할 수 없음 등) | 400 |
| `AlignmentError` | `sfm_failed` | 정합 영상 3장 미만 | 422 |
| `AlignmentError` | `georef_too_few` | GPS 있는 정합 영상 3장 미만 | 422 |
| `AlignmentError` | `georef_mismatch` | GPS와 SfM 배치 불일치 | 422 |
| `ProcessingError` | `dsm_failed` | 유효한 3D 점 부족 | 422 |
| `ProjectError` | `not_aligned` | 정렬 결과 없음 | 409 |
| `ProjectError` | `not_rendered` | (predict) 정사 모자이크·DSM 없음 | 409 |
| `Cancelled` | `cancelled` | 중단됨 | 499 |

SDK 예외가 아닌 예외(`OSError`, `MemoryError` 등)는 그대로 올라감. 디스크 부족·권한 문제 등이 여기에 해당함.
원인별 조치는 [문제 해결](troubleshooting.md)을 참고함.

---

## 9. 로깅

모든 이벤트는 콜백과 별개로 표준 `logging`의 `"quickortho"` 로거에 기록됨.

| 이벤트 | 로그 수준 |
|---|---|
| stage | INFO |
| progress | DEBUG |
| log (info) | INFO |
| log (warn) | WARNING |

```python
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
```

COLMAP 내부 로그는 오류(ERROR) 수준만 출력되며, 표준 출력이 아닌 표준 오류로 나감. 표준 출력은 CLI의 JSON-lines 전용으로 비워 둠.

---

## 10. API 안정성

- 버전은 [유의적 버전](https://semver.org/lang/ko/)을 따름. 다만 1.0 전(0.x)에는 **부 버전(0.1 → 0.2)에서 공개 API가 바뀔 수 있음**.
  바뀌면 릴리스 노트의 "호환성 변경" 항목에 적음
- 수 버전(0.1.0 → 0.1.1)은 호환성을 유지함 (버그 수정, 기능 추가)
- 공개 API 범위: `quickortho.__all__`의 이름, 이 문서에 적은 인자·속성, 예외 `code` 값, `report.json`의 `report_version` 1 형식,
  serve 프로토콜 버전 1
- 보고서·결과 dict에 **키가 추가되는 것**은 호환성 변경으로 보지 않음. 읽는 쪽은 모르는 키를 무시해야 함
