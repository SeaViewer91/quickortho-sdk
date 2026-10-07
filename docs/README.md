# QuickOrtho SDK 문서

대상 버전: 0.3.0

## 처음 쓰는 경우

1. [설치](installation.md): 요구 사항, 설치·업그레이드, 오프라인 설치, OS별 주의 사항
2. [빠른 시작](quickstart.md): 영상 폴더 하나로 정사 모자이크 만들기
3. [핵심 개념](concepts.md): 워크스페이스, 처리 단계, 좌표계, 정확도

## 개발할 때

- [API 레퍼런스](api-reference.md): 모든 공개 클래스·함수·옵션·결과·예외
- [산출물을 변수로 쓰기](outputs.md): 결과 풀기, 래스터·카메라·점군, 좌표 규약, 다른 라이브러리 연동
- [진행률과 중단](events-and-cancel.md): 이벤트 콜백, CancelToken, GUI·웹 서버 구성 방식
- [정밀 보정](refinement.md): GCP·타이포인트 보정, 수정 사항 형식
- [결과물과 보고서](report.md): 결과 파일, `report.json`·`preview.json` 필드
- [명령줄과 serve 프로토콜](cli.md): 명령 목록, JSON-lines 이벤트, 상주 모드
- [성능과 옵션 조정](performance.md): 측정치, 메모리·속도 조정

## 문제가 생겼을 때

- [문제 해결](troubleshooting.md): 오류 코드별 원인과 조치

## SDK 자체를 고칠 때

- [개발 참여](development.md): 개발 환경, 시험, CI, 릴리스 절차, 데스크톱 앱 번들

## 예제

[examples/](../examples/) 폴더:

| 파일 | 내용 |
|---|---|
| `01_quickstart.py` | 한 번에 처리 |
| `02_step_by_step.py` | 스캔 → 미리보기 → 정렬 → 정사 모자이크 두 가지 해상도 |
| `03_gcp_refine.py` | GCP 파일·사진 표시 CSV로 보정 |
| `04_thread_cancel.py` | 별도 스레드 처리, 진행률 큐, Ctrl+C 중단 |
| `05_serve_client.py` | serve 프로세스 클라이언트, 즉시 중단 |
| `06_batch_folders.py` | 여러 폴더 일괄 처리, 결과 CSV |

## 릴리스 노트

- [v0.3.0](release-notes/v0.3.0.md)
- [v0.2.1](release-notes/v0.2.1.md)
- [v0.2.0](release-notes/v0.2.0.md)
- [v0.1.0](release-notes/v0.1.0.md)
