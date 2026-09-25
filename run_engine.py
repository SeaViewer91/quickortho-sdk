"""PyInstaller 진입점. 배포용 단일 실행 폴더(onedir)를 만들 때 사용한다."""

import multiprocessing

from quickortho_engine.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows에서 동결 실행 시 필요
    raise SystemExit(main())
