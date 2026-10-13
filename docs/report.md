# 결과물과 보고서

## 1. 정사 모자이크 결과 파일

| 파일 | 형식 | 내용 |
|---|---|---|
| `orthomosaic.tif` | GeoTIFF (COG), uint8 RGBA, deflate 압축, 512px 타일, 오버뷰 포함 | 정사 모자이크. 알파 0은 자료 없음 |
| `dsm.tif` | GeoTIFF, float32, deflate 압축 | 정사투영에 쓴 간이 DSM (m). 해상도는 점군 간격 수준(수 m) |
| `preview.png` | PNG RGBA | 정사 모자이크를 긴 변 2048px 이하로 줄인 미리보기 |
| `report.json` | JSON (UTF-8) | 처리 보고서 (2절) |
| `indices/<이름>.tif` | (다중분광) GeoTIFF (COG), float32 단일 밴드, nodata NaN, 예측자 3 | 고른 식생·수분 지수 하나 (밴드 설명에 지수 이름). 0.4.1부터 |
| `indices/<이름>.png` | (다중분광) PNG RGBA, 긴 변 2048px 이하 | 지수를 미리보기 범위(`index_legend`의 `min`~`max`)로 색 띠에 칠한 것. 자료 없는 곳은 투명 |
| `project/water/<영상 경로>.png` | PNG 8비트 회색, 긴 변 480px | 영상별 수면 판별 (0 육지, 100 수면 후보, 200 확실한 수면). 수면 판별을 했을 때만. 0.5.0부터 |
| `project/water.json` | JSON | 수면 판별 요약 `{"source": "auto"\|"user", "image_fraction", "images_with_water", "confident_fraction"}` |

- 다중분광이면 `orthomosaic.tif`가 밴드별 float32 반사율(nodata NaN, 예측자 3, 밴드 설명에 밴드 이름)이고,
  `preview.png`는 반사율 R·G·B(Blue가 없으면 NIR·Red·Green 위색)를 1~99백분위로 늘인 것임
- 0.4.0은 지수를 다중밴드 `indices.tif` 하나와 `ndvi.png`로 만들었음. 0.4.1에서 지수를 다시 만들면(정사 모자이크·`make_indices`) 이 두 파일은 지워짐
- 좌표계: 보정 전에는 WGS84 UTM(촬영 위치 기준 자동), GCP 보정 후에는 GCP 좌표계
- `preview.png`를 웹 지도에 겹칠 때는 `report.json`의 `preview_corners_lonlat`(네 모서리 경위도)을 씀.
  MapLibre의 image source 좌표 순서(좌상, 우상, 우하, 좌하)와 같음
- COG는 GDAL 3.1 이상, QGIS, ArcGIS Pro, rio-tiler 등에서 바로 열고 부분 읽기를 할 수 있음

파이썬에서 읽기:

```python
import rasterio

with rasterio.open(result.orthomosaic) as ds:
    print(ds.crs, ds.res, ds.bounds, ds.count)       # EPSG:32652, (0.036, 0.036), ..., 4
    rgba = ds.read(out_shape=(4, ds.height // 8, ds.width // 8))   # 오버뷰로 1/8 읽기
```

## 2. `report.json`

`OrthoResult.report`와 같은 내용임. 정렬만 한 상태에서는 정렬 항목(`input`, `sfm`, `georef`, `timings_s`, `peak_memory_mb`, `warnings`)만 있음.
아래 예는 Mavic 2 13장을 `gsd_scale=2`, `color_balance=False`로 만든 결과임 (기본값이면 `ortho.width`·`height`가 약 2배,
`gsd_m`이 `source_gsd_m`과 같고, `ortho.color`와 `timings_s.color_s`가 추가됨).

```json
{
  "report_version": 1,
  "engine_version": "0.5.0",
  "input": {
    "folder": "/data/flight_0925",
    "total_files": 13, "selected": 13, "with_gps": 13,
    "cameras": {
      "Hasselblad L1D-20c | 10.3mm | 5472x3648": {
        "make": "Hasselblad", "model": "L1D-20c", "focal_mm": 10.26, "focal_35mm": 28.0,
        "width": 5472, "height": 3648, "count": 13, "selected": true
      }
    }
  },
  "sfm": {
    "num_input_images": 13, "matching": "exhaustive", "verified_pairs": 78, "mapper": "global",
    "num_registered": 13, "num_points3D": 7624, "mean_reprojection_error_px": 0.885
  },
  "georef": {
    "epsg": 32652, "gps_residual_rms_m": 0.156, "num_aligned": 13,
    "note": "절대 위치 정확도는 GNSS 수준(수 m)임. 정밀 위치가 필요하면 GCP 필요"
  },
  "dsm": {"mode": "sparse", "resolution_m": 1.75, "num_points": 7085},
  "ortho": {
    "width": 3812, "height": 3688, "gsd_m": 0.0787, "covered_area_m2": 79027.5,
    "tiles": 64, "blend": "seamline", "color_balance": false, "source_gsd_m": 0.0393
  },
  "outputs": {
    "orthomosaic": "/data/out/orthomosaic.tif",
    "dsm": "/data/out/dsm.tif",
    "preview": "/data/out/preview.png"
  },
  "preview_corners_lonlat": [[128.8229, 35.0078], [128.8262, 35.0078], [128.8262, 35.0052], [128.8229, 35.0052]],
  "timings_s": {
    "scan_s": 0.03, "features_s": 14.01, "matching_s": 3.62, "mapping_s": 7.72, "georef_s": 0.01,
    "dsm_s": 0.12, "ortho_s": 10.0, "finalize_s": 4.67, "total_s": 40.19
  },
  "peak_memory_mb": 758.4,
  "warnings": ["롤링 셔터 카메라(Mavic 2 등) 영상이 포함됨: 고속 비행 시 왜곡 가능"]
}
```

### 필드 설명

| 필드 | 설명 |
|---|---|
| `report_version` | 보고서 형식 버전 (1). 호환되지 않게 바뀔 때만 올림 |
| `engine_version` | 보고서를 만든 SDK 버전 |
| `input.folder` | 영상 폴더 절대 경로 |
| `input.total_files` | 폴더의 JPG 수 |
| `input.selected` | 처리 대상 영상 수 (카메라 선별 후) |
| `input.with_gps` | 처리 대상 중 GPS가 있는 영상 수 |
| `input.cameras` | 카메라별 요약 (`selected`: 처리 대상 여부) |
| `sfm.matching` | `exhaustive`(전수) 또는 `spatial`(GPS 공간 매칭) |
| `sfm.verified_pairs` | 기하 검증을 통과한 영상 쌍 수 |
| `sfm.mapper` | `global`(전역 SfM) 또는 `incremental`(증분 SfM) |
| `sfm.num_registered` | 정합된 영상 수. `num_input_images`보다 작으면 경고에 표시 |
| `sfm.num_points3D` | 희소 3D 점 수 |
| `sfm.mean_reprojection_error_px` | 평균 재투영 오차(px) |
| `georef.epsg` | 결과 좌표계 |
| `georef.gps_residual_rms_m` | 정렬 후 카메라 위치와 GPS의 수평 RMS 차이(m) |
| `georef.num_aligned` | 정렬에 쓴 영상 수 |
| `georef.mode` | (보정 후) `gcp`, `gcp_shift`, `gps` |
| (보정 후 `gcp` 모드) | GPS 기준이 아니므로 `gps_residual_rms_m`, `num_aligned`를 뺌. 정확도는 `refine.gcp_summary.check`로 판단 |
| `georef.note` | 정확도 안내 문장 |
| `dsm.mode` | 지형면 방식: `sparse`, `plane`, `external` |
| `dsm.plane_z` | (`plane`) 평면 높이(m) |
| `dsm.source` | (`external`) 외부 DSM 파일 절대 경로 |
| `dsm.vertical_offset_m` | (`external`) 외부 DSM에 더한 높이 보정량(m) |
| `dsm.resolution_m` | DSM 격자 간격(m) |
| `dsm.num_points` | 희소 점군 점 수 (이상치 제거 후). 외부 DSM을 높이 보정 없이(`dsm_vertical_align=False`) 쓰면 0 |
| `ortho.width`, `height` | 결과 화소 크기 |
| `ortho.gsd_m` | 결과 GSD(m) |
| `ortho.source_gsd_m` | 원본 GSD(m) |
| `ortho.covered_area_m2` | 자료가 있는 면적(m²) |
| `ortho.tiles` | 처리한 512px 타일 수 |
| `ortho.blend` | 영상 합성 방식. 0.3.0부터 `seamline`(화소마다 영상 하나, 경계만 섞음). 0.2.x 결과에는 없음(가중 평균) |
| `ortho.color_balance` | 밝기·색 보정을 적용했는지 (0.3.1부터). `color_balance=False`이거나 영상이 1장이면 `false` |
| `ortho.color` | (보정 시) `{"cell_m": 보정 격자 셀 크기(m), "grid": [64, 48] 영상별 이득 맵 크기, "images": 이득 맵을 만든 영상 수, "gain_p5", "gain_p95": 이득(세 채널 평균)의 5·95 백분위}`. 1에서 멀수록 영상 간 밝기 차이가 컸다는 뜻임. 이득 맵을 만든 영상이 없으면 `color` 항목 없이 `color_balance`가 `false`임 |
| `outputs.*` | 결과 파일 절대 경로 |
| `preview_corners_lonlat` | 결과 네 모서리 경위도 [좌상, 우상, 우하, 좌하] |
| `timings_s` | 단계별 시간(초). 정렬 단계 + 마지막 정사 모자이크 단계(`dsm_s`, `color_s`, `bands_s`, `ortho_s`, `finalize_s`). 수면 판별을 했으면 `water_s`. `total_s`는 두 단계의 합. `make_indices()`로 지수만 다시 만들면 `indices_s`가 추가됨 (`total_s`에 넣지 않음) |
| `peak_memory_mb` | 정렬·마지막 정사 모자이크 중 최대 메모리(RSS, MB) |
| `warnings` | 경고 (정렬 경고 + 보정 경고) |
| `refine` | (보정 후) 보정 결과. 아래 표 |

- `outputs`의 경로는 절대 경로임. 서버에서 외부에 알릴 때는 다운로드 URL 등으로 바꿔서 내보냄
- 읽는 쪽은 모르는 키를 무시해야 함. 같은 `report_version` 안에서 키가 추가될 수 있음

### `multispectral` 항목 (다중분광)

다중분광 결과에만 있음 (0.4.0부터). `outputs`에 `indices`(`{지수 이름: tif 경로}`), `index_previews`(`{지수 이름: png 경로}`)가
(지수를 만들었을 때만. 0.4.0은 `indices`가 다중밴드 파일 경로 문자열이고 `index_preview`가 있었음), `ortho`에 `bands`(밴드 이름)와
`reference_gsd_m`(기준 밴드 원본 GSD)가 추가되고, `input.multispectral`에 스캔 요약이 들어감. `ortho.source_gsd_m`은 출력 밴드의 원본 GSD임.

| 필드 | 내용 |
|---|---|
| `model` | 카메라 기종 |
| `bands` | `[{"name", "wavelength_nm"}]` 출력 밴드 (정사 모자이크 밴드 순서) |
| `reference_band` | 정렬에 쓴 기준 밴드 |
| `values` | `reflectance` 또는 `radiance` |
| `indices` | 만든 지수 이름 (등록 순서) |
| `index_legend` | 지수별 `{"label", "min", "max", "colors"}`. `min`~`max`는 미리보기 PNG의 색 범위, `colors`는 색 띠의 양끝·가운데 hex 3개 (범례 표시용). 0.4.1부터 |
| `indices_skipped` | 골랐지만 카메라에 필요한 밴드가 없어 만들지 못한 지수. 0.4.1부터 |
| `preview` | `true_color`, `false_color`, `gray` (미리보기 색 구성) |
| `radiometry.mode` | 실제 쓴 반사율 환산 방식 |
| `radiometry.irradiance_cv` | (DLS) 밴드별 비행 중 입사광 변동계수 |
| `radiometry.panel` | (패널 촬영이 있으면) 밴드별 `albedo`, `radiance`, `reflectance_dls`, `saturated_frac`(포화 비율), `count` |
| `radiometry.panel_correction` | (DLS) 밴드별 패널 배율 k (`dls`만 쓰면 1) |
| `radiometry.panel_missing_bands` | 패널 촬영은 있지만 패널 정보가 없는 밴드 (이때 패널 보정을 쓰지 않음, 경고) |
| `radiometry.band_alignment` | rig 미세 조정: `captures`(쓴 캡처 수), `initial`(조정 전), `residual`(조정 후). 밴드별 `shift_px`(중앙값 이동), `spread_px`(MAD), `n` |
| `radiometry.capture_normalization` | 블록 조정: `applied`, `points`(쓴 타이포인트 수), 밴드별 `inconsistency_before_pct`·`inconsistency_after_pct`(캡처 간 불일치 RMS %), `gain_min`·`gain_max` |

### `water` 항목 (수면 처리)

0.5.0부터. 수면 판별을 했거나 정사 단계에 `water`를 준 결과에만 있음 (다중분광에는 없음). 원리는 [핵심 개념 11절](concepts.md#11-수면-처리).
정렬 때의 판별 정보(수면 비율, 특징점 제외)에 마지막 정사 단계의 방식·DSM 통계를 합친 것임. 데스크톱 앱 v0.5.0과 같은 키에 SDK는 `source`, `levels_m`, `note`를 더함.

| 필드 | 내용 |
|---|---|
| `mode` | 마지막 정사 단계의 방식 (`auto`, `on`, `off`) |
| `active` | 수면 처리를 켰는지 (`off`이면 `false`) |
| `source` | 마스크 출처 `auto`(SDK 판별) 또는 `user`(`Project.set_water_masks`) |
| `image_fraction` | 영상 면적 중 수면 비율 평균 |
| `images_with_water` | 수면이 영상의 1% 넘게 보이는 영상 수 |
| `confident_fraction` | 영상 면적 중 확실한 수면 비율 평균 |
| `feature_masked_images` | 정렬에서 특징점을 뺀 영상 수 (정렬 때 판별하지 않았으면 0 또는 없음) |
| `candidate_cells` | DSM 칸 중 수면 후보(본 영상의 절반 이상이 수면) 칸 수 |
| `components`, `water_components` | 100 m² 이상 후보 덩어리 수, 그중 수면으로 확정한 수 |
| `area_m2` | 평면으로 덮은 수면 면적(m²) |
| `levels_m` | 확정한 수면 덩어리들의 높이(m) |
| `land_point_density` | 육지 칸의 평균 점 수 (점 밀도 조건의 기준) |
| `detail` | 덩어리별 `{area_m2, point_ratio, shore_points, inner_points, raised_m, shore_drop_m, level_m, water}` (넓은 순 최대 10개). `point_ratio`는 안쪽 점 밀도 / 육지 밀도, `raised_m`은 덩어리 안 점 중앙값 − 물가 점 중앙값, `shore_drop_m`은 수면 높이 − 물가 점 5백분위 |
| `note` | 적용하지 않은 이유 (외부·평면 DSM) |

### `refine` 항목

| 필드 | 설명 |
|---|---|
| `mode` | `gcp`, `gcp_shift`, `gps` |
| `epsg`, `crs_name` | 보정 결과 좌표계 |
| `num_control` | 사용한 기준점 수 |
| `intrinsics_refined` | (gcp) 초점거리·왜곡도 조정했는지 |
| `shift_m` | (gcp_shift) 적용한 평행 이동량 [dx, dy, dz] |
| `gps_residual_rms_m`, `num_aligned` | (gps, gcp_shift) GPS 재정렬 결과 |
| `deleted_points` | 실제로 지운 점 수 |
| `filtered_observations` | 자동 제거한 관측 수 |
| `max_reproj_error_px` | 적용한 자동 제거 기준 |
| `manual_tiepoints` | 수동 타이포인트별 `{"id", "point_id", "error_px", "marks": [{"image", "reproj_px"}]}` |
| `before`, `after` | 보정 전후 재투영 통계 `{"num_points", "num_observations", "mean_px", "rmse_px", "p95_px", "max_px"}` |
| `gcps` | GCP별 오차 표 (`RefineResult.gcps`와 같음) |
| `gcp_summary` | `{"control": {...}, "check": {...}}` 역할별 RMSE(m) |
| `timings_s` | 보정 단계별 시간 (`adjust_s`, `dsm_s`, `color_s`, `bands_s`, `ortho_s`, `finalize_s`, `total_s`) |
| `peak_memory_mb` | 보정 중 최대 메모리 |
| `warnings` | 보정 경고 |

## 3. 미리보기 결과

`qo.preview()` 결과 폴더:

| 파일 | 내용 |
|---|---|
| `quicklook.png` | 간이 모자이크 (1/8 축소 디코딩 영상을 평면 가정 호모그래피로 배치). `quicklook=False`면 없음 |
| `coverage.png` | 중복도 지도: 1장 빨강, 2장 주황, 3~4장 노랑, 5장 이상 초록, 누락 보라 |
| `coverage.tif` | 중복도 GeoTIFF (UTM): 밴드 1 중복 매수(uint16), 밴드 2 누락 구역(1). quicklook·coverage.png와 같은 격자 |
| `preview.geojson` | WGS84 FeatureCollection (아래 표) |
| `preview.json` | 요약 (`PreviewResult.raw`와 같음) |

`preview.geojson`의 `properties.kind`:

| kind | 형상 | 속성 |
|---|---|---|
| `footprint` | Polygon | `file`, `yaw_estimated`(짐벌 방향을 진행 방향으로 추정했는지) |
| `gap` | Polygon | `area_m2` — 누락 구역 |
| `low_overlap` | Polygon | `area_m2` — 조사 영역 안쪽에서 중복이 2장 미만인 곳 |
| `camera` | Point | `file` — 촬영 위치 |

`preview.json`:

```json
{
  "folder": "/data/flight_0925",
  "num_images": 18,
  "epsg": 32652,
  "corners_lonlat": [[129.10467, 35.13519], [129.10714, 35.13518], [129.10714, 35.13387], [129.10467, 35.13387]],
  "cell_m": 0.2196,
  "bounds": [520391.8, 3887345.4, 520617.2, 3887491.2],
  "coverage": {"survey_area_m2": 22665.1, "gap_area_m2": 0.0, "low_overlap_area_m2": 0.0,
               "overlap_median": 4.0, "overlap_p10": 1.0},
  "forward_overlap_median": 0.695,
  "flight_height_m": {"min": 74.6, "max": 75.6},
  "gaps": 0,
  "outputs": {"quicklook": ".../quicklook.png", "coverage": ".../coverage.png",
              "coverage_tif": ".../coverage.tif", "geojson": ".../preview.geojson"},
  "warnings": ["롤링 셔터 카메라 포함"],
  "time_s": 1.08
}
```

| 필드 | 설명 |
|---|---|
| `corners_lonlat` | 두 PNG의 네 모서리 경위도 [좌상, 우상, 우하, 좌하] |
| `cell_m` | 중복도 격자 크기(m) = PNG 한 화소 크기 |
| `multispectral` | (다중분광 폴더) 스캔 요약(`model`, `bands`, `reference_band` 등)과 `index_catalog`: 지수마다 `{"name", "label", "bands", "available", "default"}`. 같은 목록이 `indices` 키에도 있음 (데스크톱 앱 호환). RGB 폴더면 `null`. 0.4.1부터 |
| `bounds` | 격자 범위 `[xmin, ymin, xmax, ymax]` (`epsg` 좌표계). `PreviewResult.transform`을 만드는 데 씀 |
| `coverage.survey_area_m2` | 조사 영역 면적 (촬영 범위를 합치고 작은 틈을 메운 영역) |
| `coverage.gap_area_m2` | 누락 면적 |
| `coverage.low_overlap_area_m2` | 저중복(2장 미만) 면적. 조사 영역 가장자리 띠는 제외 |
| `coverage.overlap_median`, `overlap_p10` | 조사 영역 중복도의 중앙값, 하위 10% 값 |
| `forward_overlap_median` | 연속한 두 영상의 촬영 범위 겹침 비율 중앙값 (전방 중복률) |
| `flight_height_m` | 상대고도 최소·최대 |
| `gaps` | 누락 구역 수 |
| `time_s` | 처리 시간(초) |

## 4. `project/` 폴더

정렬·보정 결과는 `project/`에 저장됨. 구조는 [핵심 개념](concepts.md#1-워크스페이스)을 참고함.
`base/`, `refined/`는 COLMAP 재구성 형식(`cameras.bin`, `images.bin`, `points3D.bin`)이라 COLMAP GUI나
pycolmap으로 열어 볼 수 있음. 다만 지역 좌표계(`투영 좌표 - origin`)로 저장되어 있음.

`project/` 안의 파일은 SDK가 관리하므로 직접 고치지 않음. 수정 사항은 `Project.set_edits()`로 바꿈.
