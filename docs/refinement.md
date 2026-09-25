# 정밀 보정

GPS만으로 정렬한 결과는 절대 위치 오차가 수 m 수준임. 정밀 보정은 다음 세 가지 수단으로 위치 정확도와 정합 품질을 높임.

1. **GCP(지상기준점)**: 측량한 점을 사진에 표시해 결과를 측량 좌표계에 맞춤
2. **수동 타이포인트**: 자동 매칭이 약한 곳(수면 경계, 균질한 지표)에 같은 지점을 여러 사진에 표시해 연결을 보강함
3. **오차 큰 관측 정리**: 재투영 오차가 큰 매칭(움직이는 물체, 잘못된 매칭)을 기준값으로 자동 제거하거나 점 단위로 지움

## 1. 흐름

```text
align()/process()  →  수정 사항 입력(set_edits)  →  refine()  →  결과 확인  →  (수정 반복)  →  refine()
                         ↑ GCP 읽기(load_gcps)
                         ↑ 사진 표시 위치 찾기(predict)
                         ↑ 오차 분석(tiepoint_stats)
```

- 수정 사항은 `project/edits.json`에 저장되며, `refine()`은 **항상 최초 정렬 결과(base)에서 시작해** 수정 사항을 처음부터 적용함.
  그래서 여러 번 실행해도 결과가 누적되지 않고, 수정 사항만 고쳐 다시 실행하면 됨
- `reset_refinement()`는 보정 결과만 지우고 수정 사항은 남김
- 정렬(`align()`)을 다시 하면 보정 결과와 "삭제한 점" 목록은 지워지지만, GCP·타이포인트의 사진 표시는 남음

## 2. GCP 보정

### 2.1 측량 성과 읽기

```python
import quickortho as qo

project = qo.Project.open("flight_0925_out")

# 좌표계와 열을 직접 지정하는 방법 (가장 확실함)
gcps = qo.load_gcps("gcp.csv", epsg=5186, columns={"name": 0, "x": 2, "y": 1, "z": 3}, check=["C1", "C2"])

# 자동 추정: 머리글로 열을 찾고, 촬영 위치와 비교해 좌표계·X/Y 순서를 고름
gcps = qo.load_gcps("gcp.csv", workspace=project.workspace, check=["C1", "C2"])
```

**국내 측량 성과표의 X·Y에 주의함.** 국내 평면직각좌표는 보통 X가 북쪽(Northing), Y가 동쪽(Easting) 값임.
SDK의 `GCP.x`는 동쪽, `GCP.y`는 북쪽 값이므로, 직접 지정할 때는 `columns={"x": <Y 열>, "y": <X 열>}`로 바꿔 넣음.
`workspace`를 주면 두 순서를 모두 시험해 촬영 위치와 가까운 쪽을 고름.

자주 쓰는 좌표계:

| EPSG | 좌표계 |
|---|---|
| 5186 | GRS80 중부원점 (현행 국내 측량 기본) |
| 5187 | GRS80 동부원점 |
| 5185 | GRS80 서부원점 |
| 5188 | GRS80 동해원점 |
| 5179 | UTM-K (GRS80) |
| 5174 | Bessel 보정 중부원점 (구 좌표계) |
| 4326 | WGS84 경위도 |
| 32652 / 32651 | WGS84 UTM 52N / 51N |

### 2.2 기준점과 검사점

- **기준점(`role="control"`)**: 보정에 씀. 3점 이상이면 결과를 측량 좌표계에 맞춤. 촬영 범위 **가장자리와 모서리에 고르게** 두는 것이 좋음
- **검사점(`role="check"`)**: 보정에 쓰지 않고 정확도 평가에만 씀. 정확도를 말하려면 검사점이 있어야 함 (기준점 오차는 보정에 쓴 점이라 낙관적임)
- 권장: 기준점 4~6점 + 검사점 2점 이상

### 2.3 사진에 표시하기

GCP마다 그 점이 찍힌 사진 **2장 이상**에 표시(Mark)해야 함. 표시 좌표는 원본 사진의 화소 좌표이며,
원점은 좌상단 화소의 왼쪽 위 모서리, x는 오른쪽, y는 아래쪽임 (좌상단 화소 중심이 `(0.5, 0.5)`).

어느 사진에서 찾아야 하는지는 `predict()`로 알 수 있음. `predict()`는 DSM을 쓰므로 정사 모자이크를 만든 뒤에 씀.

```python
g = gcps[0]
pred = project.predict(world={"x": g.x, "y": g.y, "z": g.z, "epsg": g.epsg}, chips=True)
for c in pred["candidates"][:6]:
    print(c["image"], round(c["x"]), round(c["y"]), c["chip"]["path"])
```

- `candidates`는 사진 중심에 가까운 순서임. 중심에 가까운 사진일수록 왜곡이 적어 표시에 유리함
- `chips=True`면 예측 위치 주변을 원본 해상도 512px로 잘라 `project/cache/`에 저장함. 표시 UI에서 확대 화면으로 쓰면 됨
  (조각 안의 좌표 `(cx, cy)`는 원본 좌표 `(chip["x0"] + cx, chip["y0"] + cy)`)
- 보정 전에는 GPS 오차만큼 예측 위치가 어긋나 있으므로, 예측 위치 **주변에서** 실제 표지를 찾아 표시함
- 한 장에 표시하고 나서 `predict(marks=[그 표시])`를 부르면 DSM과 교차해 다른 사진의 위치를 더 정확히 예측함.
  두 장 이상 표시하면 삼각측량으로 예측함

표시를 모았으면 GCP에 넣고 저장함.

```python
g.marks = [qo.Mark("DJI_0013.JPG", 2310.4, 1520.8), qo.Mark("DJI_0014.JPG", 2298.1, 2410.3)]
project.set_edits(gcps=gcps)
```

### 2.4 보정 실행과 결과 확인

```python
result = project.refine(on_event=qo.print_progress)

print(result.mode)                           # "gcp"
print(result.rmse_before_px, "→", result.rmse_after_px)
print(result.control)                        # {"count": 4, "rmse_xy": 0.02, "rmse_z": 0.03, ...}
print(result.check)                          # 검사점 오차 → 정확도 판단 기준
for row in result.gcps:
    print(row["name"], row["role"], row["dxy"], row["dz"], row["reproj_px"])
print(result.warnings)                       # 예: "GCP G4: 정합된 사진 2장 이상에 표시해야 기준점으로 쓸 수 있음"
print(result.ortho.orthomosaic, result.ortho.epsg)   # GCP 좌표계(예: 5186)로 다시 만든 정사 모자이크
```

- 결과 정사 모자이크의 좌표계는 **GCP의 좌표계**가 됨 (GCP가 경위도 좌표계이거나 여러 좌표계가 섞이면 UTM 유지)
- `row["reproj_px"]`가 다른 점보다 유난히 크면 그 점의 표시 위치나 측량값을 의심함
- 검사점 오차가 기준점 오차보다 크게 나오는 것은 정상임

### 2.5 보정 방식 (기준점 수별)

| 사용 가능한 기준점 | `mode` | 처리 |
|---|---|---|
| 3점 이상 | `gcp` | 기준점을 삼각측량한 위치와 측량 좌표로 닮음변환(Umeyama)을 구해 GCP 좌표계로 옮긴 뒤, 기준점을 고정점으로 번들 조정 |
| 1~2점 | `gcp_shift` | 번들 조정 → GPS 정렬 → 기준점 평균 차이만큼 평행 이동 (회전·축척은 보정하지 않음, 경고 표시) |
| 0점 | `gps` | 내부표정(초점거리·왜곡) 고정 번들 조정 → GPS 재정렬. 오차 큰 관측 정리·타이포인트만 적용할 때 |

"사용 가능한 기준점"은 정합된 사진 2장 이상에 표시되어 삼각측량되는 `control` 점임.

**초점거리·왜곡 재추정 조건**: 기준점 4점 이상이고, 기준점 높이 차가 촬영 고도의 2% 이상일 때만 번들 조정에서 내부표정도 풂.
평탄지를 연직 촬영하면 초점거리와 고도가 서로 구분되지 않아(상관), 내부표정을 풀면 높이가 수십 m 흘러갈 수 있기 때문임.
이 경우 내부표정은 정렬 때 값으로 고정함.

## 3. 수동 타이포인트

자동 매칭이 약한 곳에 같은 지점을 여러 사진에 표시해 연결을 보강함. 사진 2장 이상에 표시해야 함.

```python
project.set_edits(tiepoints=[
    qo.TiePoint("T1", [qo.Mark("DJI_0031.JPG", 1022.0, 3010.5), qo.Mark("DJI_0032.JPG", 1040.2, 2011.7)]),
])
result = project.refine()
for row in result.raw["manual_tiepoints"]:
    print(row["id"], row["error_px"], row["marks"])   # 표시별 재투영 오차
```

- 수동 타이포인트는 오차 큰 관측 자동 제거 대상에서 빠짐 (보호됨)
- 재투영 오차가 수 px 이상이면 표시가 서로 다른 지점을 가리키는 것일 수 있음

## 4. 오차 큰 관측 정리

### 4.1 분석

```python
st = project.tiepoint_stats()
print(st["summary"])                   # 점 수, 관측 수, 평균·RMSE·95% 오차(px)
print(st["recommended_px"])            # 권장 자동 제거 기준
for p in st["threshold_preview"]:
    print(p["threshold_px"], f"{p['removed_ratio']:.1%}", round(p["rmse_px_after"], 3))
for p in st["worst"][:10]:
    print(p["id"], p["error_px"], p["track"], p["lon"], p["lat"])
```

- `threshold_preview`는 기준값마다 몇 %의 관측이 지워지고 RMSE가 얼마가 되는지 미리 보여 줌 (정렬 결과 기준)
- `recommended_px`는 관측을 10% 이하로 지우는 가장 작은 기준임. 데이터마다 오차 수준이 달라(특징점 추출 해상도 등) 분포로 정함
- `worst`는 점별 오차(관측 오차의 RMS)가 큰 점부터 나열함. `lon`, `lat`로 지도에 표시해 원인(파도, 차량 등)을 확인할 수 있음
- `map_points`는 지도 표시용 점 목록임 (최대 15000개. 오차 큰 점 `top`개는 항상 넣고 나머지는 무작위로 고르게 추출)

### 4.2 적용

```python
project.set_edits(max_reproj_error_px=st["recommended_px"])      # 기준보다 큰 관측 자동 제거
project.set_edits(deleted_points=[p["id"] for p in st["worst"][:20]])   # 점 단위 삭제
result = project.refine()
```

- 자동 제거는 번들 조정 전후 두 번 적용함 (조정 후 새로 커진 오차도 지움). GCP·수동 타이포인트는 제거하지 않음
- 삭제한 점 ID는 최초 정렬 결과의 점 ID이므로, 정렬을 다시 하면 목록을 비움

## 5. 수정 사항 형식 (`edits.json`)

`get_edits()` 반환값과 `project/edits.json`의 형식임. dict로 직접 만들어 `set_edits()`에 넘겨도 됨.

```json
{
  "version": 1,
  "deleted_points": [1532, 1771],
  "max_reproj_error_px": 2.0,
  "tiepoints": [
    {"id": "T1", "obs": [{"image": "DJI_0031.JPG", "x": 1022.0, "y": 3010.5},
                          {"image": "DJI_0032.JPG", "x": 1040.2, "y": 2011.7}]}
  ],
  "gcps": [
    {"name": "G1", "x": 204512.31, "y": 336201.84, "z": 12.35, "epsg": 5186, "role": "control",
     "obs": [{"image": "DJI_0013.JPG", "x": 2310.4, "y": 1520.8},
             {"image": "DJI_0014.JPG", "x": 2298.1, "y": 2410.3}]}
  ]
}
```

| 키 | 형식 | 설명 |
|---|---|---|
| `version` | int | 형식 버전 (1) |
| `deleted_points` | int[] | 지울 3D 점 ID (중복 제거·정렬해 저장) |
| `max_reproj_error_px` | float \| null | 자동 제거 기준 |
| `tiepoints[].id` | str | 타이포인트 ID |
| `tiepoints[].obs[]` | {image, x, y} | 사진 표시 |
| `gcps[].name` | str | 점 이름 |
| `gcps[].x`, `y`, `z` | float | 측량 좌표 (x 동쪽, y 북쪽) |
| `gcps[].epsg` | int | 측량 좌표계 |
| `gcps[].role` | `"control"` \| `"check"` | 기준점·검사점 (그 밖의 값은 `control`로 저장) |
| `gcps[].obs[]` | {image, x, y} | 사진 표시 |

`image`는 영상 폴더 기준 상대 경로이며, 정합되지 않은 사진의 표시는 보정에서 무시함.

## 6. 검증 결과

합성 GCP(정합 결과에 회전 0.5°, 축척 2%, 이동 수 m를 준 좌표를 측량값으로 사용, 표시 오차 0.3px)로 검증한 결과임.

| 데이터 | 기준/검사 | 검사점 RMSE 수평 / 수직 | 재투영 RMSE (자동 제거 기준) | 보정 시간 |
|---|---|---|---|---|
| Mavic 2 Pro 13장 | 4 / 2 | 0.025 / 0.017 m | 1.22 → 0.77 px (2 px) | 약 19초 |
| Aukerman 77장 | 5 / 3 | 0.050 / 0.141 m | 1.59 → 1.33 px (3 px) | 약 96초 |
| 합성 영상 12장 (GPS 오차 3.6m) | 3 / 2 | 0.026 / 0.064 m | 0.23 → 0.20 px (2 px) | 약 2초 |

실측 GCP 데이터로는 아직 검증하지 않았음.

## 7. 주의 사항

- GCP 보정 전 측량 표고와 GPS 고도는 기준면이 다를 수 있음(타원체고·해발고). 보정 후에는 GCP 기준으로 맞춰짐
- 표지(대공표지) 중심을 정확히 표시하는 것이 정확도에 가장 큰 영향을 줌. 확대 조각(`chips=True`)에서 표시하는 것을 권장함
- 롤링 셔터 카메라(Mavic 2)는 고속 비행 시 영상 안에서 위치가 흔들려, GCP 보정 후에도 오차가 남을 수 있음
