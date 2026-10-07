# 설치

## 1. 요구 사항

| 항목 | 조건 |
|---|---|
| Python | 3.10 ~ 3.14 (64비트) |
| macOS | 14(Sonoma) 이상, Apple Silicon(M1 이상) |
| Windows | 10·11 x64 |
| Ubuntu | 20.04 이상 x86_64 |
| 메모리 | 8GB 이상 권장 (측정 피크: 18장 1.4GB, 77장 1.1GB. [성능](performance.md) 참고) |
| 디스크 | 설치 약 700MB (의존성 포함), 작업마다 원본 영상 용량의 약 30~40% (기본 GSD 기준) |
| GPU | 필요 없음 (CPU만 사용) |

지원 범위를 정한 이유는 핵심 의존성인 pycolmap이 배포하는 바이너리(wheel) 범위 때문임.

- macOS: pycolmap이 macOS 14 이상 arm64용만 배포함. Intel 맥과 macOS 13 이하는 설치 단계에서 실패함
- Linux: pycolmap이 manylinux_2_28(glibc 2.28 이상) x86_64용을 배포함. Ubuntu 18.04(glibc 2.27)는 설치할 수 없음
- Windows: x64용만 배포함

별도 시스템 패키지(GDAL, COLMAP 등)를 따로 설치할 필요는 없음. 모두 pip wheel에 포함되어 있음.

## 2. 가상환경 만들기

다른 프로젝트와 의존성이 섞이지 않도록 가상환경 사용을 권장함.

macOS·Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Windows(PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

- PowerShell에서 스크립트 실행이 막혀 있으면 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`를 한 번 실행함
- conda 환경도 쓸 수 있음. 다만 conda의 GDAL과 pip의 rasterio를 섞으면 충돌할 수 있으므로 rasterio도 pip로 설치함

## 3. wheel 설치

[Releases](https://github.com/SeaViewer91/quickortho-sdk/releases)에서 버전을 고른 뒤 wheel 주소로 설치함.
wheel은 순수 파이썬 패키지(`py3-none-any`)라서 파일 하나로 세 OS 모두 설치됨.

```bash
pip install https://github.com/SeaViewer91/quickortho-sdk/releases/download/v0.3.0/quickortho_sdk-0.3.0-py3-none-any.whl
```

wheel 파일을 내려받아 두었다면 파일 경로로 설치해도 됨.

```bash
pip install ./quickortho_sdk-0.3.0-py3-none-any.whl
```

Git 태그에서 바로 설치할 수도 있음 (Git 필요).

```bash
pip install "quickortho-sdk @ git+https://github.com/SeaViewer91/quickortho-sdk@v0.3.0"
```

### 다른 프로젝트의 의존성으로 지정

`requirements.txt`:

```text
quickortho-sdk @ https://github.com/SeaViewer91/quickortho-sdk/releases/download/v0.3.0/quickortho_sdk-0.3.0-py3-none-any.whl
```

`pyproject.toml`:

```toml
[project]
dependencies = [
    "quickortho-sdk @ https://github.com/SeaViewer91/quickortho-sdk/releases/download/v0.3.0/quickortho_sdk-0.3.0-py3-none-any.whl",
]
```

PyPI에는 아직 올리지 않았으므로 `pip install quickortho-sdk`만으로는 설치되지 않음.

## 4. 설치 확인

```bash
quickortho version
```

다음과 같은 한 줄이 나오면 정상임.

```json
{"type": "result", "command": "version", "data": {"engine": "0.3.0", "sdk": "0.3.0", "protocol": 1, "python": "3.12.7", "os": "Darwin", "arch": "arm64"}}
```

파이썬에서 확인:

```python
import quickortho as qo
print(qo.__version__)
```

`import quickortho`는 pycolmap·rasterio 등을 함께 불러오므로 처음 한 번은 1~3초 걸릴 수 있음.

## 5. 업그레이드와 삭제

```bash
pip install --upgrade <새 버전 wheel 주소>
pip uninstall quickortho-sdk
```

업그레이드 전에 [릴리스 노트](release-notes/)에서 호환성 변경 사항을 확인함.
0.x 버전에서는 부 버전(0.1 → 0.2)이 바뀔 때 공개 API가 바뀔 수 있음.

## 6. 오프라인 설치

인터넷이 없는 서버·현장 노트북에 설치하려면, **같은 OS·같은 Python 버전**의 인터넷 되는 컴퓨터에서
의존성 wheel을 모두 내려받아 옮김.

```bash
# 인터넷 되는 컴퓨터 (대상과 OS·Python 버전이 같아야 함)
mkdir wheelhouse
pip download -d wheelhouse ./quickortho_sdk-0.3.0-py3-none-any.whl

# 대상 컴퓨터로 wheelhouse 폴더를 옮긴 뒤
pip install --no-index --find-links wheelhouse quickortho-sdk
```

OS가 다른 컴퓨터에서 받아야 하면 `--platform`, `--python-version`, `--only-binary=:all:`을 지정함.

```bash
pip download -d wheelhouse --only-binary=:all: \
    --platform win_amd64 --python-version 3.12 \
    ./quickortho_sdk-0.3.0-py3-none-any.whl
```

## 7. OS별 주의 사항

### macOS

- 시스템 기본 `python3`(Xcode 명령줄 도구)는 버전이 낮을 수 있음. [python.org](https://www.python.org/downloads/) 설치본이나
  Homebrew(`brew install python@3.12`)를 권장함
- 설치가 `pycolmap`에서 "no matching distribution"으로 실패하면 macOS 버전(14 이상)과 칩(Apple Silicon)을 확인함.
  Rosetta로 실행한 x86_64 Python에서도 같은 오류가 남 (`python -c "import platform; print(platform.machine())"`가 `arm64`여야 함)

### Windows

- Microsoft Store판 Python도 동작하지만, 경로가 길어져 문제가 생길 수 있으므로 python.org 설치본을 권장함
- 한글·공백이 들어간 경로도 지원함. 명령줄 출력은 항상 UTF-8임
- 백신이 처음 실행하는 네이티브 라이브러리를 검사하느라 첫 `import`가 느릴 수 있음

### Ubuntu

- `python3-venv` 패키지가 없으면 가상환경을 만들 수 없음: `sudo apt install python3-venv`
- Ubuntu 20.04의 기본 Python은 3.8이라 지원 범위 밖임. deadsnakes PPA 등으로 3.10 이상을 설치함
- 서버(화면 없음) 환경에서도 그대로 동작함. OpenCV는 headless 판을 씀

## 8. 소스에서 설치 (개발용)

```bash
git clone https://github.com/SeaViewer91/quickortho-sdk
cd quickortho-sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

자세한 내용은 [개발 참여](development.md)를 참고함.
