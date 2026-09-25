# 성능과 옵션 조정

## 1. 측정치

모두 기본 옵션(`gsd_scale=2`, `max_image_size=2000`, `max_num_features=4096`)으로 측정함.

| 데이터 | 장수·해상도 | 환경 | 정합 | 재투영 오차 | GPS 잔차 | 출력 | 총 시간 | 피크 메모리 |
|---|---|---|---|---|---|---|---|---|
| Mavic 2 Pro (부산 도심, 75m) | 18장, 20MP | 맥북 에어 M1 8GB | 18/18 | 1.20 px | 1.20 m | 6550×4353, 3.6 cm | 39초 | 1.41 GB |
| Mavic 2 Pro (부산 도심, 75m) | 18장, 20MP | 리눅스 VM 2코어 7GB | 18/18 | 1.21 px | 1.21 m | 6555×4357, 3.6 cm | 55초 | 0.79 GB |
| Mavic 2 Pro (해안 마을, 150m) | 13장, 20MP | 리눅스 VM 2코어 7GB | 13/13 | 0.88 px | 0.16 m | 3812×3688, 7.9 cm | 40초 | 0.76 GB |
| [Aukerman](https://github.com/OpenDroneMap/odm_data_aukerman) (공개, CC0) | 77장, 18MP | 리눅스 VM 2코어 7GB | 77/77 | 1.26 px | 0.96 m | 8254×6880, 5.6 cm | 312초 | 1.1 GB |

Aukerman 77장의 단계별 시간(리눅스 VM 2코어):

| 단계 | 시간 |
|---|---|
| 특징점 추출 | 109초 |
| 매칭 | 45초 |
| SfM (전역) | 71초 |
| 정사투영 | 73초 |
| COG 변환·미리보기 | 13초 |

빠른 미리보기: 18장 약 1초(간이 모자이크 포함), 77장 약 4초. `quicklook=False`면 18장 0.6초 내외.

### 규모에 따른 대략값

- 특징점 추출·정사투영은 장수에 거의 비례함
- 매칭은 40장 이하에서 전수 매칭(장수의 제곱에 비례), 그 이상은 공간 매칭(장수에 비례)임
- SfM은 장수보다 조금 빠르게 늘어남
- 단순 비례로 보면 200장은 2코어 VM에서 약 13~15분임. 코어가 많을수록 짧아짐 (실측으로 확인 필요)

## 2. 메모리

설계 목표는 **맥북 에어 M1 8GB에서 피크 약 4GB 이하**임. 메모리를 좌우하는 요소:

| 요소 | 영향 | 조정 |
|---|---|---|
| 특징점 추출 해상도 | 영상 1장 디코딩·SIFT 메모리 × 스레드 수 | `SfmOptions.max_image_size`, `num_threads` |
| 특징점 수 | 매칭·SfM 메모리 | `SfmOptions.max_num_features` |
| 영상 수 | SfM 재구성 크기 | 큰 작업은 블록으로 나눠 처리 |
| 정사투영 캐시 | 축소 영상 보관 | `OrthoOptions.cache_budget_mb` (기본 600MB) |
| 출력 해상도 | 타일 단위로 쓰므로 영향 작음. 파일 크기·시간에 영향 | `gsd_scale`, `gsd_m` |

메모리가 부족하면(시스템이 느려지거나 프로세스가 강제 종료됨) 다음 순서로 줄임.

```python
qo.OrthoOptions(
    cache_budget_mb=300,
    sfm=qo.SfmOptions(max_image_size=1600, max_num_features=3000, num_threads=4),
)
```

`report.json`의 `peak_memory_mb`로 실제 사용량을 확인할 수 있음 (프로세스 RSS를 0.5초마다 잰 최댓값).

## 3. 속도와 품질 조정

| 목적 | 옵션 | 효과 |
|---|---|---|
| 현장에서 빨리 확인 | `gsd_scale=4` | 정사투영·COG 변환 시간 약 1/4 |
| 현장에서 더 빨리 | `SfmOptions(max_image_size=1200, max_num_features=2048)` | 특징점·매칭 시간 감소. 정합률이 떨어질 수 있음 |
| 원본 해상도 결과 | `gsd_scale=1` | 화소 수 4배 → 정사투영 시간·파일 크기 약 4배 |
| 서버에서 품질 높이기 | `SfmOptions(max_image_size=3200, max_num_features=8192)` | 정합 안정성·정밀도 향상. 특징점 단계 시간·메모리 증가 |
| 정합이 안 되는 영상이 많음 | `SfmOptions(num_neighbors=25)` 또는 `exhaustive_below=80` | 매칭 쌍이 늘어 연결이 좋아짐. 매칭 시간 증가 |
| 다른 작업과 CPU 나눠 쓰기 | `SfmOptions(num_threads=4)` | SfM 단계의 코어 사용 제한 |

- `num_threads`는 SfM(COLMAP) 단계에만 적용됨. 정사투영은 NumPy·OpenCV 내부 스레드를 씀
- 정렬을 한 번 해 두면 `orthomosaic()`으로 해상도만 바꿔 여러 번 만들 수 있음. 현장에서는 `gsd_scale=4`로 확인하고,
  사무실에서 같은 워크스페이스로 `gsd_scale=1`을 다시 만드는 방식이 효율적임

## 4. 서버 운용 권장

- 작업 하나가 SfM 단계에서 모든 코어를 씀. **동시 작업 수 = 코어 수 / 8 정도**에서 시작해 조정함
- 작업마다 별도 프로세스(`quickortho serve`)로 돌려 메모리를 분리함 ([진행률과 중단](events-and-cancel.md#4-프로세스로-감싸기-웹-서버즉시-중단))
- 워크스페이스는 빠른 로컬 디스크(SSD)에 둠. 네트워크 드라이브는 정사투영 단계가 느려짐
- 원본 영상은 정사 모자이크를 다시 만들 때도 읽으므로, 워크스페이스를 보관하는 동안 영상도 유지함

## 5. GPU

0.1.0은 CPU만 씀. PyPI의 pycolmap 배포본은 CPU 빌드이며, COLMAP의 GPU 가속(CUDA)을 쓰려면 pycolmap을
CUDA로 직접 빌드해야 함. GPU 선택 가속은 이후 버전 후보임.
