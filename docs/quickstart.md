# 빠른 시작

이 문서는 설치를 마친 뒤 드론 영상 폴더 하나로 정사 모자이크를 만드는 과정을 따라 함.
설치는 [설치 문서](installation.md)를 참고함.

## 1. 준비물

- 연직(카메라가 아래를 보는) 촬영한 드론 JPG 영상 폴더. GPS 정보(EXIF)가 있어야 함
- 권장 중복도: 전방 70% 이상, 측방 60% 이상
- DJI 기체 영상이면 XMP의 상대고도·짐벌 자세를 함께 읽어 더 정확하게 처리함

영상 폴더 예:

```text
flight_0925/
├── DJI_0013.JPG
├── DJI_0014.JPG
└── ...
```

## 2. 한 번에 처리하기

```python
import quickortho as qo

result = qo.process("flight_0925", "flight_0925_out", on_event=qo.print_progress)
print(result.orthomosaic)
```

- 첫 번째 인자는 영상 폴더, 두 번째는 결과를 저장할 **워크스페이스** 폴더임 (없으면 만듦)
- `on_event=qo.print_progress`는 진행 상황을 콘솔에 출력함. 생략하면 조용히 처리함
- 결과는 `flight_0925_out/orthomosaic.tif`(Cloud Optimized GeoTIFF, UTM 좌표계)에 저장됨

출력 예 (진행률 줄 일부 생략):

```text
▶ 영상 18장 스캔 시작
▶ 특징점 추출 (18장)
  features: 18/18 (100%)
▶ 전수 매칭
▶ 카메라 위치·자세 추정 (전역 SfM)
  SfM 완료: 18/18장 정합, 3D 점 9123개, 재투영 오차 1.20px
▶ GPS로 좌표 정렬 (UTM)
  좌표계 EPSG:32652, GPS 잔차(RMS, 수평) 1.21 m
▶ 희소 점군으로 간이 DSM 생성
▶ 정사 모자이크 생성 (GSD 3.6 cm)
  ortho: 117/117 (100%)
▶ COG 변환·미리보기 생성
```

## 3. 결과 확인

```python
print(result.epsg)            # 32652 (WGS84 UTM 52N)
print(result.gsd_m)           # 0.0364 (m/화소)
print(result.width, result.height)
print(result.warnings)        # ['롤링 셔터 카메라(Mavic 2 등) 영상이 포함됨: ...']
print(result.report["sfm"])   # 정합 영상 수, 재투영 오차 등
```

워크스페이스에는 다음 파일이 생김.

| 파일 | 내용 |
|---|---|
| `orthomosaic.tif` | 정사 모자이크 (RGBA, COG). QGIS·ArcGIS에서 바로 열 수 있음 |
| `dsm.tif` | 정사투영에 쓴 간이 DSM (float32, m) |
| `preview.png` | 긴 변 2048px 미리보기 |
| `report.json` | 처리 보고서 |
| `project/` | 정렬 결과·보정 데이터 (다시 만들거나 보정할 때 씀. 직접 수정하지 않음) |

각 파일의 자세한 형식은 [결과물과 보고서](report.md)를 참고함.

### 결과를 변수로 받기

결과를 여러 변수로 풀면 정사 모자이크 배열과 좌표 정보가 바로 나옴. numpy·OpenCV·rasterio 등에 그대로 넘길 수 있음.

```python
image, transform, crs = qo.process("flight_0925", "flight_0925_out")
print(image.shape)          # (3688, 3811, 4)  RGBA uint8
x, y = transform * (0, 0)   # 왼쪽 위 모서리의 지도 좌표
print(crs.to_epsg())        # 32652
```

카메라 자세·점군·사진 좌표 변환은 [산출물을 변수로 쓰기](outputs.md)를 참고함.

## 4. 단계별로 처리하기

실제 업무에서는 단계를 나눠 쓰는 경우가 많음. 예를 들어 현장에서는 누락만 먼저 확인하고,
정사 모자이크는 확인용 저해상도로 빨리 만든 뒤, 사무실에서 원래 해상도로 다시 만드는 식임.

```python
import quickortho as qo

project = qo.Project.create("flight_0925", "flight_0925_out")

# (1) 스캔: 헤더만 읽음 (1초 내외)
scan = project.scan()
print(scan.summary)

# (2) 빠른 미리보기: SfM 없이 누락 확인 (수 초)
pv = project.preview()
print("누락 구역:", pv.num_gaps, "곳")
print("중복도 지도:", pv.coverage)          # flight_0925_out/preview/coverage.png

# (3) 정렬: 특징점 → 매칭 → SfM → GPS 좌표 정렬 (가장 오래 걸림)
align = project.align(on_event=qo.print_progress)
print(align.num_registered, "/", align.num_images, "장 정합")

# (4) 정사 모자이크: 확인용 (원본 GSD × 4)
quick = project.orthomosaic(qo.OrthoOptions(gsd_scale=4))

# (5) 정사 모자이크: 최종 (기본값, 원본 GSD 그대로). 정렬을 다시 하지 않으므로 빠름
final = project.orthomosaic()
```

나중에 같은 워크스페이스를 다시 열 때는 `Project.open`을 씀.

```python
project = qo.Project.open("flight_0925_out")
print(project)                 # Project(workspace='.../flight_0925_out', state='rendered')
result = project.result()      # 마지막 정사 모자이크 결과
```

## 5. 오류 처리

모든 SDK 예외는 `qo.QuickOrthoError`를 상속하고 `code` 속성을 가짐.

```python
try:
    result = qo.process("flight_0925", "out")
except qo.InputError as exc:        # 영상이 부족함, 폴더 없음 등
    print("입력 문제:", exc.code, exc.message)
except qo.AlignmentError as exc:    # SfM 실패, 좌표 정렬 실패
    print("정렬 실패:", exc.code, exc.message)
except qo.QuickOrthoError as exc:   # 그 밖의 SDK 오류
    print(exc.code, exc.message)
```

오류 코드 목록과 조치 방법은 [문제 해결](troubleshooting.md)에 정리함.

## 6. 명령줄로 처리하기

파이썬 코드 없이 명령줄에서도 같은 처리를 할 수 있음. 출력은 한 줄에 JSON 하나씩임.

터미널에서 실행하면 진행 막대와 요약이 나오고, 다른 프로그램이 읽으면(파이프) 한 줄에 JSON 하나씩 나옴.

```bash
quickortho doctor                                      # 설치 환경 진단
quickortho preview flight_0925 -o flight_0925_out/preview
quickortho ortho flight_0925 -o flight_0925_out --gsd-scale 4   # 확인용 (기본은 원본 해상도)
quickortho render flight_0925_out                     # 정렬 없이 원본 해상도로 다시 생성
```

자세한 내용은 [명령줄과 serve 프로토콜](cli.md)을 참고함.

## 다음 단계

- 처리 단계·좌표계·정확도를 이해하려면 [핵심 개념](concepts.md)
- GUI·웹 서버에서 진행률을 보여 주고 중단하려면 [진행률과 중단](events-and-cancel.md)
- GCP로 위치 정확도를 높이려면 [정밀 보정](refinement.md)
- 전체 API는 [API 레퍼런스](api-reference.md)
