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
```

- `scan`: 폴더 내 JPG의 EXIF·DJI XMP를 읽어 위치·고도·짐벌 자세·카메라 정보를 수집하고,
  카메라별로 묶은 뒤 매핑용(광각) 카메라를 자동 선별함

## 개발

```bash
cd engine
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```
