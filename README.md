# QuickOrtho SDK

드론 영상으로 정사 모자이크(GeoTIFF)를 만드는 Python SDK임.
[QuickOrtho 데스크톱 앱](https://github.com/SeaViewer91/quick-ortho)의 처리 엔진을 분리한 것으로,
웹 API·서버 프로그램·자동화 스크립트·다른 응용 프로그램에서 같은 처리를 쓸 수 있게 함.

```python
import quickortho as qo

result = qo.process("DJI_images/", "workspace/", on_event=qo.print_progress)
print(result.orthomosaic)   # workspace/orthomosaic.tif (COG, UTM)

# 산출물을 변수로 받아 다른 라이브러리에 바로 넘기기 (OpenCV 함수처럼)
image, transform, crs = result                                     # numpy RGBA, affine.Affine, pyproj.CRS
project = qo.Project.open("workspace/")
cameras, (xyz, rgb) = project.cameras(), project.points()          # 카메라 자세(K·dist·R·t), 희소 점군
```

> 최신 릴리스는 0.2.0(알파)임. 1.0 전까지는 공개 API가 바뀔 수 있으며, 바뀌면 릴리스 노트에 적음.

## 주요 기능

| 기능 | 설명 |
|---|---|
| 스캔 | EXIF·DJI XMP에서 위치·고도·짐벌 자세·카메라 정보를 읽고, 광각(매핑용) 카메라를 자동 선별함 |
| 빠른 미리보기 | SfM 없이 수 초 만에 촬영 범위·중복도·누락 구역·간이 모자이크를 만듦 |
| 정렬 | SIFT 특징점 → GPS 기반 공간 매칭 → 전역 SfM(실패 시 증분 SfM) → GPS 좌표 정렬 |
| 정사 모자이크 | 희소 점군 기반 간이 DSM → 타일 단위 정사투영 → Cloud Optimized GeoTIFF |
| 정밀 보정 | GCP(국내 좌표계 포함)·수동 타이포인트·오차 큰 관측 제거 후 번들 조정, 검사점 오차 보고 |
| 산출물 변수 | 결과를 풀어서 받으면 numpy 배열·`Affine`·`CRS`·카메라(OpenCV 형식 K·dist·R·t, ω·φ·κ)·점군이 나옴 |
| 사진 ↔ 지도 좌표 | 원본 사진의 탐지 결과를 지도 좌표로, 지도 좌표를 사진 좌표로 변환 |
| 좌표계·격자·지형면 | 결과 좌표계 지정(예: EPSG:5186), 시기별 결과를 같은 격자로 고정, 수평면·외부 DEM으로 정사투영 |
| 진행·중단 | 진행 이벤트 콜백, 다른 스레드에서 중단(`CancelToken`), 표준 `logging` 기록 |
| 명령줄·상주 모드 | `quickortho` 명령(터미널에서는 진행 막대, 파이프에서는 JSON), 환경 진단 `doctor`, 상주 모드 `serve` |

설계 기준은 **맥북 에어 M1 8GB(CPU 전용)** 임. 모든 기능이 GPU 없이 동작하며, 기본값으로 영상 수백 장을 처리할 때
피크 메모리 약 4GB 이하를 목표로 함.

## 지원 환경

| OS | 조건 |
|---|---|
| macOS | 14(Sonoma) 이상, **Apple Silicon 전용** (Intel 맥은 지원하지 않음) |
| Windows | 10·11, x64 (ARM은 지원하지 않음) |
| Ubuntu Linux | 20.04 이상, x86_64 (glibc 2.28 이상) |

- Python 3.10 ~ 3.14
- 검증 기종: DJI Mavic 2 Pro. 다른 기종(Mavic 3E, Matrice 4E, Phantom 4 Pro V2.0)은 검증 중임
- CI에서 세 OS 모두 합성 드론 영상으로 SfM부터 GCP 보정까지 전체 시험을 통과해야 릴리스함

## 설치

[Releases](https://github.com/SeaViewer91/quickortho-sdk/releases)에 올린 wheel 하나로 세 OS 모두 설치함.
pycolmap·rasterio·OpenCV 같은 의존성은 pip가 PyPI에서 OS에 맞게 받아 옴 (설치할 때 인터넷 필요).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install https://github.com/SeaViewer91/quickortho-sdk/releases/download/v0.2.0/quickortho_sdk-0.2.0-py3-none-any.whl
quickortho version                   # 설치 확인
```

오프라인 설치, 소스 설치, 업그레이드는 [설치 문서](docs/installation.md)를 참고함.

## 빠른 시작

```python
import quickortho as qo

project = qo.Project.create("flight_0925/", "flight_0925_out/")

scan = project.scan()                                   # 1초 내외
print(len(scan.selected_images), "장 처리 예정")

pv = project.preview()                                  # 수 초, 촬영 누락 확인
print("누락 구역", pv.num_gaps, "곳")

align = project.align(on_event=qo.print_progress)       # SfM (가장 오래 걸림)
ortho = project.orthomosaic(qo.OrthoOptions(gsd_scale=2.0))
print(ortho.orthomosaic, f"GSD {ortho.gsd_m * 100:.1f} cm")

ortho_fine = project.orthomosaic(qo.OrthoOptions(gsd_scale=1.0))  # SfM 없이 해상도만 바꿔 다시 생성
```

명령줄에서도 같은 처리를 할 수 있음.

```bash
quickortho ortho flight_0925/ -o flight_0925_out/ > events.jsonl
```

## 문서

| 문서 | 내용 |
|---|---|
| [설치](docs/installation.md) | 요구 사항, 설치·업그레이드·삭제, 오프라인 설치, OS별 주의 사항 |
| [빠른 시작](docs/quickstart.md) | 처음 쓰는 사람을 위한 따라 하기 |
| [핵심 개념](docs/concepts.md) | 워크스페이스, 처리 단계, 좌표계, 정확도, 카메라 선별 |
| [API 레퍼런스](docs/api-reference.md) | 모든 공개 클래스·함수·옵션·결과·예외 |
| [산출물을 변수로 쓰기](docs/outputs.md) | 결과 풀기, 래스터·카메라·점군, 좌표 규약, OpenCV·rasterio·PyTorch 연동 예 |
| [진행률과 중단](docs/events-and-cancel.md) | 이벤트 콜백, CancelToken, 로깅, 스레드·프로세스 활용 방식 |
| [정밀 보정](docs/refinement.md) | GCP·타이포인트 보정 흐름, 수정 사항 형식, 점 위치 예측 |
| [결과물과 보고서](docs/report.md) | 결과 파일, `report.json`·`preview.json` 필드 |
| [명령줄과 serve 프로토콜](docs/cli.md) | 명령 목록, JSON-lines 이벤트, 상주 모드 프로토콜 |
| [성능과 옵션 조정](docs/performance.md) | 측정치, 메모리·속도 조정 방법 |
| [문제 해결](docs/troubleshooting.md) | 오류 코드별 원인과 조치, OS별 문제 |
| [개발 참여](docs/development.md) | 개발 환경, 시험, 릴리스 절차, 버전 정책, 데스크톱 앱 번들 |

예제 코드는 [examples/](examples/)에 있음.

## 한계

- 간이 DSM(희소 점군 보간)을 쓰므로 고층 건물은 옆면이 보이고 경계가 겹칠 수 있음. 현장 확인·일반 매핑용이며 측량 성과용이 아님
- 절대 위치 정확도는 GNSS 수준(수 m)임. 정밀 위치가 필요하면 GCP 보정이 필요함
- 롤링 셔터(Mavic 2 등) 보정은 하지 않음 (경고만 표시)
- 수면·모래사장처럼 특징점이 없는 영역은 SfM에 실패해 빠질 수 있음
- 처리 중단(`CancelToken`)은 COLMAP 계산 구간(특징점 추출·매칭·SfM)이 끝난 뒤 반영됨. 즉시 중단은 프로세스 방식을 씀
  ([진행률과 중단](docs/events-and-cancel.md) 참고)

## 라이선스

[MIT](LICENSE). 주요 의존성 라이선스: pycolmap/COLMAP(BSD), OpenCV(Apache-2.0), rasterio(BSD), GDAL(MIT/X),
pyproj(MIT), shapely(BSD), NumPy·SciPy(BSD), Pillow(MIT-CMU).
