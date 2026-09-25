# PyInstaller 번들 (데스크톱 앱용)

QuickOrtho 데스크톱 앱에 넣을 단일 폴더(onedir) 실행 파일을 만드는 스펙임. 파이썬이 없는 컴퓨터에서도 SDK의
명령줄·serve 모드가 동작함.

```bash
pip install pyinstaller <SDK wheel 경로 또는 저장소 루트>
cd packaging/pyinstaller
pyinstaller -y quickortho-engine.spec
dist/quickortho-engine/quickortho-engine version
printf '{"job":1,"argv":["version"]}\n' | dist/quickortho-engine/quickortho-engine serve
```

- 결과: `dist/quickortho-engine/` (약 600MB, 압축 시 약 210MB)
- 번들을 만드는 OS용으로만 만들어짐. OS마다 해당 OS에서 빌드함 (앱 저장소의 CI가 수행)
- 자세한 내용은 [개발 참여](../../docs/development.md#8-데스크톱-앱용-번들-pyinstaller) 참고
