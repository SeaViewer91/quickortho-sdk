# 산출물을 변수로 쓰기

처리 결과는 파일로 저장되는 것과 별개로, **파이썬 변수로 바로 받아** numpy·OpenCV·rasterio·shapely·PyTorch 같은
다른 라이브러리의 입력으로 쓸 수 있음. OpenCV 함수처럼 결과를 여러 변수로 풀어서 받으면 핵심 산출물이 차례로 나옴.

```python
import quickortho as qo

res = qo.process("flight/", "out/")                  # 결과 객체로 받기 (기존 방식)
image, transform, crs = qo.process("flight/", "out/")  # 여러 변수로 받기
```

두 방식은 같은 결과 객체에서 나옴. 결과 객체로 받은 뒤 나중에 풀어도 됨: `image, transform, crs = res`.

## 1. 결과별로 풀리는 값

| 호출 | 풀어서 받기 | 값의 형식 |
|---|---|---|
| `qo.scan(folder)` | `images, summary` | `list[dict]`, `dict` |
| `qo.preview(folder, out)` / `project.preview()` | `quicklook, coverage, transform, crs` | `(H, W, 4)` uint8 또는 `None`, `(H, W)` uint16, `Affine`, `CRS` |
| `project.align()` | `cameras, points, crs` | `Cameras`, `PointCloud`, `CRS` |
| `project.orthomosaic()` / `project.process()` / `qo.process()` | `image, transform, crs` | `(H, W, 4)` uint8, `Affine`, `CRS` |
| `project.refine()` | `image, transform, crs, gcps` | 위와 같음 + GCP 오차 표 `list[dict]` |
| `project.points()` | `xyz, rgb` | `(N, 3)` float64, `(N, 3)` uint8 |

워크스페이스만 있으면 처리 없이 읽을 수도 있음.

```python
image, transform, crs = qo.read_orthomosaic("out/")
z, transform_z, crs = qo.read_dsm("out/")
project = qo.Project.open("out/")
cameras = project.cameras()
xyz, rgb = project.points()
```

## 2. 래스터: `(array, transform, crs)`

### 배열

- 정사 모자이크: `(H, W, 4)` uint8, **채널이 마지막 축**, RGBA. 알파 0이 자료 없음
- DSM: `(H, W)` float32, 단위 m
- 중복 매수(미리보기): `(H, W)` uint16, 0이면 촬영 안 됨

채널 순서는 `order`로 바꿈 (`"RGBA"`, `"RGB"`, `"BGRA"`, `"BGR"`). OpenCV는 BGR을 씀.

```python
bgr, transform, crs = project.read_orthomosaic(order="BGR")
cv2.imwrite("ortho.jpg", bgr)
```

rasterio처럼 `(밴드, H, W)`가 필요하면 `np.moveaxis(image, -1, 0)` 또는 `qo.read_raster(path, channels_last=False)`를 씀.

### 큰 결과 읽기

정사 모자이크를 통째로 읽으면 `가로 × 세로 × 4` 바이트를 씀 (예: 20000 × 20000이면 1.6GB). 필요한 만큼만 읽음.

```python
small, tf_small, crs = res.read(scale=0.25)                          # 1/4 크기 (COG 오버뷰 사용, 빠름)
part, tf_part, crs = res.read(bounds=(xmin, ymin, xmax, ymax))       # 지도 좌표 범위만
```

돌려주는 `transform`은 **읽은 배열 기준**이라 그대로 좌표 계산에 쓰면 됨.

### transform (`affine.Affine`)

화소 ↔ 지도 좌표 변환임.

```python
x, y = transform * (col, row)                 # 화소 모서리 → 지도 좌표
x, y = transform * (col + 0.5, row + 0.5)     # 화소 중심 → 지도 좌표
col, row = ~transform * (x, y)                # 지도 좌표 → 화소
gsd = transform.a                             # 화소 크기(m). transform.e는 -gsd
gdal_gt = transform.to_gdal()                 # GDAL GeoTransform 튜플
```

### crs (`pyproj.CRS`)

```python
crs.to_epsg()        # 32652
crs.to_wkt()         # rasterio·GDAL에 넘길 때
```

### 예: 처리한 배열을 GeoTIFF로 저장

```python
import cv2, rasterio

image, transform, crs = project.read_orthomosaic()
edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY), 50, 150)
with rasterio.open("edges.tif", "w", driver="GTiff", width=edges.shape[1], height=edges.shape[0],
                   count=1, dtype="uint8", crs=crs.to_wkt(), transform=transform) as ds:
    ds.write(edges, 1)
```

### 예: DSM으로 부피 계산

```python
z, tf, crs = project.read_dsm()
base = np.percentile(z, 5)                                   # 기준면 높이
volume = np.clip(z - base, 0, None).sum() * abs(tf.a * tf.e)  # m³
```

간이 DSM은 점군 간격(수 m) 수준이라 부피는 대략값임.

### 예: 지도 좌표로 그리기 (matplotlib)

```python
b = res.bounds
plt.imshow(image, extent=(b[0], b[2], b[1], b[3]))
```

## 3. 카메라: `CameraPose`

`project.cameras()`(또는 `align()`을 풀어서 받은 `cameras`)는 정합된 영상의 카메라 목록임.
리스트처럼 쓰고, 파일 이름으로도 찾음.

```python
cameras = project.cameras()
cam = cameras["DJI_0013.JPG"]         # 또는 cameras[0]
cameras.centers                        # (N, 3) 카메라 위치
cameras.opk                            # (N, 3) ω, φ, κ (도)
cameras.to_csv("cameras.csv")          # name,x,y,z,omega,phi,kappa
```

| 속성 | 형식 | 설명 |
|---|---|---|
| `K` | (3, 3) | 카메라 행렬 (**OpenCV 화소 규약**) |
| `dist` | (4,) 또는 (8,) | OpenCV 왜곡 계수 `(k1, k2, p1, p2[, k3, k4, k5, k6])` |
| `R` | (3, 3) | 회전 (지도 → 카메라). 카메라 좌표는 x 오른쪽, y 아래, z 시선 방향 |
| `t` | (3,) | 이동 (지도 → 카메라). `X_cam = R @ X + t` |
| `rvec` | (3,) | OpenCV 회전 벡터 (Rodrigues) |
| `P` | (3, 4) | 투영 행렬 `K @ [R | t]` (왜곡 제외) |
| `center` | (3,) | 카메라 위치 (지도 좌표) |
| `opk` | (3,) | 사진측량 자세각 ω, φ, κ (도) |
| `width`, `height` | int | 사진 크기 |
| `model`, `params` | | COLMAP 카메라 모델과 파라미터 원본 |

| 메서드 | 설명 |
|---|---|
| `project(xyz)` | 지도 좌표 (N, 3) → 사진 좌표 (N, 2), SDK 규약, 왜곡 포함. 카메라 뒤쪽은 NaN |
| `in_image(uv)` | 사진 좌표가 사진 안인지 (N,) bool |
| `rays(uv)` | 사진 좌표 → 지도 좌표계의 단위 광선 방향 (N, 3) |
| `to_camera(xyz)` | 지도 좌표 → 카메라 좌표 |
| `to_dict()` | JSON으로 저장할 수 있는 dict |

### 화소 좌표 규약 (중요)

| 규약 | 좌상단 화소 중심 | 쓰는 곳 |
|---|---|---|
| **SDK** (COLMAP과 같음) | `(0.5, 0.5)` | `Mark`, `project()`, `rays()`, `image_to_ground()`, `ground_to_image()` |
| OpenCV | `(0, 0)` | `cam.K`와 함께 쓰는 OpenCV 함수 |

즉 SDK 좌표는 **사진 왼쪽 위 모서리가 (0, 0)**인 연속 좌표임. YOLO 같은 탐지 모델의 상자 좌표(x1, y1, x2, y2)도
같은 규약이라 그대로 넣으면 됨. OpenCV 함수의 결과에는 0.5를 더하면 SDK 좌표가 됨.

```python
import cv2

uv_cv, _ = cv2.projectPoints(xyz.reshape(-1, 1, 3), cam.rvec, cam.t, cam.K, cam.dist)
uv_sdk = uv_cv.reshape(-1, 2) + 0.5          # == cam.project(xyz)

img = cv2.imread(str(project.images / cam.name))
undistorted = cv2.undistort(img, cam.K, cam.dist)
```

### 자세각 ω, φ, κ

사진측량 표준(Wolf) 정의임. 사진 좌표계는 x 오른쪽, y 위쪽, z 뒤쪽(투영 중심 쪽)이며,
지도 → 사진 회전 `M = Mκ · Mφ · Mω`임. `M = diag(1, -1, -1) · R`의 관계가 있음.

- 연직 촬영이고 사진 위쪽이 북쪽이면 (0, 0, 0)
- 사진 위쪽이 북쪽에서 시계 방향으로 θ만큼 돌아가 있으면 κ ≈ -θ (예: 위쪽이 동쪽이면 κ ≈ -90°)
- `qo.rotation_from_opk(omega, phi, kappa)`로 R을 되살릴 수 있음

다른 사진측량 소프트웨어와 값을 비교할 때는 그 소프트웨어의 자세각 정의(회전 순서·축 방향)를 확인해야 함.

## 4. 점군: `PointCloud`

SfM 타이포인트(희소 점군)임.

```python
points = project.points()
xyz, rgb = points                               # (N, 3) float64, (N, 3) uint8
points.error                                    # (N,) 재투영 오차(px)
points.track_length                             # (N,) 관측 사진 수
good = points.filter(max_error_px=1.0, min_track_length=3)
good.to_ply("points.ply")                       # CloudCompare·MeshLab·Open3D
```

좌표값이 커서(UTM 수십만~수백만 m) float32로 바꾸면 정밀도가 떨어짐. Open3D 등에 넣을 때는 원점을 옮겨서 넣음.

```python
import open3d as o3d

origin = xyz.mean(axis=0)
pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz - origin))
pcd.colors = o3d.utility.Vector3dVector(rgb / 255.0)
```

## 5. 사진 좌표 ↔ 지도 좌표

정사영상이 아닌 **원본 사진**에서 탐지·측정한 결과를 지도로 옮길 때 씀. 원본 사진은 정사영상보다 해상도가 높고
블렌딩·이음선이 없어 탐지에 유리함.

```python
from shapely.geometry import Polygon

x1, y1, x2, y2 = box                                   # 원본 사진의 탐지 상자 (SDK 규약 = 모서리 원점)
corners = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
ground = project.image_to_ground("DJI_0013.JPG", corners)   # (4, 3) 지도 좌표 (DSM과 교차)
footprint = Polygon(ground[:, :2])

sea = project.image_to_ground("DJI_0013.JPG", corners, z=0.0)  # 높이 0 m 수평면과 교차 (수면 등)
uv = project.ground_to_image("DJI_0013.JPG", ground)           # 다시 사진 좌표로
```

- `image_to_ground`는 광선과 간이 DSM의 교점을 반복 계산으로 구함. 간이 DSM의 정밀도 때문에 합성 시험(고도 100m)에서
  수평 오차 중앙값이 약 13cm였음. 높이를 알면 `z`를 주는 것이 가장 정확함
- `z`는 결과 좌표계의 높이 기준임 (GCP 보정 전에는 GPS 고도 기준이라 해발고와 다를 수 있음)
- 위쪽을 향하는 광선은 NaN

## 6. 미리보기 결과

```python
pv = qo.preview("flight/", "pv/")
quicklook, coverage, transform, crs = pv
gaps = pv.gaps()                     # 누락 구역 shapely Polygon 목록 (지도 좌표)
gaps_ll = pv.gaps(crs="lonlat")      # 경위도
footprints = pv.footprints()         # 영상별 촬영 범위 (pv.footprint_names와 같은 순서)
gap_mask = pv.read_gap_mask()        # (H, W) bool, coverage와 같은 격자
```

`coverage`는 `pv/coverage.tif`(밴드 1: 중복 매수, 밴드 2: 누락)로도 저장되어 GIS에서 열 수 있음.

geopandas로 보내기:

```python
import geopandas as gpd

gdf = gpd.GeoDataFrame(geometry=pv.gaps(), crs=crs).assign(kind="gap")
gdf.to_file("gaps.gpkg")
```

## 7. 스캔 결과

```python
images, summary = qo.scan("flight/")
positions = qo.scan("flight/").positions()     # (N, 3) [경도, 위도, 고도], GPS 없는 행은 NaN
```

## 8. 딥러닝 입력

```python
import torch

image, transform, crs = project.read_orthomosaic(order="RGB")
tensor = torch.from_numpy(image).permute(2, 0, 1).float() / 255.0   # (3, H, W)

# 큰 정사영상을 타일로 나눠 추론
res = project.result()
xmin, ymin, xmax, ymax = res.bounds
size = 512 * res.gsd_m                                                # 타일 한 변(m)
for y in np.arange(ymax, ymin, -size):
    for x in np.arange(xmin, xmax, size):
        tile, tile_tf, _ = res.read(bounds=(x, y - size, x + size, y))
        # 탐지 결과의 화소 좌표 → 지도 좌표: tile_tf * (col, row)
```

## 9. 시기별 결과를 같은 격자로 만들기

같은 좌표계·GSD·범위·격자 기준점을 주면 여러 시기의 정사 모자이크가 화소 단위로 정확히 겹침.
배열끼리 바로 빼거나 비교할 수 있음.

```python
grid = dict(epsg=5186, gsd_m=0.05, bounds=(204000, 336000, 204600, 336500), grid_origin=(0, 0))
a, tf_a, _ = qo.Project.create("2026-09/", "ws_09").process(qo.OrthoOptions(**grid))
b, tf_b, _ = qo.Project.create("2026-10/", "ws_10").process(qo.OrthoOptions(**grid))
assert tf_a == tf_b and a.shape == b.shape
diff = np.abs(a[..., :3].astype(int) - b[..., :3].astype(int)).sum(axis=-1)
```

`epsg`는 정렬 단계에서 정해지고, `bounds`·`grid_origin`·`gsd_m`은 정사 모자이크 단계에서 씀.
절대 위치가 GPS 수준(수 m)이면 시기 사이에 그만큼 어긋나므로, 정밀한 비교에는 GCP 보정이 필요함.
