# 결과물과 보고서

## 1. 정사 모자이크 결과 파일

| 파일 | 형식 | 내용 |
|---|---|---|
| `orthomosaic.tif` | GeoTIFF (COG), uint8 RGBA, deflate 압축, 512px 타일, 오버뷰 포함 | 정사 모자이크. 알파 0은 자료 없음 |
| `dsm.tif` | GeoTIFF, float32, deflate 압축 | 정사투영에 쓴 간이 DSM (m). 해상도는 점군 간격 수준(수 m) |
| `preview.png` | PNG RGBA | 정사 모자이크를 긴 변 2048px 이하로 줄인 미리보기 |
| `report.json` | JSON (UTF-8) | 처리 보고서 (2절) |

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

```json
{
  "report_version": 1,
  "engine_version": "0.2.0",
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
  "dsm": {"mode": "sparse", "resolution_m": 5.26, "num_points": 7085},
  "ortho": {
    "width": 3812, "height": 3688, "gsd_m": 0.0787, "covered_area_m2": 79027.5,
    "tiles": 64, "source_gsd_m": 0.0393
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
| `outputs.*` | 결과 파일 절대 경로 |
| `preview_corners_lonlat` | 결과 네 모서리 경위도 [좌상, 우상, 우하, 좌하] |
| `timings_s` | 단계별 시간(초). 정렬 단계 + 마지막 정사 모자이크 단계. `total_s`는 두 단계의 합 |
| `peak_memory_mb` | 정렬·마지막 정사 모자이크 중 최대 메모리(RSS, MB) |
| `warnings` | 경고 (정렬 경고 + 보정 경고) |
| `refine` | (보정 후) 보정 결과. 아래 표 |

- `outputs`의 경로는 절대 경로임. 서버에서 외부에 알릴 때는 다운로드 URL 등으로 바꿔서 내보냄
- 읽는 쪽은 모르는 키를 무시해야 함. 같은 `report_version` 안에서 키가 추가될 수 있음

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
| `timings_s` | 보정 단계별 시간 (`adjust_s`, `dsm_s`, `ortho_s`, `finalize_s`, `total_s`) |
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
