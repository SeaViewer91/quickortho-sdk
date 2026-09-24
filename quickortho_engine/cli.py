"""엔진 명령줄 진입점."""

from __future__ import annotations

import argparse
import platform
import sys
import traceback
from pathlib import Path

from . import __version__
from .protocol import Emitter
from .scan import scan_folder


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickortho-engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="엔진 버전과 실행 환경 출력")

    p_scan = sub.add_parser("scan", help="영상 폴더의 EXIF/XMP 스캔")
    p_scan.add_argument("folder", type=Path)
    p_scan.add_argument("--recursive", action="store_true", help="하위 폴더까지 스캔")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows에서 한글 경로·메시지가 깨지지 않도록 stdout을 UTF-8로 고정
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = _build_parser().parse_args(argv)
    out = Emitter()
    try:
        if args.command == "version":
            out.result(
                "version",
                {
                    "engine": __version__,
                    "python": platform.python_version(),
                    "os": platform.system(),
                    "arch": platform.machine(),
                },
            )
        elif args.command == "scan":
            out.result("scan", scan_folder(args.folder, recursive=args.recursive, emitter=out))
        return 0
    except Exception as exc:  # 앱이 항상 error 이벤트를 받도록 함
        out.error(str(exc), traceback.format_exc())
        return 1
