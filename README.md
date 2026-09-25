# QuickOrtho Engine

QuickOrtho 데스크톱 앱이 사이드카로 실행하는 Python 처리 엔진임.
배포 시에는 PyInstaller로 OS별 단일 실행파일로 묶어 앱에 포함할 예정임.

## 통신 규약 (JSON-lines)

- 엔진은 stdout으로 **한 줄에 JSON 객체 하나**씩 이벤트를 출력함
- 모든 이벤트는 `type` 필드를 가짐

| type | 필드 | 용도 |
|---|---|---|
| `stage` | `name`, `message` | 처리 단계 시작 알림 |
| `progress` | `stage`, `current`, `total` | 진행률 |
| `log` | `level`, `message` | 로그 |
| `result` | `command`, `data` | 명령 결과 (명령당 1회) |
| `error` | `message`, `detail` | 오류. 이후 종료 코드 1로 종료함 |

## 명령

```bash
python -m quickortho_engine version
python -m quickortho_engine scan <영상 폴더> [--recursive]
python -m quickortho_engine preview <영상 폴더> -o <결과 폴더> [--max-size 2048]
python -m quickortho_engine ortho <영상 폴더> -o <결과 폴더> [옵션]
```

- `scan`: 폴더 내 JPG의 EXIF·DJI XMP를 읽어 위치·고도·짐벌 자세·카메라 정보를 수집하고,
  카메라별로 묶은 뒤 매핑용(광각) 카메라를 자동 선별함
- `preview`: SfM 없이 EXIF/XMP만으로 촬영 범위·중복도·누락 구역·간이 모자이크를 수 초 내에 생성함
- `ortho`: fast ortho 방식으로 정사 모자이크를 생성함

### `preview` 결과물

| 파일 | 내용 |
|---|---|
| `quicklook.png` | 간이 모자이크 (1/8 축소 디코딩 영상을 평면 가정 호모그래피로 배치) |
| `coverage.png` | 중복도 지도 (1장 빨강, 2장 주황, 3~4장 노랑, 5장 이상 초록, 누락 보라) |
| `preview.geojson` | 영상별 촬영 범위, 누락 구역, 저중복 구역, 촬영 위치 (WGS84) |
| `preview.json` | 요약 (두 PNG의 네 모서리 경위도, 중복도 통계, 누락 수, 전방 중복률, 경고) |

- 지면을 이륙 지점 높이의 평면으로 가정하므로 위치 오차는 수 m 수준임. 누락 확인용이며 측량용이 아님
- 촬영 자세는 DJI XMP의 짐벌 yaw·pitch를 사용함. 짐벌 yaw가 기체 yaw와 일정하게 어긋난 경우(Mavic 2 샘플에서 약 31° 사례 확인) 자동 보정함
- XMP가 없으면 비행 방향으로 yaw를 추정하고 35mm 환산 24mm 화각을 가정함 (정확도 낮음, 경고 표시)
- 누락 구역은 촬영 범위 내부 구멍과, 서로 떨어진 촬영 범위 사이의 틈(영상 한 장 폭 이하)으로 판정함.
  외곽의 계단 모양 오목부는 누락으로 보지 않음
- 성능: Mavic 2 Pro 18장 약 2초, 77장 약 4초 (2코어 VM)

### `ortho` 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--gsd` | 없음 | 출력 GSD(m). 지정하면 `--gsd-scale`보다 우선함 |
| `--gsd-scale` | 2.0 | 원본 GSD 대비 출력 배율 |
| `--max-image-size` | 2000 | 특징점 추출용 영상 긴 변(px) |
| `--max-features` | 4096 | 영상당 최대 특징점 수 |
| `--threads` | -1 | 스레드 수 (-1은 전체 코어) |
| `--keep-work` | 끔 | 중간 산출물(SfM DB 등) 보존 |

### `ortho` 결과물

| 파일 | 내용 |
|---|---|
| `orthomosaic.tif` | 정사 모자이크 (RGBA, Cloud Optimized GeoTIFF, UTM 좌표계) |
| `dsm.tif` | 정사투영에 사용한 간이 DSM (float32) |
| `preview.png` | 긴 변 2048px 미리보기 |
| `report.json` | 처리 보고서 (정합 수, 재투영 오차, GPS 잔차, 단계별 시간, 피크 메모리, 경고) |

## 처리 파이프라인

1. **스캔**: EXIF/XMP 수집, 광각 카메라 선별, GPS 없는 영상·경사 촬영·롤링 셔터 표시
2. **특징점 추출**: SIFT(pycolmap). 긴 변 2000px로 축소하고 업샘플 없이(`first_octave=0`) 추출함
3. **매칭**: GPS가 가까운 15장과만 매칭함. 40장 이하이거나 GPS가 없으면 전수 매칭
4. **SfM**: 전역 SfM(GLOMAP 방식)을 먼저 시도하고, 정합률이 80% 미만이면 증분 SfM으로 재시도함
5. **좌표 정렬**: 카메라 중심과 GPS 위치(UTM)의 닮음변환을 RANSAC으로 추정함
6. **간이 DSM**: 희소 점군에서 날아간 점을 제거하고 격자로 보간함.
   점군 밖은 가우시안 정규화 합성곱으로 부드럽게 외삽함(최근접 채움은 번짐을 유발함)
7. **정사투영**: 512px 출력 타일 단위로 DSM에 역투영하고, 영상 중심에 가까울수록 큰 가중치로 블렌딩함.
   거친 격자로 기여도를 미리 계산해 기여 2% 미만인 영상은 타일에서 제외함
8. **마무리**: 오버뷰 생성, COG 변환, 미리보기 생성

## 성능 참고치

공개 데이터셋 [Aukerman](https://github.com/OpenDroneMap/odm_data_aukerman)(CC0, 77장, 18MP) 기준,
2코어·RAM 7GB 리눅스 VM에서 측정함.

| 단계 | 시간 |
|---|---|
| 특징점 추출 | 109초 |
| 매칭 | 45초 |
| SfM (전역) | 71초 |
| 정사투영 (GSD 5.6cm, 8254×6880px) | 73초 |
| COG 변환·미리보기 | 13초 |
| **합계** | **312초** |

- 피크 메모리: 약 1.1GB
- 결과: 77/77장 정합, 재투영 오차 1.26px, GPS 잔차(RMS) 0.96m

## 개발

```bash
cd engine
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```
